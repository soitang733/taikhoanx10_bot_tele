"""Read-only SQLite fallback when the localhost stock API is unavailable."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from statistics import median
from typing import Any


DATA_DIR = Path(__file__).resolve().parent / "analysis_data"
MAIN_DB = DATA_DIR / "stocks_analysis.sqlite"
SIGNALS_DB = DATA_DIR / "signals.sqlite"


def rows(database: Path, sql: str, values: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    if not database.is_file():
        raise RuntimeError("Kho dữ liệu cục bộ chưa sẵn sàng")
    uri = f"file:{database.as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute(sql, values).fetchall()]


def _ticker(params: dict[str, Any]) -> str:
    ticker = str(params.get("ticker") or "").upper().strip()
    if not ticker.isascii() or not ticker.isalnum() or not 1 <= len(ticker) <= 10:
        raise ValueError("Mã cổ phiếu không hợp lệ")
    return ticker


def _limit(params: dict[str, Any], default: int) -> int:
    return max(1, min(500, int(params.get("limit") or default)))


def get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = params or {}
    if path == "/health":
        state_path = DATA_DIR / "pipeline_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
        counts = rows(MAIN_DB, "SELECT (SELECT COUNT(*) FROM companies) companies, "
                      "(SELECT COUNT(*) FROM price_daily) price_rows, "
                      "(SELECT MAX(date) FROM price_daily WHERE analysis_ready=1) latest_market_date")[0]
        finished = state.get("finished_at_utc") or state.get("started_at_utc")
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(str(finished).replace("Z", "+00:00"))).total_seconds() / 3600
        except (TypeError, ValueError):
            age = None
        pipeline_ok = state.get("status") in {"success", "degraded"} and not state.get("analysis_deferred_reason")
        fresh = pipeline_ok and age is not None and age <= 72
        return {**counts, "pipeline": state, "pipeline_age_hours": round(age, 2) if age is not None else None,
                "pipeline_ok": pipeline_ok, "data_fresh": fresh,
                "database_ok": True, "healthy": bool(counts["price_rows"])}
    if path == "/v1/universe":
        return {"data": rows(MAIN_DB, "SELECT * FROM companies ORDER BY ticker")}
    if path == "/v1/benchmark":
        data = rows(MAIN_DB, "SELECT ticker,date,open,high,low,close,adjusted_close,volume,return_1d,source "
                    "FROM benchmark_daily WHERE analysis_ready=1 ORDER BY date DESC LIMIT 1")
        return {"data": data[0] if data else None}
    if path == "/v1/rankings":
        action = str(params.get("action") or "").upper()
        if action and action not in {"BUY", "SCREEN_BUY", "WATCH", "HOLD", "EXIT", "DATA_REVIEW"}:
            raise ValueError("Bộ lọc xếp hạng không hợp lệ")
        limit = _limit(params, 10)
        if action == "SCREEN_BUY":
            data = rows(SIGNALS_DB, "SELECT * FROM signals_latest WHERE screen_action='BUY' "
                        "ORDER BY unified_score DESC, selection_strength DESC, fa_score DESC LIMIT ?",
                        (limit,))
        elif action:
            data = rows(SIGNALS_DB, "SELECT * FROM signals_latest WHERE final_action=? "
                        "ORDER BY unified_score DESC, selection_strength DESC, fa_score DESC LIMIT ?",
                        (action, limit))
        else:
            data = rows(SIGNALS_DB, "SELECT * FROM signals_latest ORDER BY "
                        "CASE final_action WHEN 'BUY' THEN 1 WHEN 'HOLD' THEN 2 "
                        "WHEN 'WATCH' THEN 3 WHEN 'DATA_REVIEW' THEN 4 ELSE 5 END, "
                        "unified_score DESC, selection_strength DESC, fa_score DESC LIMIT ?", (limit,))
        return {"data": data, "count": len(data)}
    if path == "/v1/model-portfolio":
        runs = rows(SIGNALS_DB, "SELECT * FROM model_runs ORDER BY signal_date DESC LIMIT 1")
        positions = rows(
            SIGNALS_DB,
            "SELECT p.*,s.final_action,s.close,s.unified_score,s.exit_reason_code,s.decision_reason "
            "FROM model_positions p LEFT JOIN signals_latest s ON s.ticker=p.ticker "
            "ORDER BY s.unified_score DESC,p.ticker",
        )
        pending = rows(
            SIGNALS_DB,
            "SELECT o.*,s.close,s.unified_score,s.exit_reason_code,s.decision_reason "
            "FROM model_pending_orders o LEFT JOIN signals_latest s ON s.ticker=o.ticker "
            "ORDER BY o.side,o.ticker",
        )
        return {"data": {"run": runs[0] if runs else None,
                         "positions": positions, "pending_orders": pending}}
    if path == "/v1/industry-valuation":
        ticker = _ticker(params)
        companies = rows(MAIN_DB, "SELECT ticker,industry,sector FROM companies WHERE ticker=? LIMIT 1",
                         (ticker,))
        if not companies:
            raise LookupError("Không tìm thấy mã trong kho dữ liệu")
        company = companies[0]

        def peer_rows(field: str, classification: str) -> list[dict[str, Any]]:
            return rows(
                MAIN_DB,
                f"SELECT c.ticker,s.pe,s.market_cap,s.net_income_ttm FROM companies c "
                "JOIN financial_snapshot s ON s.ticker=c.ticker "
                f"WHERE c.{field}=? AND s.as_of_utc=(SELECT MAX(x.as_of_utc) "
                "FROM financial_snapshot x WHERE x.ticker=s.ticker)",
                (classification,),
            )

        field = "sector" if company.get("sector") else "industry"
        classification = str(company.get(field) or "")
        peers = peer_rows(field, classification) if classification else []
        if len(peers) < 5 and company.get("industry") and field != "industry":
            field, classification = "industry", str(company.get("industry") or "")
            peers = peer_rows(field, classification)
        pe_values = []
        for peer in peers:
            try:
                direct = float(peer["pe"]) if peer.get("pe") is not None else None
                derived = (float(peer["market_cap"]) / float(peer["net_income_ttm"])
                           if float(peer.get("market_cap") or 0) > 0
                           and float(peer.get("net_income_ttm") or 0) > 0 else None)
                value = direct if direct is not None and direct > 0 else derived
                if value is not None and 0 < value <= 200:
                    pe_values.append(value)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
        return {"data": {
            "ticker": ticker, "classification_field": field, "classification": classification,
            "peer_count": len(pe_values), "median_pe_ttm": median(pe_values) if pe_values else None,
            "method": "median of positive peer P/E; provider P/E, otherwise market_cap/net_income_ttm; values above 200 excluded",
            "display_only_not_used_in_fa_signal_or_backtest": True,
        }}
    ticker = _ticker(params)
    if path == "/v1/signal":
        data = rows(SIGNALS_DB, "SELECT * FROM signals_latest WHERE ticker=?", (ticker,))
        if not data:
            raise LookupError("Không tìm thấy mã trong kho dữ liệu")
        return {"data": data[0]}
    if path == "/v1/latest":
        data = rows(MAIN_DB, "SELECT * FROM price_daily WHERE ticker=? AND analysis_ready=1 "
                    "ORDER BY date DESC LIMIT 1", (ticker,))
        return {"data": data[0] if data else None}
    if path == "/v1/snapshot":
        data = rows(MAIN_DB, "SELECT * FROM financial_snapshot WHERE ticker=? ORDER BY as_of_utc DESC LIMIT 1",
                    (ticker,))
        return {"data": data[0] if data else None}
    if path == "/v1/financials":
        period = str(params.get("period") or "annual").lower()
        if period not in {"quarterly", "annual"}:
            raise ValueError("Kỳ báo cáo không hợp lệ")
        table = "financial_quarterly" if period == "quarterly" else "financial_annual"
        data = rows(MAIN_DB, f"SELECT * FROM {table} WHERE ticker=? AND row_valid=1 "
                    "ORDER BY period_end DESC, statement, item_code LIMIT ?", (ticker, _limit(params, 100)))
        return {"data": data, "count": len(data)}
    if path == "/v1/actions":
        data = rows(MAIN_DB, "SELECT ticker,ex_date,action_type,cash_dividend,stock_ratio,split_ratio,"
                    "rights_ratio,rights_price,source FROM corporate_actions WHERE ticker=? AND row_valid=1 "
                    "ORDER BY ex_date DESC LIMIT ?", (ticker, _limit(params, 3)))
        return {"data": data, "count": len(data)}
    raise ValueError("Endpoint không hỗ trợ trong chế độ dữ liệu cục bộ")
