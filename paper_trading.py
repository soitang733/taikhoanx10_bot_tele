"""Local paper-trading ledger using analysis-ready EOD prices only."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
from typing import Any, Callable
from zoneinfo import ZoneInfo


class PaperTradingError(ValueError):
    """A user-facing validation error for a simulated order."""


class PaperTradingStore:
    fee_rate = 0.0015

    def __init__(self, ledger_path: Path, market_path: Path, signals_path: Path,
                 initial_cash: float = 1_000_000_000.0,
                 realtime_quote: Callable[[str], dict[str, Any]] | None = None) -> None:
        self.ledger_path = ledger_path
        self.market_path = market_path
        self.signals_path = signals_path
        self.initial_cash = initial_cash
        self.realtime_quote = realtime_quote
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.ledger_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                "CREATE TABLE IF NOT EXISTS paper_account ("
                "id INTEGER PRIMARY KEY CHECK(id=1), initial_cash REAL NOT NULL, cash REAL NOT NULL, "
                "created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS paper_trades ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, "
                "side TEXT NOT NULL CHECK(side IN ('BUY','SELL')), quantity INTEGER NOT NULL CHECK(quantity>0), "
                "price REAL NOT NULL CHECK(price>0), gross_amount REAL NOT NULL, fee REAL NOT NULL, "
                "market_date TEXT NOT NULL, signal_action TEXT, price_source TEXT, trade_time TEXT, "
                "executed_at_utc TEXT NOT NULL);"
                "CREATE INDEX IF NOT EXISTS idx_paper_trades_ticker_id ON paper_trades(ticker,id);"
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(paper_trades)")}
            if "price_source" not in columns:
                connection.execute("ALTER TABLE paper_trades ADD COLUMN price_source TEXT")
            if "trade_time" not in columns:
                connection.execute("ALTER TABLE paper_trades ADD COLUMN trade_time TEXT")
            now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            connection.execute(
                "INSERT OR IGNORE INTO paper_account(id,initial_cash,cash,created_at_utc,updated_at_utc) "
                "VALUES(1,?,?,?,?)", (self.initial_cash, self.initial_cash, now, now)
            )

    def _market_row(self, ticker: str, *, before_today: bool = False) -> dict[str, Any]:
        uri = f"file:{self.market_path.as_posix()}?mode=ro"
        today = datetime.now(ZoneInfo("Asia/Ho_Chi_Minh")).date().isoformat()
        with closing(sqlite3.connect(uri, uri=True, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT p.ticker,p.date,COALESCE(p.adjusted_close,p.close) price,p.close raw_close,"
                "c.company_name,c.exchange "
                "FROM price_daily p LEFT JOIN companies c ON c.ticker=p.ticker "
                "WHERE p.ticker=? AND p.analysis_ready=1 AND (?=0 OR substr(p.date,1,10)<?) "
                "ORDER BY p.date DESC LIMIT 1", (ticker, int(before_today), today)
            ).fetchone()
        if not row or not row["price"] or float(row["price"]) <= 0:
            raise PaperTradingError("Không có giá EOD hợp lệ cho mã này")
        return dict(row)

    def _signal_action(self, ticker: str) -> str | None:
        if not self.signals_path.exists():
            return None
        uri = f"file:{self.signals_path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=10)) as connection:
            row = connection.execute(
                "SELECT final_action FROM signals_latest WHERE ticker=?", (ticker,)
            ).fetchone()
        return str(row[0]) if row else None

    def _signal_row(self, ticker: str) -> dict[str, Any] | None:
        if not self.signals_path.exists():
            return None
        uri = f"file:{self.signals_path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM signals_latest WHERE ticker=?", (ticker,)
            ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _position_entry_dates(trades: list[dict[str, Any]]) -> dict[str, str]:
        quantities: dict[str, int] = {}
        entries: dict[str, str] = {}
        for trade in trades:
            ticker = str(trade["ticker"])
            before = quantities.get(ticker, 0)
            quantity = int(trade["quantity"])
            after = before + quantity if trade["side"] == "BUY" else before - quantity
            if before <= 0 < after:
                entries[ticker] = str(trade.get("market_date") or "")[:10]
            if after <= 0:
                entries.pop(ticker, None)
            quantities[ticker] = after
        return entries

    def _holding_sessions(self, entry_date: str, signal_date: str) -> int:
        if not entry_date or not signal_date:
            return 0
        uri = f"file:{self.market_path.as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=10)) as connection:
            has_benchmark = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='benchmark_daily'"
            ).fetchone()
            table = "benchmark_daily" if has_benchmark else "price_daily"
            row = connection.execute(
                f"SELECT COUNT(DISTINCT substr(date,1,10)) FROM {table} "
                "WHERE analysis_ready=1 AND substr(date,1,10)>? AND substr(date,1,10)<=?",
                (entry_date, signal_date),
            ).fetchone()
        return int(row[0] or 0)

    def _annotate_position_actions(self, rows: list[dict[str, Any]],
                                   trades: list[dict[str, Any]]) -> None:
        config_path = Path(__file__).resolve().with_name("strategy_config.json")
        config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        exit_score = float(config.get("exit_score", 35))
        minimum_holding = int(config.get("minimum_holding_days", 10))
        trailing = float(config.get("trailing_stop_from_60d_high", .15))
        entries = self._position_entry_dates(trades)
        for position in rows:
            ticker = str(position["ticker"])
            signal = self._signal_row(ticker)
            entry_date = entries.get(ticker, "")
            signal_date = str((signal or {}).get("signal_date") or position.get("date") or "")[:10]
            holding_sessions = self._holding_sessions(entry_date, signal_date)
            position["position_entry_date"] = entry_date or None
            position["holding_sessions"] = holding_sessions
            position["model_signal_action"] = (signal or {}).get("final_action")
            if not signal:
                position["position_action"] = "DATA_REVIEW"
                position["position_action_reason"] = "Chưa có dữ liệu tín hiệu để đánh giá vị thế này."
                position["exit_conditions"] = []
                continue
            price = float(signal.get("price") or 0)
            ma_key = f"ma{int(config.get('exit_ma', 200))}"
            ma_value = signal.get(ma_key)
            high60 = signal.get("high_close60")
            score = signal.get("unified_score")
            fresh = bool(signal.get("price_fresh"))
            has_data = price > 0 and ma_value is not None
            ma_trigger = has_data and price < float(ma_value)
            trailing_level = float(high60) * (1 - trailing) if high60 is not None else None
            trailing_trigger = trailing_level is not None and price < trailing_level
            score_trigger = score is not None and float(score) < exit_score
            market_trigger = bool(config.get("exit_on_market_regime", False)) and not bool(signal.get("market_bull"))
            score_eligible = score_trigger and holding_sessions >= minimum_holding
            conditions = [
                {"code": "PRICE_BELOW_MA", "triggered": ma_trigger,
                 "detail": f"Giá {price:,.0f} so với {ma_key.upper()} {float(ma_value):,.0f}" if ma_value is not None else f"Thiếu {ma_key.upper()}"},
                {"code": "TRAILING_STOP_60D", "triggered": trailing_trigger,
                 "detail": f"Giá {price:,.0f} so với ngưỡng trailing {trailing_level:,.0f}" if trailing_level is not None else "Thiếu đỉnh đóng cửa 60 phiên"},
                {"code": "SCORE_BELOW_EXIT", "triggered": score_trigger,
                 "eligible": score_eligible,
                 "detail": f"Điểm {float(score):.1f}/100; ngưỡng {exit_score:.0f}; đã giữ {holding_sessions}/{minimum_holding} phiên" if score is not None else "Thiếu điểm thống nhất"},
            ]
            if not has_data or not fresh:
                action = "DATA_REVIEW"
                reason = "Dữ liệu giá/MA thoát chưa đầy đủ hoặc không cùng phiên thị trường mới nhất."
            elif ma_trigger or trailing_trigger or market_trigger or score_eligible:
                action = "EXIT"
                triggered = [item["detail"] for item in conditions if item["triggered"] and item.get("eligible", True)]
                if market_trigger:
                    triggered.append("VN-Index đã vi phạm bộ lọc thị trường")
                reason = "Phát tín hiệu BÁN vì " + "; ".join(triggered) + "."
            else:
                action = "HOLD"
                if score_trigger:
                    reason = (f"Điểm đã dưới {exit_score:.0f} nhưng vị thế mới giữ {holding_sessions}/{minimum_holding} "
                              "phiên; chưa đủ thời gian tối thiểu. MA200 và trailing stop chưa bị vi phạm.")
                else:
                    reason = "Chưa vi phạm MA200, trailing stop 15% hoặc ngưỡng điểm thoát."
            position["position_action"] = action
            position["position_action_reason"] = reason
            position["exit_conditions"] = conditions

    def _execution_quote(self, ticker: str) -> dict[str, Any]:
        """Prefer a genuinely fresh DNSE match; otherwise use the latest stored EOD close."""
        try:
            if self.realtime_quote is None:
                from stock_ai_reply import latest_trade

                trade = latest_trade(ticker)
            else:
                trade = self.realtime_quote(ticker)
            from stock_ai_reply import is_fresh_trade

            raw_price = float(trade.get("matchPrice"))
            if is_fresh_trade(trade) and raw_price > 0:
                return {"ticker": ticker, "date": str(trade.get("time") or ""),
                        "price": raw_price * 1000, "company_name": None, "exchange": None,
                        "price_source": "DNSE_REALTIME", "trade_time": trade.get("time")}
        except Exception:
            pass
        # analysis_ready is the publication boundary: a same-day finalized row is
        # safe to trade, while provisional intraday rows never reach this query.
        quote = self._market_row(ticker)
        quote["price"] = float(quote["raw_close"])
        quote["price_source"] = "EOD_FALLBACK"
        quote["trade_time"] = None
        return quote

    @staticmethod
    def _positions(trades: list[dict[str, Any]]) -> tuple[dict[str, dict[str, float]], float]:
        positions: dict[str, dict[str, float]] = {}
        realized = 0.0
        for trade in trades:
            ticker = str(trade["ticker"])
            position = positions.setdefault(ticker, {"quantity": 0.0, "cost_basis": 0.0})
            quantity = float(trade["quantity"])
            if trade["side"] == "BUY":
                position["quantity"] += quantity
                position["cost_basis"] += float(trade["gross_amount"]) + float(trade["fee"])
            else:
                held = position["quantity"]
                average_cost = position["cost_basis"] / held if held else 0.0
                realized += float(trade["gross_amount"]) - float(trade["fee"]) - average_cost * quantity
                position["quantity"] -= quantity
                position["cost_basis"] = average_cost * position["quantity"]
        return positions, realized

    def _all_trades(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        return [dict(row) for row in connection.execute("SELECT * FROM paper_trades ORDER BY id")]

    def order(self, ticker: str, side: str, quantity: int) -> dict[str, Any]:
        ticker = ticker.strip().upper()
        side = side.strip().upper()
        if not ticker.isalnum() or len(ticker) > 12:
            raise PaperTradingError("Mã cổ phiếu không hợp lệ")
        if side not in {"BUY", "SELL"}:
            raise PaperTradingError("Lệnh phải là BUY hoặc SELL")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0 or quantity > 10_000_000:
            raise PaperTradingError("Khối lượng phải là số nguyên dương")
        quote = self._execution_quote(ticker)
        price = float(quote["price"])
        gross = price * quantity
        fee = round(gross * self.fee_rate, 2)
        signal_action = self._signal_action(ticker)
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            account = connection.execute("SELECT cash FROM paper_account WHERE id=1").fetchone()
            trades = self._all_trades(connection)
            positions, _ = self._positions(trades)
            held = int(positions.get(ticker, {}).get("quantity", 0))
            if side == "BUY":
                cash_change = -(gross + fee)
                if float(account["cash"]) + cash_change < -0.01:
                    connection.rollback()
                    raise PaperTradingError("Tiền mặt không đủ cho lệnh mua demo")
            else:
                if held < quantity:
                    connection.rollback()
                    raise PaperTradingError(f"Chỉ đang nắm giữ {held:,} cổ phiếu {ticker}")
                cash_change = gross - fee
            cursor = connection.execute(
                "INSERT INTO paper_trades(ticker,side,quantity,price,gross_amount,fee,market_date,"
                "signal_action,price_source,trade_time,executed_at_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (ticker, side, quantity, price, gross, fee, quote["date"], signal_action,
                 quote["price_source"], quote.get("trade_time"), now),
            )
            connection.execute(
                "UPDATE paper_account SET cash=cash+?,updated_at_utc=? WHERE id=1", (cash_change, now)
            )
            connection.commit()
        return {"id": cursor.lastrowid, "ticker": ticker, "side": side, "quantity": quantity,
                "price": price, "gross_amount": gross, "fee": fee,
                "market_date": quote["date"], "signal_action": signal_action,
                "price_source": quote["price_source"], "trade_time": quote.get("trade_time")}

    def portfolio(self) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            account = dict(connection.execute("SELECT * FROM paper_account WHERE id=1").fetchone())
            trades = self._all_trades(connection)
        positions, realized = self._positions(trades)
        rows = []
        market_value = 0.0
        unrealized = 0.0
        for ticker, position in positions.items():
            quantity = int(position["quantity"])
            if quantity <= 0:
                continue
            quote = self._market_row(ticker)
            average_cost = position["cost_basis"] / quantity
            value = float(quote["price"]) * quantity
            pnl = value - position["cost_basis"]
            market_value += value
            unrealized += pnl
            rows.append({**quote, "quantity": quantity, "average_cost": average_cost,
                         "market_value": value, "unrealized_pnl": pnl,
                         "unrealized_return": pnl / position["cost_basis"] if position["cost_basis"] else None,
                         "signal_action": self._signal_action(ticker)})
        self._annotate_position_actions(rows, trades)
        rows.sort(key=lambda item: item["market_value"], reverse=True)
        equity = float(account["cash"]) + market_value
        history = list(reversed(trades[-100:]))
        return {"account": account, "cash": float(account["cash"]), "market_value": market_value,
                "equity": equity, "total_return": equity / float(account["initial_cash"]) - 1,
                "realized_pnl": realized, "unrealized_pnl": unrealized,
                "positions": rows, "trades": history, "fee_rate": self.fee_rate,
                "pricing_basis": "analysis-ready adjusted EOD close"}

    def marked_portfolio(self, quotes: dict[str, dict[str, Any]],
                         base: dict[str, Any] | None = None) -> dict[str, Any]:
        """Display-only live valuation; never changes the ledger or execution prices."""
        result = base if base is not None else self.portfolio()
        market_value = 0.0
        unrealized = 0.0
        live_count = 0
        for position in result["positions"]:
            quote = quotes.get(position["ticker"], {})
            raw_eod = float(position.get("raw_close") or 0)
            raw_trade = float(quote.get("price_vnd") or 0)
            if quote.get("fresh") and raw_eod > 0 and raw_trade > 0:
                position["price"] = raw_trade * float(position["price"]) / raw_eod
                position["price_source"] = "DNSE_LIVE_ADJUSTED_ESTIMATE"
                position["quote_time"] = quote.get("trade_time")
                live_count += 1
            else:
                position["price_source"] = "EOD_ADJUSTED"
                position["quote_time"] = None
            position["market_value"] = position["price"] * position["quantity"]
            position["unrealized_pnl"] = position["market_value"] - position["average_cost"] * position["quantity"]
            position["unrealized_return"] = (
                position["unrealized_pnl"] / (position["average_cost"] * position["quantity"])
                if position["average_cost"] else None
            )
            market_value += position["market_value"]
            unrealized += position["unrealized_pnl"]
        result["market_value"] = market_value
        result["unrealized_pnl"] = unrealized
        result["equity"] = result["cash"] + market_value
        result["total_return"] = result["equity"] / float(result["account"]["initial_cash"]) - 1
        result["live_position_count"] = live_count
        result["pricing_basis"] = "adjusted live estimate where fresh; adjusted EOD otherwise"
        return result

    def reset(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with closing(self._connect()) as connection, connection:
            connection.execute("DELETE FROM paper_trades")
            connection.execute(
                "UPDATE paper_account SET cash=initial_cash,updated_at_utc=? WHERE id=1", (now,)
            )
        return self.portfolio()


class PostgresPaperTradingStore(PaperTradingStore):
    """Per-Telegram-user paper ledger stored transactionally in Postgres/Supabase."""

    def __init__(self, database_url: str, telegram_user_id: int, market_path: Path,
                 signals_path: Path, initial_cash: float = 1_000_000_000.0,
                 realtime_quote: Callable[[str], dict[str, Any]] | None = None,
                 user_profile: dict[str, Any] | None = None) -> None:
        if not database_url or telegram_user_id <= 0:
            raise ValueError("Postgres URL and a positive Telegram user ID are required")
        self.database_url = database_url
        self.telegram_user_id = int(telegram_user_id)
        self.market_path = market_path
        self.signals_path = signals_path
        self.initial_cash = initial_cash
        self.realtime_quote = realtime_quote
        self.user_profile = user_profile or {}
        self._ensure_account()

    def _connect_postgres(self):
        import psycopg
        from psycopg.rows import dict_row

        return psycopg.connect(self.database_url, row_factory=dict_row)

    def _ensure_account(self) -> None:
        now = datetime.now(timezone.utc)
        with self._connect_postgres() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO public.telegram_users"
                    "(telegram_user_id,username,first_name,last_name,last_seen_at) VALUES(%s,%s,%s,%s,%s) "
                    "ON CONFLICT(telegram_user_id) DO UPDATE SET username=excluded.username,"
                    "first_name=excluded.first_name,last_name=excluded.last_name,last_seen_at=excluded.last_seen_at",
                    (self.telegram_user_id, self.user_profile.get("username"),
                     self.user_profile.get("first_name", ""), self.user_profile.get("last_name", ""), now),
                )
                cursor.execute(
                    "INSERT INTO public.paper_accounts"
                    "(telegram_user_id,initial_cash,cash,created_at,updated_at) VALUES(%s,%s,%s,%s,%s) "
                    "ON CONFLICT(telegram_user_id) DO NOTHING",
                    (self.telegram_user_id, self.initial_cash, self.initial_cash, now, now),
                )

    def _user_trades(self, cursor) -> list[dict[str, Any]]:
        cursor.execute(
            "SELECT id,ticker,side,quantity,price,gross_amount,fee,market_date,signal_action,"
            "price_source,trade_time,executed_at AS executed_at_utc FROM public.paper_trades "
            "WHERE telegram_user_id=%s ORDER BY id", (self.telegram_user_id,),
        )
        return list(cursor.fetchall())

    def order(self, ticker: str, side: str, quantity: int) -> dict[str, Any]:
        ticker = ticker.strip().upper()
        side = side.strip().upper()
        if not ticker.isalnum() or len(ticker) > 12:
            raise PaperTradingError("Mã cổ phiếu không hợp lệ")
        if side not in {"BUY", "SELL"}:
            raise PaperTradingError("Lệnh phải là BUY hoặc SELL")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0 or quantity > 10_000_000:
            raise PaperTradingError("Khối lượng phải là số nguyên dương")
        quote = self._execution_quote(ticker)
        price = float(quote["price"])
        gross = price * quantity
        fee = round(gross * self.fee_rate, 2)
        signal_action = self._signal_action(ticker)
        now = datetime.now(timezone.utc)
        with self._connect_postgres() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT cash FROM public.paper_accounts WHERE telegram_user_id=%s FOR UPDATE",
                    (self.telegram_user_id,),
                )
                account = cursor.fetchone()
                if account is None:
                    raise RuntimeError("Paper account was not initialized")
                trades = self._user_trades(cursor)
                positions, _ = self._positions(trades)
                held = int(positions.get(ticker, {}).get("quantity", 0))
                if side == "BUY":
                    cash_change = -(gross + fee)
                    if float(account["cash"]) + cash_change < -0.01:
                        raise PaperTradingError("Tiền mặt không đủ cho lệnh mua demo")
                else:
                    if held < quantity:
                        raise PaperTradingError(f"Chỉ đang nắm giữ {held:,} cổ phiếu {ticker}")
                    cash_change = gross - fee
                cursor.execute(
                    "INSERT INTO public.paper_trades"
                    "(telegram_user_id,ticker,side,quantity,price,gross_amount,fee,market_date,"
                    "signal_action,price_source,trade_time,executed_at) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (self.telegram_user_id, ticker, side, quantity, price, gross, fee,
                     str(quote["date"])[:10], signal_action, quote["price_source"],
                     quote.get("trade_time"), now),
                )
                trade_id = int(cursor.fetchone()["id"])
                cursor.execute(
                    "UPDATE public.paper_accounts SET cash=cash+%s,updated_at=%s "
                    "WHERE telegram_user_id=%s", (cash_change, now, self.telegram_user_id),
                )
        return {"id": trade_id, "ticker": ticker, "side": side, "quantity": quantity,
                "price": price, "gross_amount": gross, "fee": fee,
                "market_date": quote["date"], "signal_action": signal_action,
                "price_source": quote["price_source"], "trade_time": quote.get("trade_time")}

    def portfolio(self) -> dict[str, Any]:
        with self._connect_postgres() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT telegram_user_id,initial_cash,cash,created_at AS created_at_utc,"
                    "updated_at AS updated_at_utc FROM public.paper_accounts WHERE telegram_user_id=%s",
                    (self.telegram_user_id,),
                )
                account = cursor.fetchone()
                trades = self._user_trades(cursor)
        if account is None:
            raise RuntimeError("Paper account was not initialized")
        positions, realized = self._positions(trades)
        rows = []
        market_value = 0.0
        unrealized = 0.0
        for ticker, position in positions.items():
            quantity = int(position["quantity"])
            if quantity <= 0:
                continue
            quote = self._market_row(ticker)
            average_cost = position["cost_basis"] / quantity
            value = float(quote["price"]) * quantity
            pnl = value - position["cost_basis"]
            market_value += value
            unrealized += pnl
            rows.append({**quote, "quantity": quantity, "average_cost": average_cost,
                         "market_value": value, "unrealized_pnl": pnl,
                         "unrealized_return": pnl / position["cost_basis"] if position["cost_basis"] else None,
                         "signal_action": self._signal_action(ticker)})
        self._annotate_position_actions(rows, trades)
        rows.sort(key=lambda item: item["market_value"], reverse=True)
        equity = float(account["cash"]) + market_value
        return {"account": account, "cash": float(account["cash"]), "market_value": market_value,
                "equity": equity, "total_return": equity / float(account["initial_cash"]) - 1,
                "realized_pnl": realized, "unrealized_pnl": unrealized, "positions": rows,
                "trades": list(reversed(trades[-100:])), "fee_rate": self.fee_rate,
                "pricing_basis": "analysis-ready adjusted EOD close"}

    def reset(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        with self._connect_postgres() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT telegram_user_id FROM public.paper_accounts "
                    "WHERE telegram_user_id=%s FOR UPDATE", (self.telegram_user_id,),
                )
                cursor.execute("DELETE FROM public.paper_trades WHERE telegram_user_id=%s",
                               (self.telegram_user_id,))
                cursor.execute(
                    "UPDATE public.paper_accounts SET cash=initial_cash,updated_at=%s "
                    "WHERE telegram_user_id=%s", (now, self.telegram_user_id),
                )
        return self.portfolio()
