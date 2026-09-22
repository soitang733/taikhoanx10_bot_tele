"""Build a compact, PostgreSQL-ready export from stocks_analysis.sqlite."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE_DB = ROOT / "analysis_data" / "stocks_analysis.sqlite"
OUTPUT_DIR = ROOT / "supabase_bundle"
DATA_DIR = OUTPUT_DIR / "data"
CHUNK_ROWS = 200_000

STATEMENTS = {"balance_sheet": 1, "income_statement": 2, "cash_flow": 3}
ACTION_TYPES = {
    "bonus_shares": 1,
    "capital_increase": 2,
    "cash_dividend": 3,
    "rights_issue": 4,
    "stock_dividend": 5,
}


def clean(value):
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str) and value.strip().lower() in {"", "nan", "none", "nat"}:
        return None
    return value


def number(value):
    value = clean(value)
    if value is None:
        return None
    try:
        result = float(str(value).replace(",", ""))
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def integer(value):
    value = number(value)
    return None if value is None else int(round(value))


def date_only(value):
    value = clean(value)
    return None if value is None else str(value)[:10]


def bytea_hex(value):
    value = clean(value)
    return None if value is None else "\\x" + str(value)


class ChunkWriter:
    def __init__(self, table: str, columns: list[str], chunk_rows: int = CHUNK_ROWS):
        self.table = table
        self.columns = columns
        self.chunk_rows = chunk_rows
        self.part = 0
        self.rows = 0
        self.part_rows = 0
        self.handle = None
        self.writer = None
        self.files: list[dict[str, object]] = []

    def _open(self):
        self.part += 1
        path = DATA_DIR / f"{self.table}_{self.part:03d}.csv.gz"
        self.handle = gzip.open(path, "wt", encoding="utf-8", newline="", compresslevel=6)
        self.writer = csv.writer(self.handle, lineterminator="\n")
        self.writer.writerow(self.columns)
        self.part_rows = 0
        self.files.append({"path": str(path.relative_to(OUTPUT_DIR)).replace("\\", "/"), "rows": 0})

    def write(self, row):
        if self.handle is None or self.part_rows >= self.chunk_rows:
            if self.handle is not None:
                self.handle.close()
            self._open()
        values = [clean(value) for value in row]
        self.writer.writerow(["" if value is None else value for value in values])
        self.rows += 1
        self.part_rows += 1
        self.files[-1]["rows"] = self.part_rows

    def close(self):
        if self.handle is not None:
            self.handle.close()
        return {"table": self.table, "columns": self.columns, "rows": self.rows, "files": self.files}


def write_rows(table: str, columns: list[str], rows, chunk_rows: int = CHUNK_ROWS):
    writer = ChunkWriter(table, columns, chunk_rows)
    for row in rows:
        writer.write(row)
    return writer.close()


def main():
    if not SOURCE_DB.exists():
        raise FileNotFoundError(SOURCE_DB)
    if DATA_DIR.resolve() != (ROOT.resolve() / "supabase_bundle" / "data"):
        raise RuntimeError("Refusing to replace an unexpected bundle data directory")
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    companies = list(db.execute("select * from companies order by ticker"))
    company_ids = {row["ticker"]: index for index, row in enumerate(companies, start=1)}
    source_names = [row[0] for row in db.execute("""
        select source from price_daily union select source from benchmark_daily
        union select source from financial_annual union select source from financial_quarterly
        union select source from corporate_actions order by 1
    """) if row[0]]
    source_ids = {name: index for index, name in enumerate(source_names, start=1)}

    datasets = []
    datasets.append(write_rows("data_sources", ["id", "name"], ((i, name) for name, i in source_ids.items()), 10_000))
    datasets.append(write_rows(
        "companies",
        ["id", "ticker", "company_name", "exchange", "sector", "industry", "listing_date", "shares_outstanding", "market_cap", "trading_status"],
        ((
            company_ids[row["ticker"]], row["ticker"], row["company_name"], row["exchange"], row["sector"], row["industry"],
            date_only(row["listing_date"]), integer(row["shares_outstanding"]), number(row["market_cap"]), row["trading_status"],
        ) for row in companies),
        10_000,
    ))

    price_writer = ChunkWriter(
        "price_daily",
        ["company_id", "date", "open", "high", "low", "close", "adjusted_close", "volume", "source_id", "is_valid", "fetched_at", "data_version", "row_checksum", "dnse_checksum"],
    )
    for row in db.execute("""
        select ticker,date,open,high,low,close,adjusted_close,volume,source,analysis_ready,
               fetched_at,data_version,row_checksum,dnse_checksum
        from price_daily order by ticker,date
    """):
        price_writer.write((
            company_ids[row["ticker"]], date_only(row["date"]), row["open"], row["high"], row["low"], row["close"],
            row["adjusted_close"], integer(row["volume"]), source_ids.get(row["source"]), bool(row["analysis_ready"]),
            row["fetched_at"], integer(row["data_version"]), bytea_hex(row["row_checksum"]), bytea_hex(row["dnse_checksum"]),
        ))
    datasets.append(price_writer.close())

    datasets.append(write_rows(
        "benchmark_daily",
        ["date", "open", "high", "low", "close", "adjusted_close", "volume", "source_id", "is_valid", "fetched_at", "data_version", "row_checksum", "dnse_checksum"],
        ((
            date_only(row["date"]), row["open"], row["high"], row["low"], row["close"], row["adjusted_close"],
            integer(row["volume"]), source_ids.get(row["source"]), bool(row["analysis_ready"]),
            row["fetched_at"], integer(row["data_version"]), bytea_hex(row["row_checksum"]), bytea_hex(row["dnse_checksum"]),
        ) for row in db.execute("select * from benchmark_daily order by date")),
        10_000,
    ))

    revision_writer = ChunkWriter(
        "price_revisions",
        [
            "sync_run_id", "company_id", "date", "detected_at", "reason", "change_type",
            "old_open", "old_high", "old_low", "old_close", "old_volume", "old_source_id", "old_checksum", "old_dnse_checksum",
            "new_open", "new_high", "new_low", "new_close", "new_volume", "new_source_id", "new_checksum", "new_dnse_checksum",
        ],
        50_000,
    )
    for row in db.execute("select * from price_revisions order by detected_at,ticker,date"):
        if row["ticker"] not in company_ids:
            continue
        revision_writer.write((
            row["sync_run_id"], company_ids[row["ticker"]], date_only(row["date"]), row["detected_at"],
            row["reason"], row["change_type"], row["old_open"], row["old_high"], row["old_low"],
            row["old_close"], integer(row["old_volume"]), source_ids.get(row["old_source"]), bytea_hex(row["old_checksum"]), bytea_hex(row["old_dnse_checksum"]),
            row["new_open"], row["new_high"], row["new_low"], row["new_close"], integer(row["new_volume"]),
            source_ids.get(row["new_source"]), bytea_hex(row["new_checksum"]), bytea_hex(row["new_dnse_checksum"]),
        ))
    revision_dataset = revision_writer.close()

    sync_columns = [
        "sync_run_id", "started_at", "finished_at", "status", "requested_tickers", "updated_tickers",
        "failed_tickers", "history_audit_tickers", "action_audit_tickers", "full_refetch_tickers", "revision_rows",
    ]
    datasets.append(write_rows(
        "sync_runs",
        sync_columns,
        ((row[column] for column in sync_columns) for row in db.execute("select * from sync_runs order by started_at")),
        10_000,
    ))
    datasets.append(revision_dataset)

    item_rows = list(db.execute("""
        select item_code, max(item_name) as item_name, max(unit) as unit
        from (
          select item_code,item_name,unit from financial_annual
          union all
          select item_code,item_name,unit from financial_quarterly
        ) where item_code is not null group by item_code order by item_code
    """))
    item_ids = {row["item_code"]: index for index, row in enumerate(item_rows, start=1)}
    datasets.append(write_rows(
        "financial_items", ["id", "code", "name", "unit"],
        ((item_ids[row["item_code"]], row["item_code"], row["item_name"], row["unit"]) for row in item_rows), 10_000,
    ))

    financial_writer = ChunkWriter(
        "financial_values",
        ["company_id", "period_kind", "period_end", "fiscal_year", "fiscal_quarter", "statement_id", "item_id", "value", "source_id"],
        100_000,
    )
    for row in db.execute("""
        select ticker,1 as period_kind,period_end,fiscal_year,fiscal_quarter,statement,item_code,value,source
        from financial_annual where row_valid=1
        union all
        select ticker,2 as period_kind,period_end,fiscal_year,fiscal_quarter,statement,item_code,value,source
        from financial_quarterly where row_valid=1
        order by ticker,period_kind,period_end,statement,item_code
    """):
        financial_writer.write((
            company_ids[row["ticker"]], row["period_kind"], date_only(row["period_end"]), row["fiscal_year"],
            integer(row["fiscal_quarter"]), STATEMENTS[row["statement"]], item_ids[row["item_code"]], row["value"], source_ids.get(row["source"]),
        ))
    datasets.append(financial_writer.close())

    datasets.append(write_rows(
        "action_types", ["id", "name"], ((value, key) for key, value in ACTION_TYPES.items()), 10_000,
    ))
    datasets.append(write_rows(
        "corporate_actions",
        ["id", "company_id", "ex_date", "action_type_id", "cash_dividend", "stock_ratio", "split_ratio", "rights_ratio", "rights_price", "source_id"],
        ((
            index, company_ids[row["ticker"]], date_only(row["ex_date"]), ACTION_TYPES[row["action_type"]],
            number(row["cash_dividend"]), number(row["stock_ratio"]), number(row["split_ratio"]),
            number(row["rights_ratio"]), number(row["rights_price"]), source_ids.get(row["source"]),
        ) for index, row in enumerate(db.execute("select * from corporate_actions where row_valid=1 order by ticker,ex_date"), start=1)),
        50_000,
    ))

    snapshot_columns = [
        "company_id", "as_of_utc", "company_type", "status", "market_share", "total_assets", "eps_ttm", "pe", "ps", "pb",
        "beta", "profit_growth_qoq", "roe_ttm", "roa_ttm", "gross_margin_ttm", "debt_equity_ratio", "inventory_growth_qoq",
        "free_float_ratio", "dividend_yield", "book_value_per_share", "revenue_ttm", "net_income_ttm", "market_cap",
    ]
    numeric_snapshot = set(snapshot_columns[4:])
    snapshot_rows = []
    for row in db.execute("select * from financial_snapshot order by ticker"):
        values = []
        for column in snapshot_columns:
            if column == "company_id":
                values.append(company_ids[row["ticker"]])
            elif column == "as_of_utc":
                values.append(clean(row[column]))
            elif column in numeric_snapshot:
                values.append(number(row[column]))
            else:
                values.append(clean(row[column]))
        snapshot_rows.append(values)
    datasets.append(write_rows("financial_snapshot", snapshot_columns, snapshot_rows, 10_000))
    db.close()

    for dataset in datasets:
        for part in dataset["files"]:
            path = OUTPUT_DIR / part["path"]
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            part["bytes"] = path.stat().st_size
            part["sha256"] = digest.hexdigest()

    source_digest = hashlib.sha256()
    with SOURCE_DB.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            source_digest.update(block)
    manifest = {
        "bundle_version": "1.2.0",
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "source_database": str(SOURCE_DB),
        "source_bytes": SOURCE_DB.stat().st_size,
        "source_sha256": source_digest.hexdigest(),
        "load_order": [item["table"] for item in datasets],
        "datasets": datasets,
        "design": {
            "scope": "HOSE and HNX",
            "price_return_columns": "Omitted; derive from adjusted_close and VNINDEX calendar when needed.",
            "redundant_tables_omitted": ["financial_annual_wide", "financial_quarterly_wide", "coverage", "data_quality"],
        },
    }
    (OUTPUT_DIR / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(path.stat().st_size for path in DATA_DIR.iterdir())
    print(json.dumps({"data_bytes": total, "datasets": {d["table"]: d["rows"] for d in datasets}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
