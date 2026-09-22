"""Shared unified FA + price/volume scoring and portfolio simulation."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


CONDITION_NAMES = ("momentum", "relative_strength", "trend", "breakout", "volume")
SIMULATION_VERSION = "next-close-cash-v2"


def market_components(m: dict[str, np.ndarray], volume_ratio: float) -> dict[str, np.ndarray]:
    return {
        "momentum": (m["r3"] > 0) & (m["r6"] > 0) & (m["r12"] > 0),
        "relative_strength": m["rs6"] > 0,
        "trend": (m["px"] > m["ma50"]) & (m["ma50"] > m["ma200"]),
        "breakout": m["px"] > m["high_close20"],
        "volume": m["volume_ratio"] >= volume_ratio,
    }


def unified_scores(
    m: dict[str, np.ndarray],
    fa_score: np.ndarray,
    fa_weight: float,
    volume_ratio: float,
    component_weights: dict[str, float] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    components = market_components(m, volume_ratio)
    condition_count = np.stack(list(components.values()), axis=0).sum(axis=0)
    raw_weights = component_weights or {name: 1.0 for name in CONDITION_NAMES}
    weights = np.asarray([max(0.0, float(raw_weights.get(name, 0.0))) for name in CONDITION_NAMES])
    if weights.sum() <= 0:
        weights = np.ones(len(CONDITION_NAMES), dtype=float)
    weights = 100.0 * weights / weights.sum()
    market_score = np.zeros_like(fa_score, dtype=float)
    for weight, passed in zip(weights, components.values()):
        market_score += weight * passed
    score = fa_weight * fa_score + (1.0 - fa_weight) * market_score
    score = np.where(np.isfinite(fa_score), score, np.nan)
    return score, condition_count


def historical_fa_panel(
    companies: pd.DataFrame,
    annual: pd.DataFrame,
    config: dict[str, Any],
    reporting_lag_days: int = 90,
) -> pd.DataFrame:
    """Create annual FA snapshots using only reports available by an assumed date.

    Exact publication dates are unavailable, so annual statements become usable
    90 calendar days after period end. Current market snapshots are deliberately
    excluded to prevent future information leaking into historical tests.
    """
    from strategy_engine import build_fa

    source = annual.copy()
    source["period_end"] = pd.to_datetime(source["period_end"], errors="coerce")
    source = source.dropna(subset=["period_end"])
    clean_companies = companies.copy()
    clean_companies["market_cap"] = np.nan
    snapshots: list[pd.DataFrame] = []
    for cutoff in sorted(source["period_end"].drop_duplicates()):
        available = source.loc[source["period_end"] <= cutoff]
        scored = build_fa(clean_companies, available, pd.DataFrame(), config)
        part = scored[["ticker", "fa_score", "fa_coverage", "hard_reject_reason"]].copy()
        coverage = pd.to_numeric(part["fa_coverage"], errors="coerce").fillna(0).clip(0, 1)
        raw = pd.to_numeric(part["fa_score"], errors="coerce")
        part["effective_fa"] = 50.0 + (raw - 50.0) * coverage
        rejected = part["hard_reject_reason"].fillna("").astype(str).ne("")
        part["fa_eligible"] = ~rejected
        part.loc[rejected, "effective_fa"] = 0.0
        part["effective_date"] = pd.Timestamp(cutoff) + pd.Timedelta(days=reporting_lag_days)
        snapshots.append(part)
    if not snapshots:
        return pd.DataFrame(columns=["ticker", "effective_date", "effective_fa", "fa_eligible"])
    return pd.concat(snapshots, ignore_index=True).sort_values(["ticker", "effective_date"])


def fa_matrix(panel: pd.DataFrame, dates: pd.DatetimeIndex, tickers: list[str]) -> np.ndarray:
    result = np.full((len(dates), len(tickers)), np.nan, dtype=float)
    if panel.empty:
        return result
    for column, ticker in enumerate(tickers):
        rows = panel.loc[panel["ticker"].eq(ticker)].dropna(subset=["effective_date", "effective_fa"])
        if rows.empty:
            continue
        effective_dates = pd.DatetimeIndex(rows["effective_date"]).values
        values = pd.to_numeric(rows["effective_fa"], errors="coerce").to_numpy(dtype=float)
        positions = np.searchsorted(effective_dates, dates.values, side="right") - 1
        valid = positions >= 0
        result[valid, column] = values[positions[valid]]
    return result


def fa_eligibility_matrix(panel: pd.DataFrame, dates: pd.DatetimeIndex, tickers: list[str]) -> np.ndarray:
    """Return point-in-time FA hard-reject eligibility for each date and ticker."""
    result = np.zeros((len(dates), len(tickers)), dtype=bool)
    if panel.empty or "fa_eligible" not in panel.columns:
        return result
    for column, ticker in enumerate(tickers):
        rows = panel.loc[panel["ticker"].eq(ticker)].dropna(subset=["effective_date"])
        if rows.empty:
            continue
        effective_dates = pd.DatetimeIndex(rows["effective_date"]).values
        values = rows["fa_eligible"].fillna(False).astype(bool).to_numpy()
        positions = np.searchsorted(effective_dates, dates.values, side="right") - 1
        valid = positions >= 0
        result[valid, column] = values[positions[valid]]
    return result


def simulate_portfolio(
    m: dict[str, np.ndarray],
    fa_score: np.ndarray,
    config: dict[str, Any],
    cost_rate: float = 0.0015,
) -> dict[str, np.ndarray]:
    """Rank entries into vacant slots; fill next close with explicit cash and costs."""
    score, condition_count = unified_scores(
        m,
        fa_score,
        float(config["fa_weight"]),
        float(config["minimum_volume_ratio"]),
        config.get("component_weights"),
    )
    entry_score = float(config["entry_score"])
    exit_score = float(config.get("exit_score", entry_score - 20))
    top_n = int(config.get("max_positions", 20))
    minimum_holding_days = int(config.get("minimum_holding_days", 20))
    cooldown_days = int(config.get("cooldown_days", 5))
    volatility_target = float(config.get("volatility_target_daily", 0.0))
    participation_limit = float(config.get("max_adv_participation", 0.0))
    portfolio_vnd = float(config.get("portfolio_vnd", 0.0))
    if volatility_target < 0 or participation_limit < 0 or portfolio_vnd < 0:
        raise ValueError("Experimental risk and execution limits must be non-negative")
    if volatility_target and "volatility20" not in m:
        raise ValueError("volatility20 is required for volatility sizing")
    if participation_limit and (portfolio_vnd <= 0 or "avg_trading_value20" not in m):
        raise ValueError("ADV participation needs a positive portfolio_vnd and avg_trading_value20")
    exit_ma = m[f"ma{int(config.get('exit_ma', 200))}"]
    liquidity = m["avg_trading_value20"] >= float(config.get("minimum_average_trading_value_20d", 5_000_000_000))
    core_ready = np.isfinite(m["r12"]) & np.isfinite(m["ma200"]) & np.isfinite(fa_score)
    bull = m["bm_px"] >= m[f"bm_ma{int(config.get('regime_ma', 100))}"]
    fa_eligible = np.asarray(m.get("fa_eligible", np.ones_like(fa_score, dtype=bool)), dtype=bool)
    if fa_eligible.shape != fa_score.shape:
        raise ValueError("fa_eligible must have the same shape as fa_score")
    entry = core_ready & fa_eligible & liquidity & bull & (score >= entry_score)
    score_exit = (~np.isfinite(score)) | (score < exit_score)
    risk_exit = m["px"] < exit_ma
    trailing_stop = float(config.get("trailing_stop_from_60d_high", 1.0))
    if trailing_stop < 1.0 and "high_close60" in m:
        risk_exit = risk_exit | (m["px"] < m["high_close60"] * (1.0 - trailing_stop))
    if bool(config.get("exit_on_market_regime", False)):
        risk_exit = risk_exit | ~bull

    selection_strength = (
        0.15 * np.nan_to_num(m["r3"], nan=-10.0)
        + 0.30 * np.nan_to_num(m["r6"], nan=-10.0)
        + 0.35 * np.nan_to_num(m["r12"], nan=-10.0)
        + 0.20 * np.nan_to_num(m["rs6"], nan=-10.0)
    )
    if top_n < 1 or not 0 <= cost_rate < 1:
        raise ValueError("max_positions must be positive and cost_rate must be in [0, 1)")
    shape = m["px"].shape
    quantity = np.zeros(shape[1])  # Fractional adjusted units; initial NAV = 1.
    marks = np.zeros(shape[1])
    held_days = np.zeros(shape[1], dtype=int)
    cooldown = np.zeros(shape[1], dtype=int)
    pending_exits = np.zeros(shape[1], dtype=bool)
    pending_buys = np.array([], dtype=int)
    positions = np.zeros(shape)
    end_positions = np.zeros(shape)
    weights = np.zeros(shape)
    portfolio_returns = np.zeros(shape[0])
    turnover = np.zeros(shape[0])
    cash_weights = np.zeros(shape[0])
    stale_positions = np.zeros(shape[0], dtype=int)
    missing_buy_quotes = np.zeros(shape[0], dtype=int)
    capacity_rejected_buys = np.zeros(shape[0], dtype=int)
    capacity_deferred_exits = np.zeros(shape[0], dtype=int)
    unfilled_exits = np.zeros(shape[0], dtype=int)
    cash, previous_nav = 1.0, 1.0
    for index in range(shape[0]):
        px = m["px"][index]
        tradable = np.isfinite(px) & (px > 0)
        if "volume" in m:
            tradable &= np.isfinite(m["volume"][index]) & (m["volume"][index] > 0)
        held = quantity > 0
        positions[index] = held  # Exposure earning today's close-to-close move.
        weights[index] = quantity * marks / previous_nav
        stale_positions[index] = int((held & ~tradable).sum())
        marks[tradable] = px[tradable]
        held_days[held] += 1
        cooldown = np.maximum(cooldown - 1, 0)

        # Execute only orders decided after the PREVIOUS session's close.
        selling = pending_exits & held & tradable
        if participation_limit and index:
            previous_adv = m["avg_trading_value20"][index - 1]
            sell_notional_vnd = quantity * px * portfolio_vnd
            selling &= np.isfinite(previous_adv) & (sell_notional_vnd <= participation_limit * previous_adv)
            capacity_deferred_exits[index] = int((pending_exits & held & tradable & ~selling).sum())
        sold_value = float(np.sum(quantity[selling] * marks[selling]))
        cash += sold_value * (1.0 - cost_rate)
        quantity[selling] = 0
        held_days[selling] = 0
        cooldown[selling] = cooldown_days
        pending_exits[selling] = False
        traded_value = sold_value
        for candidate in pending_buys:
            if quantity[candidate] > 0 or selling[candidate]:
                continue
            if not tradable[candidate]:
                missing_buy_quotes[index] += 1
                continue  # Cancel this buy; never fill using a stale quote.
            slots = top_n - int((quantity > 0).sum())
            if slots <= 0 or cash <= 0:
                break
            budget = cash / slots
            if volatility_target:
                previous_vol = m["volatility20"][index - 1, candidate]
                if not np.isfinite(previous_vol) or previous_vol <= 0:
                    continue
                budget *= min(1.0, volatility_target / previous_vol)
            if participation_limit:
                previous_adv = m["avg_trading_value20"][index - 1, candidate]
                if not np.isfinite(previous_adv) or budget * portfolio_vnd > participation_limit * previous_adv:
                    capacity_rejected_buys[index] += 1
                    continue
            if budget <= 0:
                continue
            notional = budget / (1.0 + cost_rate)
            quantity[candidate] = notional / px[candidate]
            cash -= budget
            traded_value += notional
            held_days[candidate] = 0

        nav = cash + float(np.dot(quantity, marks))
        portfolio_returns[index] = nav / previous_nav - 1.0
        turnover[index] = traded_value / previous_nav  # Both buy and sell notionals.
        cash_weights[index] = cash / nav
        previous_nav = nav
        held = quantity > 0
        end_positions[index] = held

        # Today's closing features may only place orders for the next close.
        pending_exits |= held & tradable & (
            risk_exit[index] | ((held_days >= minimum_holding_days) & score_exit[index])
        )
        unfilled_exits[index] = int(pending_exits.sum())
        candidates = np.flatnonzero(entry[index] & tradable & ~held & (cooldown == 0))
        ranked = np.lexsort((candidates, -selection_strength[index, candidates], -score[index, candidates]))
        pending_buys = candidates[ranked]
    return {
        "returns": portfolio_returns,
        "turnover": turnover,
        "positions": positions.astype(float),
        "end_positions": end_positions,
        "cash_weights": cash_weights,
        "stale_positions": stale_positions,
        "missing_buy_quotes": missing_buy_quotes,
        "capacity_rejected_buys": capacity_rejected_buys,
        "capacity_deferred_exits": capacity_deferred_exits,
        "unfilled_exits": unfilled_exits,
        "weights": weights,
        "score": score,
        "condition_count": condition_count,
        "selection_strength": selection_strength,
    }
