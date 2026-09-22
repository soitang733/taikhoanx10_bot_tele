"""Validate compressed files, keys, references, and manifest row counts."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def rows_for(dataset):
    for part in dataset["files"]:
        path = ROOT / part["path"]
        if path.stat().st_size != part["bytes"] or digest(path) != part["sha256"]:
            raise AssertionError(f"Checksum mismatch: {path}")
        with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != dataset["columns"]:
                raise AssertionError(f"Header mismatch: {path}")
            yield from reader


def main():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    source = ROOT.parent / "analysis_data" / "stocks_analysis.sqlite"
    if source.exists() and manifest.get("source_sha256"):
        if source.stat().st_size != manifest["source_bytes"] or digest(source) != manifest["source_sha256"]:
            raise AssertionError("Bundle was generated from an older SQLite database")
    datasets = {item["table"]: item for item in manifest["datasets"]}
    company_ids = {row["id"] for row in rows_for(datasets["companies"])}
    sources = {row["id"]: row["name"] for row in rows_for(datasets["data_sources"])}
    source_ids = set(sources)
    item_ids = {row["id"] for row in rows_for(datasets["financial_items"])}
    action_ids = {row["id"] for row in rows_for(datasets["action_types"])}
    counts = {}

    for name, dataset in datasets.items():
        count = 0
        previous_key = None
        for row in rows_for(dataset):
            count += 1
            if name == "price_daily":
                key = (row["company_id"], row["date"])
                assert row["company_id"] in company_ids and row["source_id"] in source_ids
                assert all(row[column] != "" for column in ("open", "high", "low", "close", "adjusted_close"))
                if sources[row["source_id"]] == "DNSE":
                    assert len(row["dnse_checksum"]) == 66 and row["dnse_checksum"].startswith("\\x")
            elif name == "financial_values":
                key = (row["company_id"], row["period_kind"], row["period_end"], row["statement_id"], row["item_id"])
                assert row["company_id"] in company_ids and row["item_id"] in item_ids and row["source_id"] in source_ids
            elif name == "corporate_actions":
                key = (int(row["id"]),)
                assert row["company_id"] in company_ids and row["action_type_id"] in action_ids and row["source_id"] in source_ids
            else:
                key = None
            if key is not None:
                if key == previous_key:
                    raise AssertionError(f"Duplicate key in {name}: {key}")
                previous_key = key
        if count != dataset["rows"]:
            raise AssertionError(f"Row-count mismatch in {name}: {count} != {dataset['rows']}")
        counts[name] = count
    print(json.dumps({"status": "ok", "rows": counts}, indent=2))


if __name__ == "__main__":
    main()
