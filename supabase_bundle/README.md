# Supabase-ready stock dataset

This bundle is generated from `analysis_data/stocks_analysis.sqlite` and targets the Supabase Free database limit.

## Contents

- `schema.sql`: compact PostgreSQL schema without secondary indexes.
- `data/*.csv.gz`: normalized, chunked data files.
- `post_import.sql`: indexes, API views, read-only RLS policies, grants, and ANALYZE.
- `upload_to_supabase.py`: streamed `COPY` importer with row-count verification.
- `manifest.json`: exact files, columns, and expected row counts.

The source SQLite database is not modified. Redundant wide financial tables, coverage rows, stored returns, repeated units, and estimated trading values are intentionally omitted. API views restore ticker and source names for convenient queries.

This bundle deploys the historical data layer plus private PostgreSQL tables for per-user Paper Trading and persistent AI rate-limit counters. The trusted Python `/v1/...` backend is still required for Telegram `initData` verification, market/AI logic, and all writes. Read `../CLOUD_DEPLOYMENT.md` before publishing the Web App.

Price rows include `fetched_at`, `data_version`, and separate canonical and DNSE-source SHA-256 checksums. `sync_runs` and `price_revisions` preserve synchronization/restatement history. Audit tables and the import marker are not exposed to `anon`/`authenticated`.

## Deploy later

Use the Session pooler connection string on port 5432. Store it locally; never put the database password in source code or chat.

```powershell
$env:SUPABASE_DB_URL='postgresql://postgres.PROJECT:PASSWORD@HOST:5432/postgres?sslmode=require'
& 'D:\dtata\.venv\Scripts\python.exe' -m pip install -r supabase_bundle\requirements.txt
& 'D:\dtata\.venv\Scripts\python.exe' supabase_bundle\upload_to_supabase.py
```

The importer verifies the local SQLite hash and every bundle file before upload. It commits all data and the manifest marker together. An identical manifest may be rerun; an existing database with different data or no marker is rejected instead of being accepted by row count alone. Regenerate the bundle after any local SQLite change. Writes remain restricted to the database/service role; `anon` and `authenticated` receive read-only access under RLS.

If the Data API is configured for explicit table exposure, enable the generated public tables/views under **Project → Integrations → Data API** after import.

The Supabase Free plan currently allows a 500 MB database. The compressed CSV size is not the PostgreSQL database size; check the real database size after import because table and index overhead count toward the quota.
