"""Small read-only HTTP API for the local stock-analysis database."""

from __future__ import annotations

import argparse
from contextlib import closing
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sqlite3
from statistics import median
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from app_security import (AuthenticationError, RateLimitExceeded, TelegramIdentity,
                          configured_rate_limiter, validate_telegram_init_data)
from paper_trading import PaperTradingError, PaperTradingStore, PostgresPaperTradingStore


LOGGER = logging.getLogger("stock_data_api")

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


class StockDataHandler(BaseHTTPRequestHandler):
    database_path: Path
    signals_database_path: Path
    analysis_dir: Path
    paper_database_path: Path
    max_limit = 5_000
    _diagnostics_cache_key: tuple[Any, ...] | None = None
    _diagnostics_cache: dict[str, Any] | None = None
    telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_init_data_max_age = int(os.getenv("TELEGRAM_INIT_DATA_MAX_AGE_SECONDS", "86400"))
    telegram_public_access = os.getenv("TELEGRAM_PUBLIC_ACCESS", "false").strip().lower() in {
        "1", "true", "yes", "on",
    }
    telegram_allowed_user_ids = {
        int(value.strip()) for value in os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",")
        if value.strip().lstrip("-").isdigit()
    }
    paper_database_url = (os.getenv("PAPER_DATABASE_URL") or os.getenv("SUPABASE_DB_URL") or "").strip()
    rate_limiter = configured_rate_limiter()
    ai_minute_limit = int(os.getenv("AI_RATE_LIMIT_PER_MINUTE", "5"))
    ai_daily_limit = int(os.getenv("AI_RATE_LIMIT_PER_DAY", "30"))

    def log_message(self, format: str, *args: Any) -> None:
        return

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK,
                  headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, content_type: str) -> None:
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid content length") from exc
        if length <= 0 or length > 32_768:
            raise ValueError("JSON body is required and must be under 32 KB")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def db_connection(self) -> sqlite3.Connection:
        uri = f"file:{self.database_path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def first(params: dict[str, list[str]], name: str, default: str | None = None) -> str | None:
        values = params.get(name)
        return values[0] if values else default

    def ticker(self, params: dict[str, list[str]]) -> str:
        ticker = (self.first(params, "ticker") or "").strip().upper()
        if not ticker or len(ticker) > 12 or not ticker.isalnum():
            raise ValueError("ticker is required and must be alphanumeric")
        return ticker

    def limit(self, params: dict[str, list[str]], default: int = 500) -> int:
        return max(1, min(self.max_limit, int(self.first(params, "limit", str(default)) or default)))

    def rows(self, sql: str, values: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        # sqlite3.Connection.__exit__ commits but does not close the handle.
        # Explicit closure allows Windows to atomically replace the database.
        with closing(self.db_connection()) as connection:
            return [dict(row) for row in connection.execute(sql, values).fetchall()]

    def signal_rows(self, sql: str, values: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if not self.signals_database_path.exists():
            return []
        uri = f"file:{self.signals_database_path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(sql, values).fetchall()]

    def industry_valuation(self, ticker: str) -> dict[str, Any]:
        companies = self.rows(
            "SELECT ticker,industry,sector FROM companies WHERE ticker=? LIMIT 1", (ticker,)
        )
        if not companies:
            raise LookupError("ticker not found")
        company = companies[0]

        def peers(field: str, classification: str) -> list[dict[str, Any]]:
            return self.rows(
                f"SELECT c.ticker,c.{field} classification,s.as_of_utc,s.pe,s.market_cap,s.net_income_ttm "
                "FROM companies c JOIN financial_snapshot s ON s.ticker=c.ticker "
                f"WHERE c.{field}=? AND s.as_of_utc=(SELECT MAX(x.as_of_utc) "
                "FROM financial_snapshot x WHERE x.ticker=s.ticker)",
                (classification,),
            )

        field = "sector" if company.get("sector") else "industry"
        classification = str(company.get(field) or "")
        peer_rows = peers(field, classification) if classification else []
        if len(peer_rows) < 5 and company.get("industry") and field != "industry":
            field = "industry"
            classification = str(company.get("industry") or "")
            peer_rows = peers(field, classification)
        values = []
        for row in peer_rows:
            try:
                direct = float(row["pe"]) if row.get("pe") is not None else None
                derived = (float(row["market_cap"]) / float(row["net_income_ttm"])
                           if float(row.get("market_cap") or 0) > 0
                           and float(row.get("net_income_ttm") or 0) > 0 else None)
                value = direct if direct is not None and direct > 0 else derived
                if value is not None and 0 < value <= 200:
                    values.append(value)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
        return {
            "ticker": ticker,
            "classification_field": field,
            "classification": classification,
            "peer_count": len(values),
            "median_pe_ttm": median(values) if values else None,
            "method": "median of positive peer P/E; provider P/E, otherwise market_cap/net_income_ttm; values above 200 excluded",
            "display_only_not_used_in_fa_signal_or_backtest": True,
        }

    def authenticated_identity(self) -> TelegramIdentity:
        identity = validate_telegram_init_data(
            self.headers.get("X-Telegram-Init-Data", ""),
            self.telegram_bot_token,
            max_age_seconds=self.telegram_init_data_max_age,
        )
        if not self.telegram_public_access and identity.user_id not in self.telegram_allowed_user_ids:
            raise PermissionError("Telegram account is not allowed to use this application")
        return identity

    def paper_store(self, identity: TelegramIdentity) -> PaperTradingStore:
        if self.paper_database_url:
            return PostgresPaperTradingStore(
                self.paper_database_url, identity.user_id, self.database_path,
                self.signals_database_path,
                user_profile={"username": identity.username, "first_name": identity.first_name,
                              "last_name": identity.last_name},
            )
        # Local development remains usable without Postgres, but each verified
        # Telegram account gets an isolated file instead of the legacy shared ledger.
        ledger = self.paper_database_path.with_name(f"paper_trading_{identity.user_id}.sqlite")
        return PaperTradingStore(ledger, self.database_path, self.signals_database_path)

    def enforce_ai_rate_limit(self, identity: TelegramIdentity) -> None:
        checks = (
            (f"tg:{identity.user_id}", "ai:minute", self.ai_minute_limit, 60),
            (f"tg:{identity.user_id}", "ai:day", self.ai_daily_limit, 86_400),
            (f"ip:{self.client_address[0]}", "ai:minute", self.ai_minute_limit * 3, 60),
        )
        for subject, scope, limit, window in checks:
            result = self.rate_limiter.consume(subject, scope, limit, window)
            if not result.allowed:
                raise RateLimitExceeded(result.retry_after)

    def ai_context(self, ticker: str) -> dict[str, Any]:
        signal_rows = self.signal_rows("SELECT * FROM signals_latest WHERE ticker=?", (ticker,))
        if not signal_rows:
            raise LookupError("signal not found")
        prices = self.rows(
            "SELECT * FROM price_daily WHERE ticker=? AND analysis_ready=1 ORDER BY date DESC LIMIT 2",
            (ticker,),
        )
        annual = self.rows(
            "SELECT * FROM financial_annual WHERE ticker=? AND row_valid=1 "
            "ORDER BY period_end DESC,statement,item_code LIMIT 80", (ticker,),
        )
        quarterly = self.rows(
            "SELECT * FROM financial_quarterly WHERE ticker=? AND row_valid=1 "
            "ORDER BY period_end DESC,statement,item_code LIMIT 80", (ticker,),
        )
        snapshots = self.rows(
            "SELECT * FROM financial_snapshot WHERE ticker=? ORDER BY as_of_utc DESC LIMIT 1", (ticker,),
        )
        benchmark = self.rows(
            "SELECT * FROM benchmark_daily WHERE analysis_ready=1 ORDER BY date DESC LIMIT 1"
        )
        actions = self.rows(
            "SELECT * FROM corporate_actions WHERE ticker=? AND row_valid=1 ORDER BY ex_date DESC LIMIT 5",
            (ticker,),
        )
        trade = None
        trade_note = "Chưa lấy được giao dịch gần nhất từ DNSE."
        try:
            from stock_ai_reply import latest_trade, trade_status

            raw_trade = latest_trade(ticker)
            trade = {key: raw_trade.get(key) for key in ("symbol", "time", "matchPrice", "matchQtty")}
            trade_note = trade_status(raw_trade)
        except Exception:
            pass
        session_candle = None
        try:
            from dnse_session_candle import fetch_today_candle

            session_candle = fetch_today_candle(ticker)
        except Exception:
            pass
        from stock_report import display_valuation

        industry_valuation = self.industry_valuation(ticker)
        valuation = display_valuation({
            "signal": signal_rows[0],
            "price": prices[0] if prices else {},
            "snapshot": snapshots[0] if snapshots else {},
            "industry_valuation": industry_valuation,
        })
        return {
            "signal_from_rule_engine": signal_rows[0],
            "latest_price_eod": prices[0] if prices else None,
            "previous_price_eod": prices[1] if len(prices) > 1 else None,
            "latest_trade_dnse_raw": trade,
            "latest_trade_status": trade_note,
            "current_session_ohlcv_provisional": session_candle,
            "dnse_trade_price_unit": "thousand_vnd_per_share",
            "financial_annual_recent": annual,
            "financial_quarterly_recent": quarterly,
            "financial_snapshot": snapshots[0] if snapshots else None,
            "display_valuation_not_used_in_signal": valuation,
            "industry_valuation_not_used_in_signal": industry_valuation,
            "benchmark": benchmark[0] if benchmark else None,
            "corporate_actions": actions,
        }

    def health(self) -> dict[str, Any]:
        state_path = self.analysis_dir / "pipeline_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {"status": "unknown"}
        database_ok = self.database_path.exists() and self.database_path.stat().st_size > 0
        counts = self.rows(
            "SELECT (SELECT COUNT(*) FROM companies) AS companies, "
            "(SELECT COUNT(*) FROM price_daily) AS price_rows, "
            "(SELECT MAX(date) FROM price_daily WHERE analysis_ready = 1) AS latest_market_date"
        )[0] if database_ok else {}
        status = state.get("status", "unknown")
        finished = state.get("finished_at_utc") or state.get("started_at_utc")
        age_hours = None
        if finished:
            try:
                timestamp = datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
                age_hours = round((datetime.now(timezone.utc) - timestamp).total_seconds() / 3600, 2)
            except ValueError:
                age_hours = None
        pipeline_ok = status in {"success", "degraded"} and not state.get("analysis_deferred_reason")
        fresh = pipeline_ok and age_hours is not None and age_hours <= 72
        healthy = database_ok and bool(counts.get("price_rows"))
        return {
            "healthy": healthy,
            "database_ok": database_ok,
            "data_fresh": fresh,
            "pipeline_ok": pipeline_ok,
            "pipeline_age_hours": age_hours,
            "telegram_auth_configured": bool(self.telegram_bot_token),
            "paper_storage": "postgres" if self.paper_database_url else "sqlite-local",
            "ai_rate_limit": {"per_minute": self.ai_minute_limit, "per_day": self.ai_daily_limit,
                              "persistent": bool(self.paper_database_url)},
            **counts,
            "pipeline": state,
        }

    def do_GET(self) -> None:  # noqa: N802 - inherited HTTP method name
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                app = Path(__file__).resolve().with_name("webapp.html")
                self.send_html(app.read_text(encoding="utf-8"))
                return
            if parsed.path == "/admin":
                dashboard = Path(__file__).resolve().with_name("dashboard.html")
                self.send_html(dashboard.read_text(encoding="utf-8"))
                return
            if parsed.path == "/manifest.webmanifest":
                self.send_file(Path(__file__).resolve().with_name("manifest.webmanifest"),
                               "application/manifest+json; charset=utf-8")
                return
            if parsed.path == "/app-icon.svg":
                self.send_file(Path(__file__).resolve().with_name("app-icon.svg"),
                               "image/svg+xml; charset=utf-8")
                return
            if parsed.path == "/brand-x10.png":
                self.send_file(Path(__file__).resolve().with_name("brand-x10.png"), "image/png")
                return
            if parsed.path == "/chart_tools.js":
                self.send_file(Path(__file__).resolve().with_name("chart_tools.js"),
                               "application/javascript; charset=utf-8")
                return
            if parsed.path == "/health":
                payload = self.health()
                self.send_json(payload, HTTPStatus.OK if payload["healthy"] else HTTPStatus.SERVICE_UNAVAILABLE)
                return
            if parsed.path == "/v1/auth/me":
                identity = self.authenticated_identity()
                self.send_json({"data": {"telegram_user_id": identity.user_id,
                                           "username": identity.username,
                                           "first_name": identity.first_name,
                                           "last_name": identity.last_name}})
                return
            if parsed.path == "/ready":
                payload = self.health()
                # Liveness is not enough for automated decisions: readiness
                # also requires a completed, recent, non-deferred pipeline.
                self.send_json(payload, HTTPStatus.OK if payload["data_fresh"] else HTTPStatus.SERVICE_UNAVAILABLE)
                return
            if parsed.path == "/v1/summary":
                actions = self.signal_rows(
                    "SELECT final_action, COUNT(*) count FROM signals_latest GROUP BY final_action ORDER BY count DESC"
                )
                coverage = self.signal_rows(
                    "SELECT COUNT(*) total, SUM(CASE WHEN fa_status='READY' THEN 1 ELSE 0 END) fa_ready, "
                    "SUM(CASE WHEN fa_pass=1 THEN 1 ELSE 0 END) fa_pass, "
                    "SUM(CASE WHEN ta_status='READY' THEN 1 ELSE 0 END) ta_ready, MAX(signal_date) signal_date "
                    "FROM signals_latest"
                )
                self.send_json({"actions": actions, "coverage": coverage[0] if coverage else {}})
                return
            if parsed.path == "/v1/diagnostics":
                backtest_path = self.analysis_dir / "backtest_report.json"
                cache_key = (
                    self.database_path.stat().st_mtime_ns,
                    self.signals_database_path.stat().st_mtime_ns if self.signals_database_path.exists() else None,
                    backtest_path.stat().st_mtime_ns if backtest_path.exists() else None,
                )
                handler_class = self.__class__
                if handler_class._diagnostics_cache_key == cache_key and handler_class._diagnostics_cache is not None:
                    self.send_json(handler_class._diagnostics_cache)
                    return
                fa_breakdown = self.signal_rows(
                    "SELECT fa_status, COALESCE(NULLIF(hard_reject_reason,''),'none') reason, COUNT(*) count "
                    "FROM signals_latest GROUP BY fa_status, reason ORDER BY count DESC"
                )
                ta_breakdown = self.signal_rows(
                    "SELECT ta_status, COALESCE(NULLIF(missing_data_reason,''),'none') reason, COUNT(*) count "
                    "FROM signals_latest GROUP BY ta_status, reason ORDER BY count DESC"
                )
                review_tickers = self.signal_rows(
                    "SELECT ticker, sessions, signal_date, missing_data_reason, fa_status, "
                    "ROUND(fa_coverage*100,1) fa_coverage_pct, final_action "
                    "FROM signals_latest WHERE ta_status='DATA_REVIEW' ORDER BY sessions, ticker"
                )
                price_sources = self.rows(
                    "SELECT source, COUNT(*) rows, COUNT(DISTINCT ticker) tickers, MIN(date) first_date, MAX(date) last_date "
                    "FROM price_daily GROUP BY source ORDER BY rows DESC"
                )
                adjustment = self.rows(
                    "SELECT adjustment_status, COUNT(*) rows, COUNT(DISTINCT ticker) tickers "
                    "FROM price_daily GROUP BY adjustment_status ORDER BY rows DESC"
                )
                quality = self.rows(
                    "SELECT "
                    "COUNT(*) price_rows, "
                    "SUM(CASE WHEN analysis_ready=1 THEN 1 ELSE 0 END) analysis_ready_rows, "
                    "SUM(CASE WHEN analysis_ready=0 THEN 1 ELSE 0 END) rejected_price_rows, "
                    "SUM(CASE WHEN adjusted_close IS NOT NULL THEN 1 ELSE 0 END) adjusted_rows, "
                    "(SELECT COUNT(*) FROM benchmark_daily) benchmark_rows, "
                    "(SELECT COUNT(*) FROM financial_annual) annual_financial_rows, "
                    "(SELECT COUNT(*) FROM financial_quarterly) quarterly_financial_rows, "
                    "(SELECT COUNT(*) FROM financial_snapshot) snapshot_rows, "
                    "MIN(date) first_market_date, "
                    "MAX(CASE WHEN analysis_ready=1 THEN date END) latest_market_date "
                    "FROM price_daily"
                )[0]
                backtest = json.loads(backtest_path.read_text(encoding="utf-8")) if backtest_path.exists() else {}
                payload = {
                    "quality": quality,
                    "fa_breakdown": fa_breakdown,
                    "ta_breakdown": ta_breakdown,
                    "review_tickers": review_tickers,
                    "price_sources": price_sources,
                    "adjustment": adjustment,
                    "backtest": backtest,
                }
                handler_class._diagnostics_cache_key = cache_key
                handler_class._diagnostics_cache = payload
                self.send_json(payload)
                return
            if parsed.path == "/v1/universe":
                self.send_json({"data": self.rows("SELECT * FROM companies ORDER BY ticker")})
                return
            if parsed.path == "/v1/benchmark":
                rows = self.rows(
                    "SELECT ticker, date, open, high, low, close, adjusted_close, volume, "
                    "return_1d, source FROM benchmark_daily WHERE analysis_ready = 1 "
                    "ORDER BY date DESC LIMIT 1"
                )
                self.send_json({"data": rows[0] if rows else None})
                return
            if parsed.path == "/v1/actions":
                ticker = self.ticker(params)
                limit = self.limit(params, 5)
                data = self.rows(
                    "SELECT ticker, ex_date, action_type, cash_dividend, stock_ratio, "
                    "split_ratio, rights_ratio, rights_price, source FROM corporate_actions "
                    "WHERE ticker = ? AND row_valid = 1 ORDER BY ex_date DESC LIMIT ?",
                    (ticker, limit),
                )
                self.send_json({"data": data, "count": len(data)})
                return
            if parsed.path == "/v1/latest":
                ticker = self.ticker(params)
                rows = self.rows(
                    "SELECT * FROM price_daily WHERE ticker = ? AND analysis_ready = 1 ORDER BY date DESC LIMIT 1",
                    (ticker,),
                )
                if not rows:
                    self.send_json({"error": "ticker or price not found"}, HTTPStatus.NOT_FOUND)
                else:
                    self.send_json({"data": rows[0]})
                return
            if parsed.path == "/v1/quote":
                ticker = self.ticker(params)
                try:
                    from stock_ai_reply import is_fresh_trade, latest_trade, trade_status

                    trade = latest_trade(ticker)
                    raw_price = trade.get("matchPrice")
                    price_vnd = float(raw_price) * 1000 if raw_price is not None and float(raw_price) > 0 else None
                    self.send_json({"data": {
                        "ticker": ticker, "price_vnd": price_vnd,
                        "time": trade.get("time"), "match_quantity_raw": trade.get("matchQtty"),
                        "status": trade_status(trade), "source": "DNSE",
                        "fresh": is_fresh_trade(trade),
                        "price_basis": "unadjusted latest matched trade",
                    }})
                except Exception:
                    self.send_json({"data": None, "status": "unavailable"})
                return
            if parsed.path == "/v1/session-candle":
                ticker = self.ticker(params)
                try:
                    from dnse_session_candle import fetch_today_candle

                    candle = fetch_today_candle(ticker)
                    self.send_json({"data": candle, "status": "ok" if candle else "no_session_candle"})
                except RuntimeError as exc:
                    reason = str(exc)
                    self.send_json({"data": None, "status": reason if reason.startswith("DNSE OHLC HTTP ")
                                    else "unavailable"})
                except Exception:
                    self.send_json({"data": None, "status": "unavailable"})
                return
            if parsed.path == "/v1/quotes":
                raw = (self.first(params, "tickers") or "").upper()
                tickers = list(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
                if not tickers or len(tickers) > 40 or any(not value.isalnum() or len(value) > 12 for value in tickers):
                    raise ValueError("tickers must contain 1-40 alphanumeric symbols")
                placeholders = ",".join("?" for _ in tickers)
                eod_rows = self.rows(
                    "SELECT p.ticker,p.date,p.close FROM price_daily p "
                    f"WHERE p.ticker IN ({placeholders}) AND p.analysis_ready=1 "
                    "AND p.date=(SELECT MAX(q.date) FROM price_daily q "
                    "WHERE q.ticker=p.ticker AND q.analysis_ready=1)", tuple(tickers),
                )
                from market_quotes import quote_board

                data = quote_board(tickers, {row["ticker"]: row for row in eod_rows})
                self.send_json({"data": data, "refresh_after_seconds": 90})
                return
            if parsed.path == "/v1/snapshot":
                ticker = self.ticker(params)
                rows = self.rows(
                    "SELECT * FROM financial_snapshot WHERE ticker = ? ORDER BY as_of_utc DESC LIMIT 1",
                    (ticker,),
                )
                self.send_json({"data": rows[0] if rows else None})
                return
            if parsed.path == "/v1/industry-valuation":
                ticker = self.ticker(params)
                self.send_json({"data": self.industry_valuation(ticker)})
                return
            if parsed.path == "/v1/prices":
                ticker = self.ticker(params)
                start = self.first(params, "start", "1900-01-01")
                end = self.first(params, "end", "2999-12-31")
                limit = self.limit(params, 1000)
                data = self.rows(
                    "SELECT * FROM price_daily WHERE ticker = ? AND date BETWEEN ? AND ? "
                    "AND analysis_ready = 1 ORDER BY date DESC LIMIT ?",
                    (ticker, start, end, limit),
                )
                self.send_json({"data": list(reversed(data)), "count": len(data)})
                return
            if parsed.path == "/v1/financials":
                ticker = self.ticker(params)
                period = (self.first(params, "period", "annual") or "annual").lower()
                if period not in {"annual", "quarterly"}:
                    raise ValueError("period must be annual or quarterly")
                table = "financial_annual" if period == "annual" else "financial_quarterly"
                limit = self.limit(params, 1000)
                data = self.rows(
                    f"SELECT * FROM {table} WHERE ticker = ? AND row_valid = 1 "
                    "ORDER BY period_end DESC, statement, item_code LIMIT ?",
                    (ticker, limit),
                )
                self.send_json({"data": data, "count": len(data)})
                return
            if parsed.path == "/v1/signal":
                ticker = self.ticker(params)
                rows = self.signal_rows("SELECT * FROM signals_latest WHERE ticker = ?", (ticker,))
                if not rows:
                    self.send_json({"error": "signal not found"}, HTTPStatus.NOT_FOUND)
                else:
                    self.send_json({"data": rows[0]})
                return
            if parsed.path == "/v1/rankings":
                limit = self.limit(params, 20)
                action = (self.first(params, "action") or "").strip().upper()
                if action == "SCREEN_BUY":
                    data = self.signal_rows(
                        "SELECT * FROM signals_latest WHERE screen_action='BUY' "
                        "ORDER BY unified_score DESC,selection_strength DESC,fa_score DESC LIMIT ?",
                        (limit,),
                    )
                elif action:
                    data = self.signal_rows(
                        "SELECT * FROM signals_latest WHERE final_action = ? ORDER BY unified_score DESC, selection_strength DESC, fa_score DESC LIMIT ?",
                        (action, limit),
                    )
                else:
                    data = self.signal_rows(
                        "SELECT * FROM signals_latest ORDER BY CASE final_action WHEN 'BUY' THEN 1 WHEN 'HOLD' THEN 2 WHEN 'WATCH' THEN 3 WHEN 'DATA_REVIEW' THEN 4 ELSE 5 END, unified_score DESC, selection_strength DESC, fa_score DESC LIMIT ?",
                        (limit,),
                    )
                self.send_json({"data": data, "count": len(data)})
                return
            if parsed.path == "/v1/market-context":
                context_path = self.analysis_dir / "market_context.json"
                context = json.loads(context_path.read_text(encoding="utf-8")) if context_path.exists() else {"status": "UNAVAILABLE"}
                self.send_json({"data": context})
                return
            if parsed.path == "/v1/backtest":
                backtest_path = self.analysis_dir / "backtest_report.json"
                payload = json.loads(backtest_path.read_text(encoding="utf-8")) if backtest_path.exists() else {}
                current_date = self.rows(
                    "SELECT MAX(date) AS latest_date FROM price_daily WHERE analysis_ready=1"
                )[0]["latest_date"]
                report_date = str(payload.get("data_latest_date") or "")[:10]
                payload["current_data_latest_date"] = current_date
                payload["report_stale"] = not report_date or report_date < str(current_date or "")[:10]
                self.send_json({"data": payload})
                return
            if parsed.path == "/v1/model-portfolio":
                runs = self.signal_rows("SELECT * FROM model_runs ORDER BY signal_date DESC LIMIT 1")
                positions = self.signal_rows(
                    "SELECT p.*,s.final_action,s.close,s.unified_score,s.exit_reason_code,s.decision_reason "
                    "FROM model_positions p LEFT JOIN signals_latest s ON s.ticker=p.ticker "
                    "ORDER BY s.unified_score DESC,p.ticker"
                )
                pending = self.signal_rows(
                    "SELECT o.*,s.close,s.unified_score,s.exit_reason_code,s.decision_reason "
                    "FROM model_pending_orders o LEFT JOIN signals_latest s ON s.ticker=o.ticker "
                    "ORDER BY o.side,o.ticker"
                )
                self.send_json({"data": {"run": runs[0] if runs else None,
                                           "positions": positions, "pending_orders": pending}})
                return
            if parsed.path == "/v1/paper/portfolio":
                identity = self.authenticated_identity()
                self.send_json({"data": self.paper_store(identity).portfolio()})
                return
            if parsed.path == "/v1/paper/portfolio-live":
                identity = self.authenticated_identity()
                store = self.paper_store(identity)
                base = store.portfolio()
                positions = base["positions"]
                quotes: dict[str, dict[str, Any]] = {}
                if positions:
                    from market_quotes import quote_board

                    tickers = [row["ticker"] for row in positions]
                    eod = {row["ticker"]: {"close": row["raw_close"], "date": row["date"]}
                           for row in positions}
                    for offset in range(0, len(tickers), 40):
                        batch = tickers[offset:offset + 40]
                        quotes.update({row["ticker"]: row for row in quote_board(batch, eod)})
                self.send_json({"data": store.marked_portfolio(quotes, base)})
                return
            self.send_json({"error": "endpoint not found"}, HTTPStatus.NOT_FOUND)
        except AuthenticationError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.UNAUTHORIZED)
        except PermissionError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.FORBIDDEN)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception:
            LOGGER.exception("GET %s failed", parsed.path)
            self.send_json({"error": "internal server error"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802 - inherited HTTP method name
        parsed = urlparse(self.path)
        try:
            payload = self.read_json()
            if parsed.path == "/v1/paper/order":
                identity = self.authenticated_identity()
                ticker = str(payload.get("ticker") or "")
                side = str(payload.get("side") or "")
                quantity = payload.get("quantity")
                if isinstance(quantity, float) and quantity.is_integer():
                    quantity = int(quantity)
                result = self.paper_store(identity).order(ticker, side, quantity)
                self.send_json({"data": result}, HTTPStatus.CREATED)
                return
            if parsed.path == "/v1/paper/reset":
                identity = self.authenticated_identity()
                if payload.get("confirm") != "RESET":
                    raise ValueError("confirm must equal RESET")
                self.send_json({"data": self.paper_store(identity).reset()})
                return
            if parsed.path == "/v1/ai-analysis":
                identity = self.authenticated_identity()
                ticker = str(payload.get("ticker") or "").strip().upper()
                question = str(payload.get("question") or "").strip()
                if not ticker.isalnum() or len(ticker) > 12:
                    raise ValueError("invalid ticker")
                if not question or len(question) > 1000:
                    raise ValueError("question is required and must be under 1000 characters")
                self.enforce_ai_rate_limit(identity)
                from stock_research import investment_research

                try:
                    answer, sources = investment_research(self.ai_context(ticker), question)
                except Exception as exc:
                    LOGGER.exception("AI analysis failed for %s: %s", ticker, type(exc).__name__)
                    self.send_json(
                        {"error": "Dịch vụ phân tích AI tạm thời chưa phản hồi. Vui lòng thử lại."},
                        HTTPStatus.SERVICE_UNAVAILABLE,
                    )
                    return
                self.send_json({"data": {"ticker": ticker, "answer": answer, "sources": sources}})
                return
            self.send_json({"error": "endpoint not found"}, HTTPStatus.NOT_FOUND)
        except AuthenticationError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.UNAUTHORIZED)
        except PermissionError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.FORBIDDEN)
        except RateLimitExceeded as exc:
            self.send_json(
                {"error": f"Bạn đã dùng quá nhiều lượt phân tích AI. Thử lại sau {exc.retry_after} giây.",
                 "retry_after": exc.retry_after},
                HTTPStatus.TOO_MANY_REQUESTS,
                {"Retry-After": str(exc.retry_after)},
            )
        except (ValueError, PaperTradingError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except LookupError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
        except Exception:
            LOGGER.exception("POST %s failed", parsed.path)
            self.send_json({"error": "internal server error"}, HTTPStatus.INTERNAL_SERVER_ERROR)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve analysis-ready stock data over HTTP")
    parser.add_argument("--database", type=Path, default=Path("analysis_data/stocks_analysis.sqlite"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    database = args.database.resolve()
    if not database.exists():
        raise SystemExit(f"Database not found: {database}")
    StockDataHandler.database_path = database
    StockDataHandler.analysis_dir = database.parent
    StockDataHandler.signals_database_path = database.parent / "signals.sqlite"
    StockDataHandler.paper_database_path = database.parent / "paper_trading.sqlite"
    if os.getenv("APP_ENV", "development").strip().lower() == "production":
        if not StockDataHandler.telegram_bot_token:
            raise SystemExit("Production requires TELEGRAM_BOT_TOKEN")
        if not StockDataHandler.paper_database_url:
            raise SystemExit("Production requires PAPER_DATABASE_URL or SUPABASE_DB_URL")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[RotatingFileHandler(database.parent / "local_api.error.log",
                                      maxBytes=1_000_000, backupCount=2, encoding="utf-8")],
    )
    server = ThreadingHTTPServer((args.host, args.port), StockDataHandler)
    print(f"Stock data API: http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
