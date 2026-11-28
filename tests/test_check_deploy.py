"""Unit tests for src/check.py, src/deploy.py, and src/compact_cache.py."""

from __future__ import annotations

import gzip
import json
import tempfile
import unittest
from pathlib import Path

from src import check as integrity_check
from src.compact_cache import compact_companyfacts_cache
from src.deploy import _compute_asset_hash, collect_dist_files


class CheckScannerTests(unittest.TestCase):
    def test_detects_leaks_unsafe_dom_cdn_and_non_english(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            clean = root / "clean.js"
            clean.write_text("export const contact = 'demo@example.com';\n", encoding="utf-8")
            errs: list[str] = []
            integrity_check._scan_file_content(clean, "web/js/clean.js", ["SECRETVALUE123"], "alice", errs)
            self.assertEqual(errs, [])

            bad_email = root / "email.txt"
            bad_email.write_text("owner: real.person" + "@" + "corp-domain.io\n", encoding="utf-8")
            errs = []
            integrity_check._scan_file_content(bad_email, "docs/email.txt", [], "", errs)
            self.assertTrue(any("Real email" in e for e in errs))

            bad_secret = root / "secret.txt"
            bad_secret.write_text("token is SECRETVALUE123\n", encoding="utf-8")
            errs = []
            integrity_check._scan_file_content(bad_secret, "docs/secret.txt", ["SECRETVALUE123"], "", errs)
            self.assertTrue(any("Secret value" in e for e in errs))

            bad_dom = root / "dom.js"
            bad_dom.write_text("node.inner" + "HTML = '<b>x</b>';\n", encoding="utf-8")
            errs = []
            integrity_check._scan_file_content(bad_dom, "web/js/dom.js", [], "", errs)
            self.assertTrue(any("unsafe DOM" in e for e in errs))

            bad_cdn = root / "cdn.html"
            bad_cdn.write_text("<script src='https://cdn.jsdelivr.net/npm/chart.js'></script>\n", encoding="utf-8")
            errs = []
            integrity_check._scan_file_content(bad_cdn, "web/cdn.html", [], "", errs)
            self.assertTrue(any("External CDN" in e for e in errs))

            bad_i18n = root / "i18n.md"
            bad_i18n.write_text("Hello \u30c6\u30b9\u30c8\n", encoding="utf-8")
            errs = []
            integrity_check._scan_file_content(bad_i18n, "docs/i18n.md", [], "", errs)
            self.assertTrue(any("[I18N]" in e for e in errs))


class DeployHelperTests(unittest.TestCase):
    def test_asset_hash_deterministic_and_32_hex(self) -> None:
        h1 = _compute_asset_hash(b"hello world", "json")
        h2 = _compute_asset_hash(b"hello world", "json")
        h3 = _compute_asset_hash(b"hello world", "js")
        self.assertEqual(h1, h2)
        self.assertNotEqual(h1, h3)
        self.assertEqual(len(h1), 32)
        self.assertTrue(all(c in "0123456789abcdef" for c in h1))

    def test_collect_dist_files_separates_headers_and_skips_ds_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            dist = Path(tmpdir)
            (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
            (dist / "_headers").write_text("/*\n  X-Frame-Options: DENY\n", encoding="utf-8")
            (dist / ".DS_Store").write_bytes(b"junk")
            (dist / "data").mkdir()
            (dist / "data" / "meta.json").write_text(json.dumps({"etfs": []}), encoding="utf-8")

            assets, special = collect_dist_files(dist)
            rel_urls = sorted(a.rel_url for a in assets)
            self.assertEqual(rel_urls, ["/data/meta.json", "/index.html"])
            self.assertIn("_headers", special)

    def test_collect_dist_files_rejects_missing_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(RuntimeError):
                collect_dist_files(Path(tmpdir))


class CompactCacheTests(unittest.TestCase):
    def test_compact_converts_json_and_removes_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cdir = Path(tmpdir)
            raw1 = json.dumps({"cik": 1, "facts": {"us-gaap": {"Revenues": [1, 2, 3]}}}).encode("utf-8")
            raw2 = json.dumps({"cik": 2, "facts": {}}).encode("utf-8")
            (cdir / "CIK0000000001.json").write_bytes(raw1)
            (cdir / "CIK0000000002.json").write_bytes(raw2)
            (cdir / "CIK0000000002.json.gz").write_bytes(gzip.compress(raw2))

            dry = compact_companyfacts_cache(cdir, dry_run=True)
            self.assertEqual(dry["files"], 1)
            self.assertEqual(dry["removed_duplicates"], 1)
            self.assertTrue((cdir / "CIK0000000001.json").is_file())

            res = compact_companyfacts_cache(cdir, dry_run=False)
            self.assertEqual(res["files"], 1)
            self.assertEqual(res["removed_duplicates"], 1)
            self.assertFalse((cdir / "CIK0000000001.json").exists())
            self.assertFalse((cdir / "CIK0000000002.json").exists())
            self.assertEqual(gzip.decompress((cdir / "CIK0000000001.json.gz").read_bytes()), raw1)
            self.assertEqual(gzip.decompress((cdir / "CIK0000000002.json.gz").read_bytes()), raw2)


if __name__ == "__main__":
    unittest.main()
