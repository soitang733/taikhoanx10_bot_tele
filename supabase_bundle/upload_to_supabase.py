"""Upload this bundle with PostgreSQL COPY using SUPABASE_DB_URL."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
from pathlib import Path

import psycopg
from psycopg import sql


ROOT = Path(__file__).resolve().parent


def run_sql(connection, filename: str):
    statement = (ROOT / filename).read_text(encoding="utf-8")
    with connection.cursor() as cursor:
        cursor.execute(statement)
    connection.commit()


def main():
    database_url = os.environ.get("SUPABASE_DB_URL", "").strip()
    if not database_url:
        raise SystemExit("Missing SUPABASE_DB_URL in the environment")
    manifest_bytes = (ROOT / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
    source = ROOT.parent / "analysis_data" / "stocks_analysis.sqlite"
    if source.exists() and manifest.get("source_sha256"):
        with source.open("rb") as stream:
            current_sha = hashlib.file_digest(stream, "sha256").hexdigest()
        if current_sha != manifest["source_sha256"]:
            raise RuntimeError("Bundle is stale relative to local SQLite; regenerate it before upload")
    from validate_bundle import main as validate_bundle
    validate_bundle()

    with psycopg.connect(database_url, autocommit=False) as connection:
        with connection.cursor() as cursor:
            cursor.execute("set statement_timeout = 0")
        run_sql(connection, "schema.sql")

        with connection.cursor() as cursor:
            cursor.execute("select manifest_sha256 from public.bundle_imports")
            imported = {row[0] for row in cursor.fetchall()}
            if imported and manifest_sha not in imported:
                raise RuntimeError("Target contains a different bundle; incremental migration is required")
            existing_counts = {}
            for dataset in manifest["datasets"]:
                cursor.execute(sql.SQL("select count(*) from public.{}").format(sql.Identifier(dataset["table"])))
                existing_counts[dataset["table"]] = cursor.fetchone()[0]
        if manifest_sha in imported:
            mismatches = {dataset["table"]: existing_counts[dataset["table"]]
                          for dataset in manifest["datasets"]
                          if existing_counts[dataset["table"]] != dataset["rows"]}
            if mismatches:
                raise RuntimeError(f"Imported bundle row counts changed: {mismatches}")
            print("Identical bundle already imported; no rows written")
            return
        if any(existing_counts.values()):
            raise RuntimeError("Target contains data without this bundle's manifest marker; refusing count-only skip")

        for dataset in manifest["datasets"]:
            table = dataset["table"]
            columns = dataset["columns"]
            with connection.cursor() as cursor:
                cursor.execute(sql.SQL("select count(*) from public.{}").format(sql.Identifier(table)))
                existing = cursor.fetchone()[0]
            if existing:
                raise RuntimeError(
                    f"Target table public.{table} is partially loaded ({existing}/{dataset['rows']}); "
                    "clear that table before retrying"
                )
            copy_statement = sql.SQL("copy public.{} ({}) from stdin with (format csv, header true)").format(
                sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, columns))
            )
            loaded = 0
            for part in dataset["files"]:
                path = ROOT / part["path"]
                with connection.cursor() as cursor, cursor.copy(copy_statement) as copy:
                    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
                        while chunk := handle.read(1024 * 1024):
                            copy.write(chunk)
                loaded += part["rows"]
                print(f"{table}: {loaded}/{dataset['rows']}")
        # Keep all data and the import marker in one transaction. A failed COPY
        # can be rolled back and retried against the still-empty schema.
        with connection.cursor() as cursor:
            cursor.execute((ROOT / "post_import.sql").read_text(encoding="utf-8"))
        with connection.cursor() as cursor:
            for dataset in manifest["datasets"]:
                cursor.execute(sql.SQL("select count(*) from public.{}").format(sql.Identifier(dataset["table"])))
                actual = cursor.fetchone()[0]
                if actual != dataset["rows"]:
                    raise RuntimeError(f"Row-count mismatch for {dataset['table']}: {actual} != {dataset['rows']}")
            cursor.execute("select pg_database_size(current_database())")
            database_bytes = cursor.fetchone()[0]
            cursor.execute("insert into public.bundle_imports(manifest_sha256,source_sha256) values (%s,%s)",
                           (manifest_sha, manifest["source_sha256"]))
        connection.commit()
        print(json.dumps({"status": "ok", "database_bytes": database_bytes}, indent=2))
        if database_bytes > 450_000_000:
            print("WARNING: database is above the 450 MB safety threshold for Supabase Free")


if __name__ == "__main__":
    main()
