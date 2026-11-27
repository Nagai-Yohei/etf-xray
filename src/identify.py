"""Ticker, CIK, and security_id entity resolution engine.

AGENTS.md resolution priority:
1. config/manual_tickers.csv (CUSIP override)
2. OpenFIGI (CUSIP / ISIN)
3. Legacy filing name bridge (unambiguous normalized names from N-PORT holdings)
4. config/manual_tickers.csv (name override)
5. SEC company_tickers.json (normalized name match)

Required invariants:
- Raise an exception if duplicate security_ids appear within the same (etf_id, as_of).
- Disambiguate dual-class shares (e.g. CWEN / CWEN.A) via CUSIP or exact manual overrides.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from src.adapters.nport import FilingHoldings, RawHolding
from src.edgar import EdgarClient, normalize_cik
from src.env import CONFIG_DIR, RAW_DIR, get_openfigi_api_key
from src.fundamentals import load_cik_overrides

MANUAL_TICKERS_PATH = CONFIG_DIR / "manual_tickers.csv"
OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
OPENFIGI_CACHE_PATH = RAW_DIR / "openfigi" / "cache.json"

# U.S. exchange priority (US = U.S. composite preferred first)
US_EXCHANGE_PRIORITY = {
    "US": 0,
    "UN": 1,  # NYSE
    "UW": 2,  # NASDAQ GS
    "UQ": 3,  # NASDAQ GM
    "UR": 4,  # NASDAQ CM
    "UA": 5,  # NYSE American
    "UP": 6,  # NYSE Arca
    "UF": 7,  # Cboe BZX
}

_PUNCT_RE = re.compile(r"[^a-z0-9&/\s]+")
_SPACE_RE = re.compile(r"\s+")
# Trailing SEC state codes / qualifiers (e.g. /DE/, /NEW/, /OH/, /MD/, trailing /)
_SEC_SLASH_SUFFIX_RE = re.compile(r"(?:\s*/[A-Za-z0-9]{1,5}/?|\s*/)+$")
# Legal entity suffixes stripped during normalized company-name matching
_CORP_SUFFIX_RE = re.compile(
    r"(?:\s+(?:inc|incorporated|corp|corporation|co|company|cos|companies|ltd|limited|plc|nv|sa|lp|llc|holdings|holding|group))+$"
)
_CLASS_SUFFIX_RE = re.compile(
    r"(?:,?\s+(?:(?:class|cl\.?)\s+([a-z])(?:\s+shares?)?|non[\s-]?voting(?:\s+shares?)?))$",
    re.IGNORECASE,
)
# Non-common-stock ticker suffixes (preferred series, warrants, units, rights)
_NON_COMMON_TICKER_RE = re.compile(
    r"(?:\.(?:P[A-Z]|PR[A-Z]?|WS|WT|W|U|R)|R)$"
)


def is_non_common_ticker(ticker: str | None) -> bool:
    """Return True for preferred shares, warrants, units, or rights tickers (e.g. OXY.WS, AHL.PD)."""
    if not ticker:
        return False
    t = ticker.upper()
    if t.endswith((".WS", ".WT", ".PR")) or re.search(r"\.(?:P[A-Z]|PR[A-Z])$", t):
        return True
    if t in {"TMUSR"}:
        return True
    return False


@dataclass
class ResolvedHolding:
    """Single holding record with resolved security identifiers."""

    security_id: str
    ticker: str | None
    reported_name: str
    shares: float
    value_usd: float
    weight: float
    pct_net_assets: float | None
    rank: int
    cusip: str | None
    isin: str | None
    cik: str | None
    ticker_source: str


def normalize_ticker(raw_ticker: str | None) -> str | None:
    if not raw_ticker:
        return None
    t = raw_ticker.strip().upper()
    if not t:
        return None
    # Normalize OpenFIGI slash notation (BF/B, CWEN/A) and SEC hyphen notation (BF-B) to dot notation
    t = t.replace("/", ".").replace("-", ".")
    return t


def normalize_company_name(
    name: str,
    strip_corp: bool = True,
    strip_class: bool = False,
) -> str:
    """Build a normalized company-name key (retains share-class suffixes by default)."""
    s = name.strip()
    # Strip trailing "/The", trailing "(The)", or leading "The "
    s = re.sub(r"/\s*The$", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"\(\s*The\s*\)$", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"^The\s+", "", s, flags=re.IGNORECASE).strip()
    # Strip SEC trailing state/qualifier suffixes (e.g. /DE/, /NEW/)
    s = _SEC_SLASH_SUFFIX_RE.sub("", s).strip()

    # Temporarily extract share class (Class A / Cl A / Non Voting, etc.)
    share_class = ""
    m_cls = _CLASS_SUFFIX_RE.search(s)
    if m_cls:
        if not strip_class and m_cls.group(1):
            share_class = f" class {m_cls.group(1).lower()}"
        s = s[: m_cls.start()].strip()

    s = s.lower()
    # Remove apostrophes without inserting spaces (McDonald's -> mcdonalds)
    s = s.replace("'", "").replace("’", "")
    s = s.replace("&", " and ")
    s = _PUNCT_RE.sub(" ", s)
    s = _SPACE_RE.sub(" ", s).strip()

    if strip_corp:
        prev = None
        while prev != s:
            prev = s
            s = _CORP_SUFFIX_RE.sub("", s).strip()

    return (s + share_class).strip()


def load_manual_tickers(
    path: Path = MANUAL_TICKERS_PATH,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Load manual_tickers.csv and return (cusip_map, exact_name_map, norm_name_map)."""
    by_cusip: dict[str, str] = {}
    by_exact_name: dict[str, str] = {}
    by_norm_name: dict[str, str] = {}

    if not path.is_file():
        return by_cusip, by_exact_name, by_norm_name

    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            name = (row.get("name") or "").strip()
            cusip = (row.get("cusip") or "").strip().upper()
            ticker = normalize_ticker(row.get("ticker"))
            if not ticker:
                continue
            if cusip:
                by_cusip[cusip] = ticker
            elif name:
                by_exact_name[name.lower()] = ticker
                by_norm_name[normalize_company_name(name, strip_corp=False)] = ticker
                by_norm_name[normalize_company_name(name, strip_corp=True)] = ticker

    return by_cusip, by_exact_name, by_norm_name


class OpenFigiClient:
    """Disk-cached OpenFIGI mapping API client."""

    def __init__(self, cache_path: Path = OPENFIGI_CACHE_PATH) -> None:
        self.cache_path = cache_path
        self.api_key = get_openfigi_api_key()
        self.cache: dict[str, dict[str, Any] | None] = self._load_cache()

    def _load_cache(self) -> dict[str, dict[str, Any] | None]:
        if not self.cache_path.is_file():
            return {}
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(self.cache, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self.cache_path)

    @staticmethod
    def _pick_best_us_equity(items: list[dict[str, Any]]) -> dict[str, Any] | None:
        best: tuple[int, int, dict[str, Any]] | None = None
        for item in items:
            ticker = normalize_ticker(item.get("ticker"))
            if not ticker:
                continue
            exch = str(item.get("exchCode") or "").upper()
            if exch not in US_EXCHANGE_PRIORITY:
                continue
            sec_type = str(item.get("securityType") or "").lower()
            is_common = 0 if ("common" in sec_type or "reit" in sec_type or "adr" in sec_type) else 1
            score = (is_common, US_EXCHANGE_PRIORITY[exch])
            if best is None or score < (best[0], best[1]):
                best = (score[0], score[1], item)
        return best[2] if best else None

    def resolve_batch(
        self,
        queries: list[tuple[str, str]],
        offline_only: bool = False,
    ) -> None:
        """Batch-resolve uncached (idType, idValue) pairs via OpenFIGI API and persist to cache."""
        missing: list[tuple[str, str]] = []
        seen: set[str] = set()
        for id_type, id_val in queries:
            cache_key = f"{id_type}:{id_val}"
            if cache_key in self.cache or cache_key in seen:
                continue
            seen.add(cache_key)
            missing.append((id_type, id_val))

        if not missing or offline_only:
            return

        # Free tier allows 10 jobs/request; authenticated API key allows 100 jobs/request
        batch_size = 100 if self.api_key else 10
        min_wait = 0.35 if self.api_key else 2.6

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-OPENFIGI-APIKEY"] = self.api_key

        session = requests.Session()
        session.headers.update(headers)

        for start in range(0, len(missing), batch_size):
            batch = missing[start : start + batch_size]
            payload = [
                {"idType": id_type, "idValue": id_val, "exchCode": "US"}
                for id_type, id_val in batch
            ]

            resp_data: list[dict[str, Any]] | None = None
            for attempt in range(5):
                try:
                    resp = session.post(OPENFIGI_URL, json=payload, timeout=30)
                except requests.RequestException:
                    time.sleep(min(2**attempt, 20))
                    continue

                if resp.status_code == 200:
                    resp_data = resp.json()
                    break
                if resp.status_code in (429, 500, 502, 503, 504):
                    retry_after = resp.headers.get("Retry-After")
                    wait = int(retry_after) if (retry_after and retry_after.isdigit()) else min(5 * (attempt + 1), 60)
                    time.sleep(wait)
                    continue
                raise RuntimeError(f"OpenFIGI API error HTTP {resp.status_code}")

            if resp_data is None:
                raise RuntimeError("OpenFIGI API failed after retries")

            # Retry items returning empty with exchCode=US without an exchange filter (for some ADRs/ISINs)
            retry_no_exch: list[tuple[int, str, str]] = []
            for idx, ((id_type, id_val), entry) in enumerate(zip(batch, resp_data)):
                cache_key = f"{id_type}:{id_val}"
                items = entry.get("data") or []
                picked = self._pick_best_us_equity(items)
                if picked:
                    self.cache[cache_key] = {
                        "ticker": normalize_ticker(picked.get("ticker")),
                        "name": picked.get("name"),
                        "exchCode": picked.get("exchCode"),
                        "securityType": picked.get("securityType"),
                    }
                else:
                    retry_no_exch.append((idx, id_type, id_val))
                    self.cache[cache_key] = None

            if retry_no_exch:
                time.sleep(min_wait)
                payload2 = [{"idType": t, "idValue": v} for _, t, v in retry_no_exch]
                try:
                    resp2 = session.post(OPENFIGI_URL, json=payload2, timeout=30)
                    if resp2.status_code == 200:
                        for (_, id_type, id_val), entry2 in zip(retry_no_exch, resp2.json()):
                            picked2 = self._pick_best_us_equity(entry2.get("data") or [])
                            if picked2:
                                self.cache[f"{id_type}:{id_val}"] = {
                                    "ticker": normalize_ticker(picked2.get("ticker")),
                                    "name": picked2.get("name"),
                                    "exchCode": picked2.get("exchCode"),
                                    "securityType": picked2.get("securityType"),
                                }
                except requests.RequestException:
                    pass

            # Save cache incrementally after each batch for crash resilience
            self._save_cache()
            if start + batch_size < len(missing):
                time.sleep(min_wait)

    def lookup(self, cusip: str | None, isin: str | None) -> str | None:
        if cusip:
            hit = self.cache.get(f"ID_CUSIP:{cusip}")
            if hit and hit.get("ticker"):
                return str(hit["ticker"])
        if isin:
            hit = self.cache.get(f"ID_ISIN:{isin}")
            if hit and hit.get("ticker"):
                return str(hit["ticker"])
        return None


def _build_sec_maps(
    client: EdgarClient,
    *,
    max_age_days: float | None = None,
    offline_only: bool = False,
) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """Build (ticker->cik, norm_name->(ticker, cik)) maps from SEC company_tickers.json."""
    raw = client.get_company_tickers(
        max_age_days=max_age_days,
        offline_only=offline_only,
    )
    ticker_to_cik: dict[str, str] = {}
    name_to_sec: dict[str, tuple[str, str]] = {}

    for item in raw.values():
        ticker = normalize_ticker(item.get("ticker"))
        title = str(item.get("title") or "").strip()
        cik_raw = item.get("cik_str")
        if not ticker or cik_raw is None:
            continue
        cik10 = normalize_cik(cik_raw)
        ticker_to_cik[ticker] = cik10
        # Register both dot and hyphen variants (e.g. BF-B <-> BF.B)
        if "-" in ticker:
            ticker_to_cik.setdefault(ticker.replace("-", "."), cik10)
        if "." in ticker:
            ticker_to_cik.setdefault(ticker.replace(".", "-"), cik10)

        if title:
            # Skip preferred/warrant/unit/right tickers when building name->ticker lookup so acquired
            # common stocks (e.g. TRTN, AHL, ANAT) are never mapped to surviving preferred tickers
            if is_non_common_ticker(ticker) or ticker.endswith((".W", ".U", ".R")):
                continue
            for strip_corp in (False, True):
                nk = normalize_company_name(title, strip_corp=strip_corp)
                if nk and nk not in name_to_sec:
                    name_to_sec[nk] = (ticker, cik10)

    return ticker_to_cik, name_to_sec


def resolve_all_filings(
    client: EdgarClient,
    filings: list[FilingHoldings],
    *,
    tickers_max_age_days: float | None = None,
    offline_only: bool = False,
) -> tuple[dict[str, list[ResolvedHolding]], dict[str, dict[str, Any]]]:
    """Resolve holdings across all reporting dates and return (by_as_of_holdings, securities_catalog)."""
    manual_cusip, manual_exact_name, manual_norm_name = load_manual_tickers()
    cik_overrides = load_cik_overrides()
    ticker_to_cik, sec_name_map = _build_sec_maps(
        client,
        max_age_days=tickers_max_age_days,
        offline_only=offline_only,
    )
    figi = OpenFigiClient()

    # 1. Collect and batch-resolve OpenFIGI queries for filings with CUSIP / ISIN (N-PORT, etc.)
    figi_queries: list[tuple[str, str]] = []
    for filing in filings:
        for h in filing.holdings:
            if h.cusip and h.cusip in manual_cusip:
                continue
            if h.cusip:
                figi_queries.append(("ID_CUSIP", h.cusip))
            elif h.isin:
                figi_queries.append(("ID_ISIN", h.isin))
    figi.resolve_batch(figi_queries, offline_only=offline_only)

    # 2. Pass 1: Build a normalized-name bridge from filings with CUSIP / ISIN (N-PORT).
    #    Exclude ambiguous keys shared by multiple tickers (e.g. GOOG/GOOGL, FOX/FOXA, BRK.A/BRK.B, CWEN/CWEN.A)
    #    to prevent misattribution.
    nport_name_bridge: dict[str, tuple[str, str | None, str | None]] = {}
    nport_name_tickers: dict[str, set[str]] = {}
    for filing in sorted(filings, key=lambda f: f.as_of, reverse=True):
        for h in filing.holdings:
            if not h.cusip and not h.isin:
                continue
            ticker: str | None = None
            if h.cusip and h.cusip in manual_cusip:
                ticker = manual_cusip[h.cusip]
            elif h.isin and h.isin in manual_cusip:
                ticker = manual_cusip[h.isin]
            else:
                ticker = figi.lookup(h.cusip, h.isin)
            if not ticker:
                low = h.reported_name.lower()
                ticker = manual_exact_name.get(low) or manual_norm_name.get(
                    normalize_company_name(h.reported_name, strip_corp=True)
                )
            if ticker:
                for strip_corp in (False, True):
                    nk = normalize_company_name(h.reported_name, strip_corp=strip_corp)
                    if not nk:
                        continue
                    nport_name_tickers.setdefault(nk, set()).add(ticker)
                    if nk not in nport_name_bridge:
                        nport_name_bridge[nk] = (ticker, h.cusip, h.isin)
    for nk, tset in nport_name_tickers.items():
        if len(tset) > 1:
            nport_name_bridge.pop(nk, None)

    # Build a collision-free spaceless bridge (recovers legacy PDF-to-HTML dropped spaces, e.g. "3MCo.", "SyscoCorp.")
    spaceless_bridge: dict[str, tuple[str, str | None, str | None, str | None, str]] = {}
    spaceless_tickers: dict[str, set[str]] = {}

    def _register_spaceless(
        raw_key: str,
        tkr: str,
        b_cusip: str | None,
        b_isin: str | None,
        b_cik: str | None,
        src_label: str,
    ) -> None:
        sk = raw_key.replace(" ", "")
        if len(sk) < 4:
            return
        spaceless_tickers.setdefault(sk, set()).add(tkr)
        if sk not in spaceless_bridge:
            spaceless_bridge[sk] = (tkr, b_cusip, b_isin, b_cik, src_label)

    for nk, (tkr, b_cusip, b_isin) in nport_name_bridge.items():
        _register_spaceless(nk, tkr, b_cusip, b_isin, None, "legacy_bridge")
    for nk, tkr in manual_norm_name.items():
        _register_spaceless(nk, tkr, None, None, None, "manual_name")
    for nk, (tkr, b_cik) in sec_name_map.items():
        _register_spaceless(nk, tkr, None, None, b_cik, "sec_name")
    for sk, tset in spaceless_tickers.items():
        if len(tset) > 1:
            spaceless_bridge.pop(sk, None)

    # 3. Pass 2: Resolve every holding across all reporting dates following priority order
    resolved_by_as_of: dict[str, list[ResolvedHolding]] = {}
    securities_catalog: dict[str, dict[str, Any]] = {}

    for filing in sorted(filings, key=lambda f: f.as_of):
        seen_ids: set[str] = set()
        resolved_rows: list[ResolvedHolding] = []
        total_val = filing.total_value_usd

        for rank, h in enumerate(filing.holdings, start=1):
            ticker: str | None = None
            source = "unresolved"
            cusip = h.cusip
            isin = h.isin
            cik: str | None = None

            # Priority 1: manual_tickers.csv (CUSIP or ISIN override)
            if cusip and cusip in manual_cusip:
                ticker = manual_cusip[cusip]
                source = "manual_cusip"
            elif isin and isin in manual_cusip:
                ticker = manual_cusip[isin]
                source = "manual_cusip"

            # Priority 2: OpenFIGI (CUSIP / ISIN)
            if ticker is None and (cusip or isin):
                figi_ticker = figi.lookup(cusip, isin)
                if figi_ticker:
                    ticker = figi_ticker
                    source = "openfigi"

            # Priority 3a: Exact-name manual override for legacy filings (disambiguates classes / legacy entities)
            if ticker is None and not cusip and not isin:
                low = h.reported_name.lower()
                if low in manual_exact_name:
                    ticker = manual_exact_name[low]
                    source = "manual_name"

            # Priority 3b: Legacy name bridge (preserving share class)
            if ticker is None and not cusip and not isin:
                for strip_corp in (False, True):
                    nk = normalize_company_name(
                        h.reported_name, strip_corp=strip_corp, strip_class=False
                    )
                    if nk in nport_name_bridge:
                        b_ticker, b_cusip, b_isin = nport_name_bridge[nk]
                        ticker = b_ticker
                        cusip = cusip or b_cusip
                        isin = isin or b_isin
                        source = "legacy_bridge"
                        break

            # Priority 4a: manual_tickers.csv (name match preserving share class)
            if ticker is None:
                low = h.reported_name.lower()
                if low in manual_exact_name:
                    ticker = manual_exact_name[low]
                    source = "manual_name"
                else:
                    for strip_corp in (False, True):
                        nk = normalize_company_name(
                            h.reported_name, strip_corp=strip_corp, strip_class=False
                        )
                        if nk in manual_norm_name:
                            ticker = manual_norm_name[nk]
                            source = "manual_name"
                            break

            # Priority 3c: Legacy name bridge (ignoring "Class A" suffix on single-class issuers)
            if ticker is None and not cusip and not isin:
                for strip_corp in (False, True):
                    nk = normalize_company_name(
                        h.reported_name, strip_corp=strip_corp, strip_class=True
                    )
                    if nk in nport_name_bridge:
                        b_ticker, b_cusip, b_isin = nport_name_bridge[nk]
                        ticker = b_ticker
                        cusip = cusip or b_cusip
                        isin = isin or b_isin
                        source = "legacy_bridge"
                        break

            # Priority 4b: manual_tickers.csv (name match ignoring share class fallback)
            if ticker is None:
                for strip_corp in (False, True):
                    nk = normalize_company_name(
                        h.reported_name, strip_corp=strip_corp, strip_class=True
                    )
                    if nk in manual_norm_name:
                        ticker = manual_norm_name[nk]
                        source = "manual_name"
                        break

            # Priority 5: SEC company_tickers.json (normalized company-name match)
            if ticker is None:
                for strip_class in (False, True):
                    for strip_corp in (False, True):
                        nk = normalize_company_name(
                            h.reported_name, strip_corp=strip_corp, strip_class=strip_class
                        )
                        if nk in sec_name_map:
                            ticker, cik = sec_name_map[nk]
                            source = "sec_name"
                            break
                    if ticker is not None:
                        break

            # Priority 6: Unambiguous spaceless match for legacy filings (recovers dropped spaces like "3MCo.")
            if ticker is None and not cusip and not isin:
                for strip_class in (False, True):
                    for strip_corp in (False, True):
                        sk = normalize_company_name(
                            h.reported_name, strip_corp=strip_corp, strip_class=strip_class
                        ).replace(" ", "")
                        if sk in spaceless_bridge:
                            b_tkr, b_cusip, b_isin, b_cik, b_src = spaceless_bridge[sk]
                            ticker = b_tkr
                            cusip = cusip or b_cusip
                            isin = isin or b_isin
                            cik = cik or b_cik
                            source = b_src
                            break
                    if ticker is not None:
                        break

            # Resolve CIK:
            # 1) Never attach common-stock CIK to warrants/preferreds/rights or .OLD legacy entities
            # 2) Respect explicit cik_overrides.json (including explicit null to block reused tickers)
            # 3) Only strip single-letter share-class suffixes (.A/.B/.C) when falling back to parent CIK
            if ticker and (is_non_common_ticker(ticker) or ticker.endswith(".OLD")):
                cik = None
            elif ticker and ticker in cik_overrides:
                cik = cik_overrides[ticker].get("cik")
            elif cik is None and ticker:
                cik = ticker_to_cik.get(ticker) or ticker_to_cik.get(ticker.replace(".", "-"))
                if cik is None and "." in ticker:
                    base, suffix = ticker.split(".", 1)
                    if suffix in {"A", "B", "C"}:
                        cik = ticker_to_cik.get(base)

            # Assign canonical security_id
            if ticker:
                security_id = ticker
            elif cusip:
                security_id = f"CUSIP_{cusip}"
            elif isin:
                security_id = f"ISIN_{isin}"
            else:
                digest = hashlib.sha1(
                    normalize_company_name(h.reported_name).encode("utf-8")
                ).hexdigest()[:8].upper()
                security_id = f"UNK_{digest}"

            # Required check: raise on duplicate security_id within the same (etf_id, as_of)
            if security_id in seen_ids:
                raise ValueError(
                    f"Duplicate security_id {security_id!r} in {filing.etf_id} on {filing.as_of} "
                    f"(reported_name={h.reported_name!r}, cusip={cusip!r})"
                )
            seen_ids.add(security_id)

            weight = (h.value_usd / total_val) if total_val > 0 else 0.0
            resolved_rows.append(
                ResolvedHolding(
                    security_id=security_id,
                    ticker=ticker,
                    reported_name=h.reported_name,
                    shares=h.shares,
                    value_usd=h.value_usd,
                    weight=weight,
                    pct_net_assets=h.pct_net_assets,
                    rank=rank,
                    cusip=cusip,
                    isin=isin,
                    cik=cik,
                    ticker_source=source,
                )
            )

            # Update securities catalog (prefer newer reporting date names and identifiers)
            if security_id not in securities_catalog:
                securities_catalog[security_id] = {
                    "security_id": security_id,
                    "ticker": ticker,
                    "name": h.reported_name,
                    "cusip": cusip,
                    "isin": isin,
                    "cik": cik,
                    "ticker_source": source,
                    "first_seen": filing.as_of,
                    "last_seen": filing.as_of,
                }
            else:
                cat = securities_catalog[security_id]
                cat["name"] = h.reported_name
                cat["last_seen"] = max(cat["last_seen"], filing.as_of)
                cat["first_seen"] = min(cat["first_seen"], filing.as_of)
                if cusip:
                    cat["cusip"] = cusip
                if isin:
                    cat["isin"] = isin
                if cik and not cat["cik"]:
                    cat["cik"] = cik
                if source in ("manual_cusip", "openfigi") and cat["ticker_source"] not in ("manual_cusip", "openfigi"):
                    cat["ticker_source"] = source

        resolved_by_as_of[filing.as_of] = resolved_rows

    return resolved_by_as_of, securities_catalog
