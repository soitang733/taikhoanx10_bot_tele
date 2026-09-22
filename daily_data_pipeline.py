"""Incrementally refresh market data and rebuild the bot-facing data layer."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import pandas as pd

import prepare_analysis_data
import backtest_engine
import dnse_financial_snapshot
import strategy_engine
import vn_stock_scraper_complete as scraper


LOGGER = logging.getLogger("daily_data_pipeline")
SOURCE_PRIORITY = {"DNSE": 0, "vnstock KBS": 10, "vnstock VCI": 20}
REVISION_COLUMNS = [
    "sync_run_id", "ticker", "date", "detected_at", "reason", "change_type",
    "old_open", "old_high", "old_low", "old_close", "old_volume", "old_source", "old_checksum", "old_dnse_checksum",
    "new_open", "new_high", "new_low", "new_close", "new_volume", "new_source", "new_checksum", "new_dnse_checksum",
]
RAW_BUILD_REQUIRED_FILES = ("metadata.json", "ta_daily.csv", "diagnostics.json")


def configure_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_path, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    LOGGER.addHandler(handler)
    LOGGER.addHandler(logging.StreamHandler(sys.stdout))


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
    os.replace(temp, path)


@contextmanager
def tracked_pipeline_run(lock_path: Path, state_path: Path, initial_state: dict[str, Any]) -> Iterator[None]:
    """Persist a terminal failure state if a locked pipeline run raises."""
    with pipeline_lock(lock_path):
        atomic_json(state_path, initial_state)
        try:
            yield
        except BaseException as exc:
            failed_state = {
                **initial_state,
                "status": "failed",
                "finished_at_utc": datetime.now(timezone.utc).isoformat(),
                "error": f"{type(exc).__name__}: {exc}",
            }
            atomic_json(state_path, scraper.json_safe(failed_state))
            raise


def atomic_csv(path: Path, frame: pd.DataFrame) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    frame.to_csv(temp, index=False, encoding="utf-8-sig")
    os.replace(temp, path)


def append_csv_atomic(path: Path, rows: pd.DataFrame, columns: list[str] | None = None) -> None:
    if rows.empty:
        return
    previous = pd.read_csv(path, encoding="utf-8-sig", low_memory=False) if path.exists() else pd.DataFrame()
    combined = pd.concat([previous, rows], ignore_index=True)
    if columns:
        combined = combined.reindex(columns=columns)
    atomic_csv(path, combined)


def _number_token(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if not np.isfinite(number):
        return ""
    return format(number, ".8f").rstrip("0").rstrip(".") or "0"


def price_checksum(row: pd.Series) -> str:
    values = [str(row.get("ticker", "")).strip().upper(), str(row.get("date", ""))[:10]]
    values.extend(_number_token(row.get(column)) for column in ("open", "high", "low", "close", "volume"))
    return hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()


def ensure_lineage(frame: pd.DataFrame, default_fetched_at: str) -> pd.DataFrame:
    result = scraper.ensure_columns(frame, scraper.TA_COLUMNS).copy()
    if result.empty:
        return result
    result["ticker"] = result["ticker"].astype("string").str.upper().str.strip()
    result["date"] = pd.to_datetime(result["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    # New empty columns arrive as float64/NaN from pandas. Convert first so
    # assigning SHA-256 strings remains valid on current and future pandas.
    result["row_checksum"] = result["row_checksum"].astype("string")
    missing_checksum = result["row_checksum"].isna() | result["row_checksum"].str.strip().eq("")
    result.loc[missing_checksum, "row_checksum"] = result.loc[missing_checksum].apply(price_checksum, axis=1)
    result["dnse_checksum"] = result["dnse_checksum"].astype("string")
    missing_dnse = result["dnse_checksum"].isna() | result["dnse_checksum"].str.strip().eq("")
    dnse_rows = result["source"].astype("string").eq("DNSE").fillna(False)
    result.loc[missing_dnse & dnse_rows, "dnse_checksum"] = result.loc[missing_dnse & dnse_rows, "row_checksum"]
    result["fetched_at"] = result["fetched_at"].fillna(default_fetched_at)
    result["data_version"] = pd.to_numeric(result["data_version"], errors="coerce").fillna(1).astype("Int64")
    return result


def action_fingerprint(frame: pd.DataFrame) -> str:
    normalized = scraper.ensure_columns(frame, scraper.CA_COLUMNS).copy()
    if normalized.empty:
        return hashlib.sha256(b"").hexdigest()
    normalized = normalized.fillna("").astype(str).sort_values(scraper.CA_COLUMNS)
    payload = normalized.to_csv(index=False, lineterminator="\n")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def rotating_audit_set(symbols: list[str], size: int, day: datetime) -> set[str]:
    ordered = sorted(set(symbols))
    if size <= 0 or not ordered:
        return set()
    size = min(size, len(ordered))
    start = day.toordinal() * size % len(ordered)
    return {ordered[(start + offset) % len(ordered)] for offset in range(size)}


def revision_rows(
    existing: pd.DataFrame,
    incoming: pd.DataFrame,
    sync_run_id: str,
    detected_at: str,
    reason: str,
    include_removed: bool = False,
) -> pd.DataFrame:
    old = ensure_lineage(existing, detected_at).set_index("date", drop=False)
    new = ensure_lineage(incoming, detected_at).set_index("date", drop=False)
    rows: list[dict[str, Any]] = []
    dates = set(old.index).intersection(new.index)
    if include_removed:
        dates |= set(old.index).difference(new.index)
    for date in sorted(dates):
        old_row = old.loc[date]
        new_row = new.loc[date] if date in new.index else None
        old_checksum = str(old_row.get("row_checksum") or "")
        new_checksum = str(new_row.get("row_checksum") or "") if new_row is not None else ""
        old_dnse = str(old_row.get("dnse_checksum") or "") if pd.notna(old_row.get("dnse_checksum")) else ""
        new_dnse = str(new_row.get("dnse_checksum") or "") if new_row is not None and pd.notna(new_row.get("dnse_checksum")) else ""
        if old_checksum == new_checksum and old_dnse == new_dnse:
            continue
        row: dict[str, Any] = {
            "sync_run_id": sync_run_id, "ticker": old_row.get("ticker"), "date": date,
            "detected_at": detected_at, "reason": reason,
            "change_type": "deleted" if new_row is None else "updated",
            "old_source": old_row.get("source"), "old_checksum": old_checksum,
            "old_dnse_checksum": old_dnse or None,
            "new_source": new_row.get("source") if new_row is not None else None,
            "new_checksum": new_checksum or None,
            "new_dnse_checksum": new_dnse or None,
        }
        for column in ("open", "high", "low", "close", "volume"):
            row[f"old_{column}"] = old_row.get(column)
            row[f"new_{column}"] = new_row.get(column) if new_row is not None else None
        rows.append(row)
    return pd.DataFrame(rows, columns=REVISION_COLUMNS)


def dnse_overlap_revisions(existing: pd.DataFrame, incoming: pd.DataFrame,
                           sync_run_id: str, detected_at: str) -> pd.DataFrame:
    """Only DNSE-to-DNSE changes can establish a DNSE source restatement.

    A canonical fallback change is still recorded by revision_rows(), but must
    not be interpreted as proof that DNSE has changed its historical candles.
    """
    revisions = revision_rows(existing, incoming, sync_run_id, detected_at, "dnse_overlap_changed")
    if revisions.empty:
        return revisions
    return revisions.loc[
        revisions["old_dnse_checksum"].notna()
        & revisions["new_dnse_checksum"].notna()
        & revisions["old_dnse_checksum"].ne(revisions["new_dnse_checksum"])
    ].reset_index(drop=True)


def apply_lineage(incoming: pd.DataFrame, existing: pd.DataFrame, fetched_at: str) -> pd.DataFrame:
    old = ensure_lineage(existing, fetched_at).set_index("date", drop=False)
    new = ensure_lineage(incoming, fetched_at)
    for index, row in new.iterrows():
        date = row["date"]
        checksum = row["row_checksum"]
        same_canonical = date in old.index and str(old.loc[date, "row_checksum"]) == str(checksum)
        same_dnse = date in old.index and str(old.loc[date, "dnse_checksum"]) == str(row["dnse_checksum"])
        if same_canonical and same_dnse:
            new.at[index, "fetched_at"] = old.loc[date, "fetched_at"]
            new.at[index, "data_version"] = old.loc[date, "data_version"]
        elif date in old.index:
            new.at[index, "fetched_at"] = fetched_at
            new.at[index, "data_version"] = int(old.loc[date, "data_version"]) + 1
        else:
            new.at[index, "fetched_at"] = fetched_at
            new.at[index, "data_version"] = 1
    return scraper.ensure_columns(new, scraper.TA_COLUMNS)


@contextmanager
def pipeline_lock(lock_path: Path, stale_hours: int = 8) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        age = time.time() - lock_path.stat().st_mtime
        lock_text = lock_path.read_text(encoding="utf-8", errors="ignore")
        match = re.search(r"\bpid=(\d+)\b", lock_text)
        pid = int(match.group(1)) if match else None
        process_alive = False
        if pid:
            try:
                os.kill(pid, 0)
                process_alive = True
            except OSError:
                process_alive = False
        # A live process owns the lock regardless of age. A legacy lock without a
        # PID remains protected until its stale timeout. Dead-PID locks are removed.
        if process_alive or (pid is None and age < stale_hours * 3600):
            raise RuntimeError(f"Another daily update is active: {lock_path}")
        lock_path.unlink()
    descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(descriptor, f"pid={os.getpid()} started={datetime.now(timezone.utc).isoformat()}".encode())
        os.close(descriptor)
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def read_existing_prices(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return scraper.empty_frame(scraper.TA_COLUMNS)
    frame = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
    return scraper.ensure_columns(frame, scraper.TA_COLUMNS)


def priority(value: object) -> int:
    text = str(value or "")
    for source, rank in SOURCE_PRIORITY.items():
        if text.casefold().startswith(source.casefold()):
            return rank
    return 99


def merge_prices(existing: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    old = scraper.ensure_columns(existing, scraper.TA_COLUMNS).copy()
    new = scraper.ensure_columns(incoming, scraper.TA_COLUMNS).copy()
    old["_new"] = False
    new["_new"] = True
    merged = pd.concat([old, new], ignore_index=True)
    merged["ticker"] = merged["ticker"].astype("string").str.upper().str.strip()
    merged["date"] = pd.to_datetime(merged["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    merged["_source_priority"] = merged["source"].map(priority)
    merged = (
        merged.dropna(subset=["ticker", "date", "close"])
        # A freshly reconciled KBS/VCI row must be allowed to replace an older
        # DNSE row that failed validation. Source priority only breaks ties
        # within the same refresh generation.
        .sort_values(["ticker", "date", "_new", "_source_priority"], ascending=[True, True, False, True])
        .drop_duplicates(["ticker", "date"], keep="first")
        .sort_values(["ticker", "date"])
        .drop(columns=["_new", "_source_priority"])
        .reset_index(drop=True)
    )
    return scraper.ensure_columns(merged, scraper.TA_COLUMNS)


def date_window(existing: pd.DataFrame, overlap_days: int, bootstrap_days: int,
                market_dates: pd.DatetimeIndex | None = None) -> tuple[datetime, datetime]:
    end = datetime.now(timezone.utc) + timedelta(days=1)
    existing_dates = pd.to_datetime(existing.get("date"), errors="coerce")
    if existing_dates is not None and existing_dates.notna().any():
        latest = existing_dates.max().to_pydatetime().replace(tzinfo=timezone.utc)
        eligible = market_dates[market_dates <= pd.Timestamp(latest).tz_localize(None)] if market_dates is not None else pd.DatetimeIndex([])
        if len(eligible):
            start = eligible[-min(max(overlap_days, 1), len(eligible))].to_pydatetime().replace(tzinfo=timezone.utc)
        else:
            start = latest - timedelta(days=overlap_days)
    else:
        start = end - timedelta(days=bootstrap_days)
    return start, end


def update_diagnostics(symbol_dir: Path, prices: pd.DataFrame, diagnostics: list[dict[str, Any]]) -> None:
    path = symbol_dir / "diagnostics.json"
    document: dict[str, Any] = {}
    if path.exists():
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            document = {}
    document["ticker"] = symbol_dir.name.upper()
    document["daily_update"] = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source_diagnostics": diagnostics,
        "ta_daily": scraper.ta_stats(prices),
    }
    document.setdefault("statuses", {})["ta_status"] = "OK" if not prices.empty else "MISSING"
    document.setdefault("quality", {})["ta_daily"] = scraper.ta_stats(prices)
    atomic_json(path, scraper.json_safe(document))


def refresh_symbol(
    symbol: str,
    raw_dir: Path,
    overlap_days: int,
    bootstrap_days: int,
    sync_run_id: str,
    history_audit: bool = False,
    action_audit: bool = False,
    market_dates: pd.DatetimeIndex | None = None,
) -> dict[str, Any]:
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    symbol_dir = raw_dir / symbol
    symbol_dir.mkdir(parents=True, exist_ok=True)
    price_path = symbol_dir / "ta_daily.csv"
    existing = ensure_lineage(read_existing_prices(price_path), fetched_at)
    full_refetch_reason: str | None = "scheduled_history_audit" if history_audit else None

    action_path = symbol_dir / "corporate_actions.csv"
    actions = pd.read_csv(action_path, encoding="utf-8-sig") if action_path.exists() else scraper.empty_frame(scraper.CA_COLUMNS)
    actions = scraper.ensure_columns(actions, scraper.CA_COLUMNS)
    actions_reliable = action_path.exists()
    action_diagnostics: list[dict[str, Any]] = []
    if action_audit:
        refreshed_actions, action_diagnostics, reliable = scraper.fetch_corporate_actions(symbol)
        if reliable:
            if action_path.exists() and action_fingerprint(actions) != action_fingerprint(refreshed_actions):
                full_refetch_reason = "corporate_action_changed"
            actions = refreshed_actions
            actions_reliable = True
            atomic_csv(action_path, actions)

    if full_refetch_reason and not existing.empty:
        earliest = pd.to_datetime(existing["date"], errors="coerce").min()
        start = earliest.to_pydatetime().replace(tzinfo=timezone.utc)
        end = datetime.now(timezone.utc) + timedelta(days=1)
    else:
        start, end = date_window(existing, overlap_days, bootstrap_days, market_dates)
    incoming, diagnostics = scraper.fetch_ta_daily_range(symbol, start, end)
    diagnostics.extend(action_diagnostics)
    if incoming.empty:
        detail = "; ".join(f"{item.get('source')}={item.get('status')}" for item in diagnostics)
        raise RuntimeError(f"No price rows returned ({detail})")
    incoming = ensure_lineage(incoming, fetched_at)
    source_changes = dnse_overlap_revisions(existing, incoming, sync_run_id, fetched_at)
    if not source_changes.empty and full_refetch_reason is None and not existing.empty:
        confirmation, confirm_diagnostics = scraper.fetch_ta_daily_range(symbol, start, end)
        diagnostics.extend(confirm_diagnostics)
        confirmed = dnse_overlap_revisions(existing, confirmation, sync_run_id, fetched_at)
        expected = source_changes.set_index("date")["new_dnse_checksum"].to_dict()
        observed = confirmed.set_index("date")["new_dnse_checksum"].to_dict()
        if not expected.items() <= observed.items():
            raise RuntimeError("DNSE overlap change was not reproduced on a second fetch")
        full_refetch_reason = "confirmed_dnse_restatement"
        earliest = pd.to_datetime(existing["date"], errors="coerce").min().to_pydatetime().replace(tzinfo=timezone.utc)
        incoming, full_diagnostics = scraper.fetch_ta_daily_range(symbol, earliest, end)
        diagnostics.extend(full_diagnostics)
        if incoming.empty:
            raise RuntimeError("Full-history reconciliation returned no rows")
        incoming = ensure_lineage(incoming, fetched_at)

    changes = revision_rows(
        existing,
        incoming,
        sync_run_id,
        fetched_at,
        full_refetch_reason or "daily_overlap",
    )
    incoming = apply_lineage(incoming, existing, fetched_at)
    merged = merge_prices(existing, incoming)
    history_complete = actions_reliable and (
        actions.empty or str(actions["ex_date"].min()) <= str(merged["date"].min())
    )
    adjusted, adjustment_status, warnings = scraper.apply_corporate_actions(
        merged,
        actions,
        actions_reliable=actions_reliable,
        history_complete=history_complete,
    )
    diagnostics.extend(
        scraper.diagnostic("PRICE_ADJUSTMENT", "corporate_actions", "WARNING", detail=warning)
        for warning in warnings
    )
    atomic_csv(price_path, adjusted)
    update_diagnostics(symbol_dir, adjusted, diagnostics)
    return {
        "ticker": symbol,
        "rows_before": len(existing),
        "rows_fetched": len(incoming),
        "rows_after": len(adjusted),
        "latest_date": adjusted["date"].max(),
        "source": str(incoming["source"].iloc[0]),
        "adjustment_status": adjustment_status,
        "full_refetch": bool(full_refetch_reason),
        "full_refetch_reason": full_refetch_reason,
        "revision_rows": changes.to_dict("records"),
    }


def refresh_benchmark(raw_dir: Path, overlap_days: int, bootstrap_days: int, sync_run_id: str,
                      market_dates: pd.DatetimeIndex | None = None) -> dict[str, Any]:
    fetched_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    path = raw_dir / "vnindex_daily.csv"
    existing = ensure_lineage(read_existing_prices(path), fetched_at)
    start, end = date_window(existing, overlap_days, bootstrap_days, market_dates)
    incoming, diagnostics = scraper.fetch_ta_daily_range("VNINDEX", start, end)
    if incoming.empty:
        raise RuntimeError("No VNINDEX rows returned")
    incoming = apply_lineage(incoming, existing, fetched_at)
    merged = merge_prices(existing, incoming)
    for adjusted, raw in {
        "adjusted_open": "open",
        "adjusted_high": "high",
        "adjusted_low": "low",
        "adjusted_close": "close",
    }.items():
        merged[adjusted] = merged[raw]
    merged["adjustment_status"] = "SOURCE_ADJUSTED"
    merged["adjustment_source"] = merged["source"]
    atomic_csv(path, merged)
    return {"rows": len(merged), "latest_date": merged["date"].max(), "diagnostics": diagnostics}


def resolve_symbols(raw_dir: Path, explicit: str | None) -> tuple[list[str], list[str]]:
    if explicit:
        requested = sorted({item.strip().upper() for item in explicit.split(",") if item.strip()})
    else:
        requested = scraper.get_all_symbols("ALL")
    ready: list[str] = []
    skipped: list[str] = []
    for symbol in requested:
        symbol_dir = raw_dir / symbol
        if explicit or (symbol_dir / "diagnostics.json").exists():
            ready.append(symbol)
        else:
            skipped.append(symbol)
    return ready, skipped


def analysis_rebuild_deferred_reason(raw_dir: Path, analysis_dir: Path) -> str | None:
    """Protect the canonical DB from a rebuild from an incomplete raw mirror."""
    database_path = analysis_dir / "stocks_analysis.sqlite"
    if not database_path.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
        try:
            expected = int(connection.execute("SELECT COUNT(*) FROM companies").fetchone()[0])
        finally:
            connection.close()
    except (sqlite3.Error, OSError):
        return "could not verify the existing analysis database before rebuild"
    completed = sum(
        all((path / filename).exists() and (path / filename).stat().st_size > 0 for filename in RAW_BUILD_REQUIRED_FILES)
        for path in raw_dir.iterdir()
        if path.is_dir()
    )
    if expected and completed < expected:
        return (
            f"raw mirror incomplete ({completed}/{expected} ticker folders); "
            "analysis rebuild deferred to protect the existing database"
        )
    return None


def run(args: argparse.Namespace) -> dict[str, Any]:
    raw_dir = args.raw_dir.resolve()
    analysis_dir = args.analysis_dir.resolve()
    raw_dir.mkdir(parents=True, exist_ok=True)
    analysis_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(analysis_dir / "daily_pipeline.log")
    state_path = analysis_dir / "pipeline_state.json"
    started_at = datetime.now(timezone.utc)
    sync_run_id = started_at.strftime("sync-%Y%m%dT%H%M%SZ")
    state: dict[str, Any] = {
        "status": "running",
        "started_at_utc": started_at.isoformat(),
        "finished_at_utc": None,
        "sync_run_id": sync_run_id,
        "updated_tickers": 0,
        "failed_tickers": [],
    }

    with tracked_pipeline_run(analysis_dir / "data_update.lock", state_path, state):
        symbols, skipped = resolve_symbols(raw_dir, args.symbols)
        benchmark_raw = read_existing_prices(raw_dir / "vnindex_daily.csv")
        market_dates = pd.DatetimeIndex(
            pd.to_datetime(benchmark_raw["date"], errors="coerce").dropna().unique()
        ).sort_values()
        if args.full_audit:
            history_audits = set(symbols)
            action_audits = set(symbols)
        else:
            history_audits = rotating_audit_set(symbols, args.history_audit_size, started_at)
            action_audits = rotating_audit_set(symbols, args.action_audit_size, started_at + timedelta(days=1))
        LOGGER.info("Daily update: %d ready tickers, %d awaiting bootstrap", len(symbols), len(skipped))
        LOGGER.info("Audit sample: %d price histories, %d corporate-action histories", len(history_audits), len(action_audits))
        results: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            future_map = {
                executor.submit(
                    refresh_symbol,
                    symbol,
                    raw_dir,
                    args.overlap_days,
                    args.bootstrap_days,
                    sync_run_id,
                    symbol in history_audits,
                    symbol in action_audits,
                    market_dates,
                ): symbol
                for symbol in symbols
            }
            for future in as_completed(future_map):
                symbol = future_map[future]
                try:
                    result = future.result()
                    results.append(result)
                    LOGGER.info("[%s] updated through %s via %s", symbol, result["latest_date"], result["source"])
                except Exception as exc:
                    failures.append({"ticker": symbol, "error": str(exc)})
                    LOGGER.exception("[%s] daily update failed", symbol)

        benchmark: dict[str, Any] | None = None
        benchmark_error: str | None = None
        try:
            benchmark = refresh_benchmark(raw_dir, args.overlap_days, args.bootstrap_days, sync_run_id, market_dates)
        except Exception as exc:
            benchmark_error = str(exc)
            LOGGER.exception("VNINDEX update failed")

        all_revisions = pd.DataFrame(
            [row for result in results for row in result.pop("revision_rows", [])],
            columns=REVISION_COLUMNS,
        )
        append_csv_atomic(raw_dir / "price_revisions.csv", all_revisions, REVISION_COLUMNS)

        failure_ratio = len(failures) / len(symbols) if symbols else 1.0
        status = "success" if not failures and not benchmark_error else "degraded"
        if failure_ratio > args.max_failure_ratio:
            status = "failed"
        analysis_deferred_reason = analysis_rebuild_deferred_reason(raw_dir, analysis_dir)
        if analysis_deferred_reason and status == "success":
            status = "degraded"
        finished_data_at = datetime.now(timezone.utc)
        sync_row = pd.DataFrame([{
            "sync_run_id": sync_run_id,
            "started_at": started_at.isoformat(),
            "finished_at": finished_data_at.isoformat(),
            "status": status,
            "requested_tickers": len(symbols),
            "updated_tickers": len(results),
            "failed_tickers": len(failures),
            "history_audit_tickers": len(history_audits),
            "action_audit_tickers": len(action_audits),
            "full_refetch_tickers": sum(bool(item.get("full_refetch")) for item in results),
            "revision_rows": len(all_revisions),
        }])
        append_csv_atomic(raw_dir / "sync_runs.csv", sync_row)

        analysis_manifest = None
        financial_snapshot = None
        financial_snapshot_error = None
        if analysis_deferred_reason:
            LOGGER.warning("%s", analysis_deferred_reason)
        if not args.skip_financial_snapshot and not analysis_deferred_reason:
            try:
                financial_snapshot = dnse_financial_snapshot.refresh(
                    symbols,
                    raw_dir / "financial_snapshot.csv",
                    workers=max(1, args.workers),
                )
            except Exception as exc:
                financial_snapshot_error = str(exc)
                LOGGER.warning("DNSE financial snapshot skipped: %s", exc)
        elif analysis_deferred_reason:
            financial_snapshot_error = analysis_deferred_reason
        if not args.skip_analysis and not analysis_deferred_reason:
            analysis_manifest = prepare_analysis_data.build(raw_dir, analysis_dir)
        elif analysis_deferred_reason:
            analysis_manifest = {"status": "deferred", "reason": analysis_deferred_reason}
        strategy_manifest = None
        backtest_summary = None
        backtest_error = None
        if analysis_manifest is not None and not analysis_deferred_reason:
            strategy_manifest = strategy_engine.build(
                analysis_dir / "stocks_analysis.sqlite",
                analysis_dir,
                Path(__file__).resolve().parent / "strategy_config.json",
            )
            if not getattr(args, "skip_backtest", False):
                try:
                    backtest_report = backtest_engine.run(
                        analysis_dir / "stocks_analysis.sqlite", analysis_dir,
                        compare_components=False,
                    )
                    backtest_summary = {
                        "generated_at_utc": backtest_report.get("generated_at_utc"),
                        "data_latest_date": backtest_report.get("data_latest_date"),
                        "simulation_version": backtest_report.get("simulation_version"),
                    }
                except Exception as exc:
                    backtest_error = str(exc)
                    LOGGER.exception("Backtest refresh failed")
                    if status == "success":
                        status = "degraded"
        finished_at = datetime.now(timezone.utc)
        state = {
            "status": status,
            "started_at_utc": started_at.isoformat(),
            "finished_at_utc": finished_at.isoformat(),
            "duration_seconds": round((finished_at - started_at).total_seconds(), 2),
            "requested_tickers": len(symbols) + len(skipped),
            "updated_tickers": len(results),
            "sync_run_id": sync_run_id,
            "full_refetch_tickers": sum(bool(item.get("full_refetch")) for item in results),
            "revision_rows": len(all_revisions),
            "awaiting_bootstrap": skipped,
            "failed_tickers": failures,
            "benchmark": benchmark,
            "benchmark_error": benchmark_error,
            "latest_market_date": max((item["latest_date"] for item in results), default=None),
            "analysis_manifest": analysis_manifest,
            "analysis_deferred_reason": analysis_deferred_reason,
            "financial_snapshot": financial_snapshot,
            "financial_snapshot_error": financial_snapshot_error,
            "strategy_manifest": strategy_manifest,
            "backtest": backtest_summary,
            "backtest_error": backtest_error,
        }
        atomic_json(state_path, scraper.json_safe(state))
        if status == "failed":
            raise RuntimeError(f"Daily pipeline failed: {len(failures)}/{len(symbols)} tickers failed")
        return state


def main() -> None:
    parser = argparse.ArgumentParser(description="Incremental daily data update for the stock-analysis bot")
    parser.add_argument("--raw-dir", type=Path, default=Path("scraper_output"))
    parser.add_argument("--analysis-dir", type=Path, default=Path("analysis_data"))
    parser.add_argument("--symbols", help="Optional comma-separated subset for testing or recovery")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--overlap-days",
        type=int,
        default=2,
        help="Number of prior VNINDEX trading sessions to overlap (kept as --overlap-days for task compatibility)",
    )
    parser.add_argument("--bootstrap-days", type=int, default=3660)
    parser.add_argument("--history-audit-size", type=int, default=10)
    parser.add_argument("--action-audit-size", type=int, default=20)
    parser.add_argument(
        "--full-audit",
        action="store_true",
        help="Audit full price and corporate-action history for every selected ticker",
    )
    parser.add_argument("--max-failure-ratio", type=float, default=0.20)
    parser.add_argument("--skip-analysis", action="store_true")
    parser.add_argument("--skip-backtest", action="store_true")
    parser.add_argument("--skip-financial-snapshot", action="store_true")
    args = parser.parse_args()
    state = run(args)
    print(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
