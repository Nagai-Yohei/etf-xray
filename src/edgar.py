"""SEC EDGAR HTTP クライアント。

- 5 req/s 以下にレート制限する（既定 0.22 秒間隔）
- data/raw/edgar/ にレスポンスをローカルキャッシュする
- 429 / 5xx や一時的な通信エラーは指数バックオフで再試行する
- SEC_USER_AGENT 未設定時は外部アクセスせず明示エラーで停止する
"""

from __future__ import annotations

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
    """CIK を 10 桁ゼロ埋め文字列に正規化する。"""
    digits = "".join(ch for ch in str(cik) if ch.isdigit())
    if not digits:
        raise ValueError(f"Invalid CIK: {cik!r}")
    return digits.zfill(10)


class EdgarClient:
    """レート制限とディスクキャッシュを備えた SEC EDGAR クライアント。"""

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
            # 外部アクセス直前に SEC_USER_AGENT を必須検証する
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

    def fetch_bytes(
        self,
        url: str,
        cache_rel_path: str | Path,
        force_refresh: bool = False,
        offline_only: bool = False,
    ) -> bytes:
        """URL からバイト列を取得し、data/raw/edgar 配下にキャッシュする。"""
        cache_path = self.cache_dir / cache_rel_path
        not_found_marker = cache_path.with_suffix(cache_path.suffix + ".404")
        if not force_refresh:
            if cache_path.is_file():
                return cache_path.read_bytes()
            if not_found_marker.is_file():
                raise RuntimeError(f"SEC EDGAR request failed with HTTP 404 (cached) for {url}")
            if offline_only:
                raise RuntimeError(f"SEC EDGAR cache miss in offline mode for {url}")

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
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                if not_found_marker.exists():
                    not_found_marker.unlink()
                tmp_path = cache_path.with_suffix(cache_path.suffix + ".tmp")
                tmp_path.write_bytes(data)
                tmp_path.replace(cache_path)
                return data

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

    def fetch_json(
        self,
        url: str,
        cache_rel_path: str | Path,
        force_refresh: bool = False,
        offline_only: bool = False,
    ) -> Any:
        raw = self.fetch_bytes(
            url, cache_rel_path, force_refresh=force_refresh, offline_only=offline_only
        )
        return json.loads(raw.decode("utf-8"))

    def get_company_tickers(self, force_refresh: bool = False) -> dict[str, dict[str, Any]]:
        """SEC 公式のティッカー ↔ CIK 対応表を取得する。"""
        return self.fetch_json(
            COMPANY_TICKERS_URL,
            "company_tickers.json",
            force_refresh=force_refresh,
        )

    def build_ticker_to_cik_map(self, force_refresh: bool = False) -> dict[str, str]:
        """ティッカー（大文字）→ 10桁 CIK の辞書を返す。"""
        raw = self.get_company_tickers(force_refresh=force_refresh)
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
    ) -> dict[str, Any]:
        """指定 CIK の XBRL companyfacts JSON を取得する。"""
        cik10 = normalize_cik(cik)
        url = COMPANY_FACTS_URL.format(cik=cik10)
        return self.fetch_json(
            url,
            Path("companyfacts") / f"CIK{cik10}.json",
            force_refresh=force_refresh,
            offline_only=offline_only,
        )

    def get_submissions(self, cik: int | str, force_refresh: bool = False) -> dict[str, Any]:
        """指定 CIK の提出書類一覧（submissions JSON）を取得する。"""
        cik10 = normalize_cik(cik)
        url = SUBMISSIONS_URL.format(cik=cik10)
        return self.fetch_json(
            url,
            Path("submissions") / f"CIK{cik10}.json",
            force_refresh=force_refresh,
        )

    def get_all_filings(
        self,
        cik: int | str,
        forms: set[str] | None = None,
        force_refresh: bool = False,
    ) -> list[dict[str, str]]:
        """recent と過去アーカイブ JSON をすべて結合した提出書類リストを返す。"""
        sub = self.get_submissions(cik, force_refresh=force_refresh)
        chunks: list[dict[str, list[Any]]] = []
        recent = (sub.get("filings") or {}).get("recent")
        if recent:
            chunks.append(recent)

        for extra in (sub.get("filings") or {}).get("files") or []:
            fname = extra.get("name")
            if not fname:
                continue
            extra_data = self.fetch_json(
                SUBMISSIONS_EXTRA_URL.format(filename=fname),
                Path("submissions") / fname,
                force_refresh=force_refresh,
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
    ) -> dict[str, Any]:
        """提出書類ディレクトリの index.json を取得する。"""
        cik_int = int(normalize_cik(cik))
        acc_nodash = accession.replace("-", "")
        url = ARCHIVE_INDEX_URL.format(cik_int=cik_int, acc_nodash=acc_nodash)
        return self.fetch_json(
            url,
            Path("archives") / str(cik_int) / acc_nodash / "index.json",
            force_refresh=force_refresh,
        )

    def get_filing_document(
        self,
        cik: int | str,
        accession: str,
        doc_name: str,
        force_refresh: bool = False,
    ) -> bytes:
        """提出書類内の個別ドキュメント（XML / HTML 等）を取得する。"""
        cik_int = int(normalize_cik(cik))
        acc_nodash = accession.replace("-", "")
        # doc_name に XSL プレフィックス（例: xslFormNPORT-P_X01/primary_doc.xml）がある場合は除去
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
        )

    def search_efts_accessions(
        self,
        query: str,
        forms: str,
        cache_key: str,
        force_refresh: bool = False,
    ) -> list[dict[str, Any]]:
        """SEC EDGAR 公式全文検索（EFTS）で該当提出の accession 一覧を取得する。"""
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
