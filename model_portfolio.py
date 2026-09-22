"""Stateful end-of-day model portfolio built on top of explainable signals."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

import numpy as np
import pandas as pd


def empty_state() -> dict[str, Any]:
    return {"last_signal_date": None, "positions": {}, "pending_orders": {}, "cooldowns": {}}


def _session_steps(last_date: str | None, current_date: str,
                   trading_dates: Iterable[str]) -> int:
    if not last_date or current_date <= last_date:
        return 0
    dates = sorted({str(value)[:10] for value in trading_dates})
    return sum(last_date < value <= current_date for value in dates)


def _advance_state(state: dict[str, Any], current_date: str, trading_dates: Iterable[str],
                   cooldown_days: int, tradable_tickers: set[str]) -> dict[str, Any]:
    result = deepcopy(state)
    last_date = result.get("last_signal_date")
    steps = _session_steps(last_date, current_date, trading_dates)
    if steps <= 0:
        return result

    positions = result.setdefault("positions", {})
    cooldowns = result.setdefault("cooldowns", {})
    pending = result.setdefault("pending_orders", {})
    for position in positions.values():
        position["holding_sessions"] = int(position.get("holding_sessions", 0)) + steps
    for ticker in list(cooldowns):
        remaining = max(0, int(cooldowns[ticker]) - steps)
        if remaining:
            cooldowns[ticker] = remaining
        else:
            cooldowns.pop(ticker, None)

    # Orders decided at the previous EOD are assumed filled on the first later
    # trading session. New buys have zero holding sessions on their fill day.
    for ticker, order in list(pending.items()):
        if str(order.get("signal_date") or "") >= current_date:
            continue
        side = str(order.get("side") or "").upper()
        if side == "SELL":
            if ticker not in tradable_tickers:
                continue
            positions.pop(ticker, None)
            remaining = max(0, cooldown_days - max(0, steps - 1))
            if remaining:
                cooldowns[ticker] = remaining
        elif side == "BUY":
            if ticker in tradable_tickers and ticker not in positions and int(cooldowns.get(ticker, 0)) == 0:
                positions[ticker] = {
                    "entry_signal_date": str(order.get("signal_date") or ""),
                    "entry_date": current_date,
                    "holding_sessions": max(0, steps - 1),
                }
        pending.pop(ticker, None)
    return result


def apply_model_portfolio(signals: pd.DataFrame, config: dict[str, Any],
                          state: dict[str, Any] | None, current_date: str,
                          trading_dates: Iterable[str]) -> tuple[pd.DataFrame, dict[str, Any], pd.DataFrame]:
    """Apply prior holdings, T+1 pending orders and cooldowns to today's signals."""
    cooldown_days = int(config.get("cooldown_days", 5))
    minimum_holding_days = int(config.get("minimum_holding_days", 10))
    max_positions = int(config.get("max_positions", 30))
    tradable = signals["price_fresh"].fillna(False).astype(bool)
    price = signals["price"] if "price" in signals else pd.Series(1.0, index=signals.index)
    tradable &= pd.to_numeric(price, errors="coerce").gt(0)
    if "volume" in signals:
        tradable &= pd.to_numeric(signals["volume"], errors="coerce").gt(0)
    tradable_tickers = set(signals.loc[tradable, "ticker"].astype(str))
    updated = _advance_state(state or empty_state(), current_date, trading_dates,
                             cooldown_days, tradable_tickers)
    positions: dict[str, dict[str, Any]] = updated.setdefault("positions", {})
    cooldowns: dict[str, int] = updated.setdefault("cooldowns", {})

    result = signals.copy()
    # Preserve the market-wide screen independently from portfolio orders.
    # A strong stock can remain SCREEN BUY while its model position is HOLD.
    if "final_action" in result:
        result["screen_action"] = result["final_action"]
    else:
        result["screen_action"] = np.where(result["entry_eligible"], "BUY", "WATCH")
    ticker_index = {str(ticker): index for index, ticker in result["ticker"].items()}
    result["is_held"] = result["ticker"].astype(str).isin(positions)
    result["model_entry_signal_date"] = result["ticker"].map(
        lambda ticker: positions.get(str(ticker), {}).get("entry_signal_date")
    )
    result["model_entry_date"] = result["ticker"].map(
        lambda ticker: positions.get(str(ticker), {}).get("entry_date")
    )
    result["holding_sessions"] = result["ticker"].map(
        lambda ticker: int(positions.get(str(ticker), {}).get("holding_sessions", 0))
    )
    result["cooldown_remaining"] = result["ticker"].map(
        lambda ticker: int(cooldowns.get(str(ticker), 0))
    )

    actions = pd.Series("WATCH", index=result.index, dtype="string")
    ta_ready = result["ta_status"].eq("READY")
    price_fresh = result["price_fresh"].fillna(False).astype(bool)
    has_exit_data = result["has_exit_data"].fillna(False).astype(bool)
    actions.loc[~ta_ready] = "DATA_REVIEW"

    for ticker, position in positions.items():
        index = ticker_index.get(ticker)
        if index is None:
            continue
        holding_sessions = int(position.get("holding_sessions", 0))
        if not has_exit_data.loc[index] or not price_fresh.loc[index]:
            actions.loc[index] = "DATA_REVIEW"
            continue
        risk_exit = bool(result.loc[index, "exit_risk_trigger"])
        score_exit = bool(result.loc[index, "exit_score_trigger"]) and holding_sessions >= minimum_holding_days
        actions.loc[index] = "EXIT" if risk_exit or score_exit else "HOLD"

    # A newly signalled EXIT still occupies its slot until the T+1 sell. This
    # matches the backtest: only previously pending sells free capacity today.
    slots = max(0, max_positions - len(positions))
    eligible = result["screen_action"].eq("BUY") & ~result["is_held"]
    eligible &= result["cooldown_remaining"].eq(0)
    selected = result.loc[eligible].sort_values(
        ["unified_score", "selection_strength", "ticker"], ascending=[False, False, True]
    ).head(slots).index
    actions.loc[selected] = "BUY"
    result["final_action"] = actions

    result["exit_score_blocked_by_min_hold"] = (
        result["is_held"] & result["exit_score_trigger"].fillna(False).astype(bool)
        & result["holding_sessions"].lt(minimum_holding_days)
        & ~result["exit_risk_trigger"].fillna(False).astype(bool)
    )
    result["exit_triggered"] = result["final_action"].eq("EXIT")
    result["watch_reason"] = result["watch_reason"].astype("string")
    result.loc[eligible & ~result.index.isin(selected), "watch_reason"] = "NO_FREE_SLOT"
    result.loc[result["cooldown_remaining"].gt(0) & result["entry_eligible"], "watch_reason"] = "COOLDOWN"

    reason_parts = []
    for _, row in result.iterrows():
        reasons = []
        if bool(row.get("exit_ma_trigger")):
            reasons.append("PRICE_BELOW_MA")
        if bool(row.get("exit_trailing_trigger")):
            reasons.append("TRAILING_STOP_60D")
        if bool(row.get("exit_score_trigger")):
            reasons.append("SCORE_BELOW_EXIT")
        if bool(row.get("exit_market_trigger")):
            reasons.append("MARKET_REGIME")
        reason_parts.append("|".join(reasons) or "NONE")
    result["exit_reason_code"] = reason_parts
    for index, row in result.loc[result["is_held"]].iterrows():
        action = str(row["final_action"])
        if action == "EXIT":
            result.loc[index, "decision_reason"] = (
                f"EXIT danh mục mô hình vì {row['exit_reason_code']}. Tín hiệu hình thành sau EOD "
                f"{current_date}; lệnh bán được giả định thực hiện ở phiên kế tiếp."
            )
        elif action == "HOLD" and bool(row.get("exit_score_blocked_by_min_hold")):
            result.loc[index, "decision_reason"] = (
                f"Tiếp tục HOLD: điểm đã dưới ngưỡng thoát nhưng mới nắm giữ "
                f"{int(row['holding_sessions'])}/{minimum_holding_days} phiên; chưa vi phạm MA hoặc trailing stop."
            )
        elif action == "HOLD":
            result.loc[index, "decision_reason"] = (
                "Tiếp tục HOLD vì vị thế chưa vi phạm MA thoát, trailing stop hoặc ngưỡng điểm 35."
            )
        else:
            result.loc[index, "decision_reason"] = (
                "Chuyển DATA_REVIEW vì dữ liệu thoát thiếu hoặc giá không cùng phiên mới nhất; không phát lệnh bán."
            )

    pending = updated.setdefault("pending_orders", {})
    # Same-day reruns replace only today's decisions and never execute them.
    for ticker in list(pending):
        if str(pending[ticker].get("signal_date") or "") == current_date:
            pending.pop(ticker, None)
    events = []
    for _, row in result.loc[result["final_action"].isin(["BUY", "EXIT", "HOLD", "DATA_REVIEW"])].iterrows():
        ticker = str(row["ticker"])
        action = str(row["final_action"])
        if action == "BUY":
            pending[ticker] = {"side": "BUY", "signal_date": current_date}
        elif action == "EXIT":
            pending[ticker] = {"side": "SELL", "signal_date": current_date}
        if action in {"BUY", "EXIT"} or bool(row.get("is_held")):
            events.append({
                "signal_date": current_date, "ticker": ticker, "action": action,
                "reason_code": str(row.get("exit_reason_code") or row.get("watch_reason") or "NONE"),
                "unified_score": float(row["unified_score"]) if pd.notna(row.get("unified_score")) else None,
                "holding_sessions": int(row.get("holding_sessions") or 0),
            })
    updated["last_signal_date"] = current_date
    action_order = pd.Categorical(
        result["final_action"], ["BUY", "HOLD", "WATCH", "DATA_REVIEW", "EXIT"], ordered=True
    )
    result = result.assign(_order=action_order).sort_values(
        ["_order", "unified_score", "selection_strength", "ticker"],
        ascending=[True, False, False, True],
    ).drop(columns="_order").reset_index(drop=True)
    return result, updated, pd.DataFrame(events)
