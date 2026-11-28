"""SEC EDGAR HTTP client.

- Rate-limited to <= 5 req/s (default 0.22s interval)
- Local disk response cache under data/raw/edgar/
- Exponential backoff retry on HTTP 429 / 5xx and transient network errors
- Fails fast without network access if SEC_USER_AGENT is unset or a placeholder
"""

from __future__ import annotations

import gzip
import json
import time
from pathlib import Path
from typing import Any

import requests

from src.env import RAW_DIR, get_sec_user_agent

EDGAR_CACHE_DIR = RAW_DIR / "edgar"
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANY_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SUBMISSIONS_EXTRA_URL = "https://data.sec.gov/submissions/{filename}"
ARCHIVE_INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/index.json"
ARCHIVE_DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{doc_name}"
EFTS_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def normalize_cik(cik: int | str) -> str:
    """Normalize a CIK into a 10-digit zero-padded string."""
    digits = "".join(ch for ch in str(cik) if ch.isdigit())
    if not digits:
        raise ValueError(f"Invalid CIK: {cik!r}")
    return digits.zfill(10)


class EdgarClient:
    """Rate-limited, disk-cached SEC EDGAR HTTP client."""

    def __init__(
        self,
        cache_dir: Path = EDGAR_CACHE_DIR,
        min_interval: float = 0.22,
        timeout: float = 30.0,
        max_retries: int = 5,
    ) -> None:
        self.cache_dir = cache_dir
        self.min_interval = max(min_interval, 0.20)
        self.timeout = timeout
        self.max_retries = max_retries
        self._last_request_at = 0.0
        self._session: requests.Session | None = None

    def _get_session(self) -> requests.Session:
        if self._session is None:
            # Validate SEC_USER_AGENT immediately before any outbound request
            user_agent = get_sec_user_agent()
            session = requests.Session()
            session.headers.update(
                {
                    "User-Agent": user_agent,
                    "Accept-Encoding": "gzip, deflate",
                }
            )
            self._session = session
        return self._session

    def _wait_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    @staticmethod
    def _is_cache_fresh(path: Path, max_age_days: float | None) -> bool:
        """Return True if cached file is within max_age_days (None = never expires)."""
        if max_age_days is None:
            return True
        if max_age_days <= 0:
            return False
        age_sec = time.time() - path.stat().st_mtime
        return age_sec < (max_age_days * 86400.0)

    @staticmethod
    def _read_cached_file(path: Path) -> bytes:
        raw = path.read_bytes()
        if path.suffix == ".gz":
            return gzip.decompress(raw)
        return raw

    def _find_existing_cache(self, cache_path: Path, compress: bool) -> Path | None:
        """Return the active cache file path (preferring .gz when compress=True, with legacy fallback)."""
        if compress:
            gz_path = cache_path.with_suffix(cache_path.suffix + ".gz")
            if gz_path.is_file():
                return gz_path
        if cache_path.is_file():
            return cache_path
        return None

    def fetch_bytes_with_status(
        self,
        url: str,
        cache_rel_path: str | Path,
        force_refresh: bool = False,
        offline_only: bool = False,
        max_age_days: float | None = None,
        compress: bool = False,
    ) -> tuple[bytes, bool]:
        """Fetch raw bytes from URL with disk caching, returning (bytes, changed).

        `changed` is True only when a network fetch occurred and the response bytes
        differed from any previously cached file. When `compress=True`, new downloads
        are stored as `.gz` while transparently reading any legacy uncompressed file.
        """
        cache_path = self.cache_dir / cache_rel_path
        write_path = cache_path.with_suffix(cache_path.suffix + ".gz") if compress else cache_path
        not_found_marker = cache_path.with_suffix(cache_path.suffix + ".404")
        existing_path = self._find_existing_cache(cache_path, compress=compress)

        if not force_refresh:
            if existing_path is not None and (
                offline_only or self._is_cache_fresh(existing_path, max_age_days)
            ):
                return self._read_cached_file(existing_path), False
            if not_found_marker.is_file() and (
                offline_only or self._is_cache_fresh(not_found_marker, max_age_days)
            ):
                raise RuntimeError(f"SEC EDGAR request failed with HTTP 404 (cached) for {url}")
            if offline_only:
                raise RuntimeError(f"SEC EDGAR cache miss in offline mode for {url}")

        old_bytes = self._read_cached_file(existing_path) if existing_path is not None else None
        session = self._get_session()
        last_error: Exception | None = None

        for attempt in range(self.max_retries):
            self._wait_rate_limit()
            try:
                self._last_request_at = time.monotonic()
                response = session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                backoff = min(2**attempt, 30)
                time.sleep(backoff)
                continue

            if response.status_code == 200:
                data = response.content
                write_path.parent.mkdir(parents=True, exist_ok=True)
                if not_found_marker.exists():
                    not_found_marker.unlink()
                disk_bytes = gzip.compress(data, compresslevel=6) if compress else data
                tmp_path = write_path.with_suffix(write_path.suffix + ".tmp")
                tmp_path.write_bytes(disk_bytes)
                tmp_path.replace(write_path)
                # Remove legacy uncompressed copy once compressed copy is safely in place
                if compress and cache_path != write_path and cache_path.is_file():
                    cache_path.unlink()
                return data, (old_bytes != data)

            if response.status_code == 404:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                not_found_marker.write_text("404\n", encoding="utf-8")
                raise RuntimeError(
                    f"SEC EDGAR request failed with HTTP 404 for {url}"
                )

            if response.status_code in RETRYABLE_STATUS_CODES:
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait_sec = min(int(retry_after), 60)
                else:
                    wait_sec = min(2**attempt, 30)
                last_error = RuntimeError(
                    f"SEC EDGAR returned HTTP {response.status_code} for {url}"
                )
                time.sleep(wait_sec)
                continue

            raise RuntimeError(
                f"SEC EDGAR request failed with HTTP {response.status_code} for {url}"
            )

        raise RuntimeError(
            f"SEC EDGAR request failed after {self.max_retries} attempts for {url}: {last_error}"
        )

    def refresh_bytes_if_stale(
        self,
        url: str,
        cache_rel_path: str | Path,
        force_refresh: bool = False,
        offline_only: bool = False,
        max_age_days: float | None = None,
        compress: bool = False,
    ) -> bool:
        """Refresh cached file only when missing or older than max_age_days; return True if bytes changed.

        Avoids reading or decoding large cached files on fresh cache hits.
        """
        cache_path = self.cache_dir / cache_rel_path
        not_found_marker = cache_path.with_suffix(cache_path.suffix + ".404")
        existing_path = self._find_existing_cache(cache_path, compress=compress)
        if not force_refresh:
            if existing_path is not None and (
                offline_only or self._is_cache_fresh(existing_path, max_age_days)
            ):
                return False
            if not_found_marker.is_file() and (
                offline_only or self._is_cache_fresh(not_found_marker, max_age_days)
            ):
                raise RuntimeError(f"SEC EDGAR request failed with HTTP 404 (cached) for {url}")
            if offline_only:
                raise RuntimeError(f"SEC EDGAR cache miss in offline mode for {url}")

        _, changed = self.fetch_bytes_with_status(
            url,
            cache_rel_path,
            force_refresh=force_refresh,
            offline_only=offline_only,
            max_age_days=max_age_days,
            compress=compress,
        )
        return changed

    def fetch_bytes(
        self,
        url: str,
        cache_rel_path: str | Path,
        force_refresh: bool = False,
        offline_only: bool = False,
        max_age_days: float | None = None,
    ) -> bytes:
        """Fetch raw bytes from URL and cache under data/raw/edgar."""
        data, _ = self.fetch_bytes_with_status(
            url,
            cache_rel_path,
            force_refresh=force_refresh,
            offline_only=offline_only,
            max_age_days=max_age_days,
        )
        return data

    def fetch_json(
        self,
        url: str,
        cache_rel_path: str | Path,
        force_refresh: bool = False,
        offline_only: bool = False,
        max_age_days: float | None = None,
    ) -> Any:
        raw = self.fetch_bytes(
            url,
            cache_rel_path,
            force_refresh=force_refresh,
            offline_only=offline_only,
            max_age_days=max_age_days,
        )
        return json.loads(raw.decode("utf-8"))

    def get_company_tickers(
        self,
        force_refresh: bool = False,
        max_age_days: float | None = None,
        offline_only: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """Fetch SEC's official ticker-to-CIK mapping JSON."""
        return self.fetch_json(
            COMPANY_TICKERS_URL,
            "company_tickers.json",
            force_refresh=force_refresh,
            offline_only=offline_only,
            max_age_days=max_age_days,
        )

    def build_ticker_to_cik_map(
        self,
        force_refresh: bool = False,
        max_age_days: float | None = None,
        offline_only: bool = False,
    ) -> dict[str, str]:
        """Return a mapping of uppercase ticker to 10-digit CIK."""
        raw = self.get_company_tickers(
            force_refresh=force_refresh,
            max_age_days=max_age_days,
            offline_only=offline_only,
        )
        mapping: dict[str, str] = {}
        for item in raw.values():
            ticker = str(item.get("ticker", "")).strip().upper()
            cik = item.get("cik_str")
            if ticker and cik is not None:
                mapping[ticker] = normalize_cik(cik)
        return mapping

    def get_company_facts(
        self,
        cik: int | str,
        force_refresh: bool = False,
        offline_only: bool = False,
        max_age_days: float | None = None,
    ) -> dict[str, Any]:
        """Fetch XBRL companyfacts JSON for the given CIK."""
        payload, _ = self.get_company_facts_with_status(
            cik,
            force_refresh=force_refresh,
            offline_only=offline_only,
            max_age_days=max_age_days,
        )
        return payload

    def get_company_facts_mtime(self, cik: int | str) -> float:
        """Return cache mtime (seconds since epoch) for CIK companyfacts, or 0.0 if missing."""
        cik10 = normalize_cik(cik)
        cache_path = self.cache_dir / "companyfacts" / f"CIK{cik10}.json"
        existing = self._find_existing_cache(cache_path, compress=True)
        if existing is not None:
            return existing.stat().st_mtime
        marker = cache_path.with_suffix(cache_path.suffix + ".404")
        if marker.is_file():
            return marker.stat().st_mtime
        return 0.0

    def get_company_facts_with_status(
        self,
        cik: int | str,
        force_refresh: bool = False,
        offline_only: bool = False,
        max_age_days: float | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Fetch XBRL companyfacts JSON (stored gzip-compressed) and return (payload, changed)."""
        cik10 = normalize_cik(cik)
        url = COMPANY_FACTS_URL.format(cik=cik10)
        raw, changed = self.fetch_bytes_with_status(
            url,
            Path("companyfacts") / f"CIK{cik10}.json",
            force_refresh=force_refresh,
            offline_only=offline_only,
            max_age_days=max_age_days,
            compress=True,
        )
        return json.loads(raw.decode("utf-8")), changed

    def refresh_company_facts(
        self,
        cik: int | str,
        force_refresh: bool = False,
        offline_only: bool = False,
        max_age_days: float | None = None,
    ) -> bool:
        """Refresh cached XBRL companyfacts JSON if stale and return True if bytes changed."""
        cik10 = normalize_cik(cik)
        url = COMPANY_FACTS_URL.format(cik=cik10)
        return self.refresh_bytes_if_stale(
            url,
            Path("companyfacts") / f"CIK{cik10}.json",
            force_refresh=force_refresh,
            offline_only=offline_only,
            max_age_days=max_age_days,
            compress=True,
        )

    def get_submissions(
        self,
        cik: int | str,
        force_refresh: bool = False,
        offline_only: bool = False,
        max_age_days: float | None = None,
    ) -> dict[str, Any]:
        """Fetch the submissions JSON listing filings and entity metadata (including SIC) for the given CIK."""
        cik10 = normalize_cik(cik)
        url = SUBMISSIONS_URL.format(cik=cik10)
        return self.fetch_json(
            url,
            Path("submissions") / f"CIK{cik10}.json",
            force_refresh=force_refresh,
            offline_only=offline_only,
            max_age_days=max_age_days,
        )

    def get_all_filings(
        self,
        cik: int | str,
        forms: set[str] | None = None,
        force_refresh: bool = False,
        max_age_days: float | None = None,
        min_filing_date: str | None = None,
        offline_only: bool = False,
    ) -> list[dict[str, str]]:
        """Return all filings combining recent and historical archive submission files."""
        sub = self.get_submissions(
            cik,
            force_refresh=force_refresh,
            offline_only=offline_only,
            max_age_days=max_age_days,
        )
        chunks: list[dict[str, list[Any]]] = []
        recent = (sub.get("filings") or {}).get("recent")
        if recent:
            chunks.append(recent)

        for extra in (sub.get("filings") or {}).get("files") or []:
            fname = extra.get("name")
            if not fname:
                continue
            filing_to = str(extra.get("filingTo") or "")
            if min_filing_date and filing_to and filing_to < min_filing_date:
                continue
            extra_data = self.fetch_json(
                SUBMISSIONS_EXTRA_URL.format(filename=fname),
                Path("submissions") / fname,
                force_refresh=force_refresh,
                offline_only=offline_only,
                max_age_days=max_age_days,
            )
            chunks.append(extra_data)

        rows: list[dict[str, str]] = []
        for chunk in chunks:
            accns = chunk.get("accessionNumber") or []
            form_list = chunk.get("form") or []
            filing_dates = chunk.get("filingDate") or []
            report_dates = chunk.get("reportDate") or []
            primary_docs = chunk.get("primaryDocument") or []
            for i, accn in enumerate(accns):
                form = str(form_list[i]) if i < len(form_list) else ""
                if forms is not None and form not in forms:
                    continue
                rows.append(
                    {
                        "accessionNumber": str(accn),
                        "form": form,
                        "filingDate": str(filing_dates[i]) if i < len(filing_dates) else "",
                        "reportDate": str(report_dates[i]) if i < len(report_dates) else "",
                        "primaryDocument": str(primary_docs[i]) if i < len(primary_docs) else "",
                    }
                )
        return rows

    def get_filing_index(
        self,
        cik: int | str,
        accession: str,
        force_refresh: bool = False,
        offline_only: bool = False,
    ) -> dict[str, Any]:
        """Fetch index.json for a filing directory."""
        cik_int = int(normalize_cik(cik))
        acc_nodash = accession.replace("-", "")
        url = ARCHIVE_INDEX_URL.format(cik_int=cik_int, acc_nodash=acc_nodash)
        return self.fetch_json(
            url,
            Path("archives") / str(cik_int) / acc_nodash / "index.json",
            force_refresh=force_refresh,
            offline_only=offline_only,
        )

    def get_filing_document(
        self,
        cik: int | str,
        accession: str,
        doc_name: str,
        force_refresh: bool = False,
        offline_only: bool = False,
    ) -> bytes:
        """Fetch an individual document (XML / HTML, etc.) from a filing."""
        cik_int = int(normalize_cik(cik))
        acc_nodash = accession.replace("-", "")
        # Strip XSL prefix if present (e.g. xslFormNPORT-P_X01/primary_doc.xml)
        clean_name = doc_name.split("/")[-1]
        url = ARCHIVE_DOC_URL.format(
            cik_int=cik_int,
            acc_nodash=acc_nodash,
            doc_name=clean_name,
        )
        return self.fetch_bytes(
            url,
            Path("archives") / str(cik_int) / acc_nodash / clean_name,
            force_refresh=force_refresh,
            offline_only=offline_only,
        )

    def search_efts_accessions(
        self,
        query: str,
        forms: str,
        cache_key: str,
        force_refresh: bool = False,
        max_age_days: float | None = None,
        offline_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Search SEC EDGAR EFTS full-text index for matching filing accessions."""
        from urllib.parse import urlencode

        all_hits: list[dict[str, Any]] = []
        start_from = 0
        page_size = 100
        while True:
            params = urlencode(
                {
                    "q": query,
                    "forms": forms,
                    "from": start_from,
                    "size": page_size,
                }
            )
            url = f"{EFTS_SEARCH_URL}?{params}"
            data = self.fetch_json(
                url,
                Path("efts") / f"{cache_key}_from_{start_from}.json",
                force_refresh=force_refresh,
                offline_only=offline_only,
                max_age_days=max_age_days,
            )
            hits = ((data.get("hits") or {}).get("hits")) or []
            if not hits:
                break
            all_hits.extend(hits)
            total_val = ((data.get("hits") or {}).get("total") or {}).get("value", 0)
            start_from += len(hits)
            if start_from >= total_val:
                break
        return all_hits
