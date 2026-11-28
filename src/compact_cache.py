"""Compress legacy uncompressed SEC EDGAR CompanyFacts JSON cache files into .json.gz.

Usage:
    python -m src.compact_cache            # Compress legacy .json files atomically
    python -m src.compact_cache --dry-run  # Report savings without modifying disk
"""

from __future__ import annotations

import argparse
import gzip
from pathlib import Path

from src.env import RAW_DIR

COMPANYFACTS_DIR = RAW_DIR / "edgar" / "companyfacts"


def compact_companyfacts_cache(
    cache_dir: Path = COMPANYFACTS_DIR,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Gzip-compress uncompressed CIK*.json files in cache_dir with round-trip verification."""
    if not cache_dir.is_dir():
        return {"files": 0, "before_bytes": 0, "after_bytes": 0, "removed_duplicates": 0}

    targets = sorted(p for p in cache_dir.glob("*.json") if p.is_file())
    before_bytes = 0
    after_bytes = 0
    converted = 0
    removed_duplicates = 0

    for json_path in targets:
        gz_path = json_path.with_suffix(".json.gz")
        raw_size = json_path.stat().st_size
        before_bytes += raw_size

        if gz_path.is_file():
            # Verify existing .gz before removing the legacy uncompressed copy
            try:
                gzip.decompress(gz_path.read_bytes())
                gz_ok = True
            except Exception:  # noqa: BLE001
                gz_ok = False
            if gz_ok:
                after_bytes += gz_path.stat().st_size
                if not dry_run:
                    json_path.unlink()
                removed_duplicates += 1
                continue

        raw_bytes = json_path.read_bytes()
        compressed = gzip.compress(raw_bytes, compresslevel=6)
        after_bytes += len(compressed)
        if not dry_run:
            tmp_path = gz_path.with_suffix(".gz.tmp")
            tmp_path.write_bytes(compressed)
            if gzip.decompress(tmp_path.read_bytes()) != raw_bytes:
                tmp_path.unlink(missing_ok=True)
                raise RuntimeError(f"Gzip verification failed for {json_path.name}")
            tmp_path.replace(gz_path)
            json_path.unlink()
        converted += 1

    return {
        "files": converted,
        "removed_duplicates": removed_duplicates,
        "before_bytes": before_bytes,
        "after_bytes": after_bytes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compress legacy uncompressed CompanyFacts JSON cache files into .json.gz."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show estimated disk savings without modifying files.",
    )
    args = parser.parse_args()
    stats = compact_companyfacts_cache(dry_run=args.dry_run)
    saved_mb = (stats["before_bytes"] - stats["after_bytes"]) / (1024 * 1024)
    before_mb = stats["before_bytes"] / (1024 * 1024)
    after_mb = stats["after_bytes"] / (1024 * 1024)
    prefix = "[DRY RUN] " if args.dry_run else ""
    print(
        f"{prefix}Compacted {stats['files']} file(s) "
        f"(removed {stats['removed_duplicates']} redundant duplicate(s)): "
        f"{before_mb:.1f} MB -> {after_mb:.1f} MB (saved {saved_mb:.1f} MB)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
