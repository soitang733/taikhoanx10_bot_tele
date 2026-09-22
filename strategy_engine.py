"""Build explainable FA/TA signals from the local analysis database.

The formulas follow the supplied VNStock specification. Missing inputs stay
missing: the engine never invents a score or silently substitutes raw prices
for a required adjusted-price series.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from model_portfolio import apply_model_portfolio, empty_state


FA_GROUP_FACTORS = {
    "quality": ["roe", "roa", "roic", "profit_margin", "cash_flow_quality", "earnings_stability"],
    "growth": ["revenue_cagr_3y", "eps_cagr_3y", "profit_growth", "margin_expansion_3y"],
    "value": ["pe", "pb", "earnings_yield", "ev_ebitda", "sector_relative_value"],
    "safety": ["debt_equity", "net_debt_ebitda", "interest_coverage", "current_ratio"],
}


def safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    result = pd.to_numeric(numerator, errors="coerce") / pd.to_numeric(denominator, errors="coerce")
    return result.replace([np.inf, -np.inf], np.nan)


def cagr(current: pd.Series, previous: pd.Series, years: float) -> pd.Series:
    ratio = safe_divide(current, previous)
    valid = (pd.to_numeric(current, errors="coerce") > 0) & (pd.to_numeric(previous, errors="coerce") > 0)
    return (ratio.where(valid).pow(1.0 / years) - 1.0).replace([np.inf, -np.inf], np.nan)


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def read_table(connection: sqlite3.Connection, name: str) -> pd.DataFrame:
    return pd.read_sql_query(f"SELECT * FROM {name}", connection) if table_exists(connection, name) else pd.DataFrame()


def atomic_replace(source: Path, destination: Path) -> None:
    last_error: OSError | None = None
    for attempt in range(10):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.2 * (attempt + 1))
    if last_error:
        raise last_error


def bucket_score(series: pd.Series, rules: list[tuple[Callable[[pd.Series], pd.Series], float]]) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    result = pd.Series(np.nan, index=values.index, dtype="float64")
    for predicate, score in rules:
        mask = values.notna() & result.isna() & predicate(values)
        result.loc[mask] = score
    return result


def fa_factor_scores(metrics: pd.DataFrame) -> pd.DataFrame:
    scores = pd.DataFrame(index=metrics.index)
    ge = lambda a: (lambda x: x >= a)
    between = lambda a, b: (lambda x: (x >= a) & (x < b))
    le = lambda a: (lambda x: x <= a)
    gt_le = lambda a, b: (lambda x: (x > a) & (x <= b))
    scores["roe"] = bucket_score(metrics["roe"], [(ge(.20), 100), (between(.15, .20), 80), (between(.10, .15), 50), (lambda x: x < .10, 0)])
    scores["roa"] = bucket_score(metrics["roa"], [(ge(.10), 100), (between(.07, .10), 75), (between(.05, .07), 50), (lambda x: x < .05, 0)])
    scores["roic"] = bucket_score(metrics["roic"], [(ge(.15), 100), (between(.10, .15), 70), (lambda x: x < .10, 20)])
    scores["profit_margin"] = bucket_score(metrics["profit_margin"], [(ge(.15), 100), (between(.10, .15), 75), (between(.05, .10), 40), (lambda x: x < .05, 0)])
    scores["cash_flow_quality"] = bucket_score(metrics["cash_flow_quality"], [(ge(1), 100), (between(.7, 1), 70), (lambda x: (x > 0) & (x < .7), 30), (lambda x: x <= 0, 0)])
    scores["earnings_stability"] = bucket_score(metrics["earnings_stability"], [(le(.2), 100), (gt_le(.2, .5), 60), (lambda x: x > .5, 20)])
    for factor in ("revenue_cagr_3y", "eps_cagr_3y"):
        scores[factor] = bucket_score(metrics[factor], [(ge(.20), 100), (between(.12, .20), 75), (between(.05, .12), 40), (lambda x: x < .05, 0)])
    scores["profit_growth"] = bucket_score(metrics["profit_growth"], [(ge(.25), 100), (between(.15, .25), 75), (between(0, .15), 40), (lambda x: x < 0, 0)])
    scores["margin_expansion_3y"] = bucket_score(metrics["margin_expansion_3y"], [(lambda x: x > .02, 100), (lambda x: (x >= 0) & (x <= .02), 60), (lambda x: x < 0, 0)])
    scores["pe"] = bucket_score(metrics["pe"], [(lambda x: (x > 0) & (x <= 10), 100), (gt_le(10, 15), 75), (gt_le(15, 22), 40), (lambda x: (x <= 0) | (x > 22), 0)])
    scores["pb"] = bucket_score(metrics["pb"], [(lambda x: (x > 0) & (x <= 1.2), 100), (gt_le(1.2, 2), 70), (gt_le(2, 3.5), 30), (lambda x: (x <= 0) | (x > 3.5), 0)])
    scores["earnings_yield"] = bucket_score(metrics["earnings_yield"], [(ge(.10), 100), (between(.065, .10), 70), (lambda x: x < .065, 20)])
    scores["ev_ebitda"] = bucket_score(metrics["ev_ebitda"], [(le(6), 100), (gt_le(6, 10), 70), (lambda x: x > 10, 20)])
    scores["sector_relative_value"] = bucket_score(metrics["sector_relative_value"], [(le(.8), 100), (gt_le(.8, 1), 75), (gt_le(1, 1.2), 40), (lambda x: x > 1.2, 20)])
    scores["debt_equity"] = bucket_score(metrics["debt_equity"], [(le(.5), 100), (gt_le(.5, 1), 75), (gt_le(1, 2), 40), (lambda x: x > 2, 0)])
    scores["net_debt_ebitda"] = bucket_score(metrics["net_debt_ebitda"], [(le(1.5), 100), (gt_le(1.5, 3), 60), (lambda x: x > 3, 0)])
    scores["interest_coverage"] = bucket_score(metrics["interest_coverage"], [(ge(5), 100), (between(3, 5), 70), (between(1.5, 3), 30), (lambda x: x < 1.5, 0)])
    scores["current_ratio"] = bucket_score(metrics["current_ratio"], [(ge(1.5), 100), (between(1, 1.5), 60), (lambda x: x < 1, 0)])
    return scores


def build_fa(companies: pd.DataFrame, annual: pd.DataFrame, snapshot: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    base = companies.copy()
    base["ticker"] = base["ticker"].astype(str).str.upper()
    if annual.empty:
        base["fa_score"], base["fa_coverage"] = np.nan, 0.0
        base["fa_status"], base["fa_pass"], base["hard_reject_reason"] = "INSUFFICIENT_DATA", False, "missing annual financials"
        return base
    annual = annual.copy()
    annual["ticker"] = annual["ticker"].astype(str).str.upper()
    annual["period_end"] = pd.to_datetime(annual["period_end"], errors="coerce")
    annual = annual.sort_values(["ticker", "period_end"])
    latest = annual.groupby("ticker", observed=True).tail(1).set_index("ticker")
    def previous_year(offset: int) -> pd.DataFrame:
        # nth(-2/-4) retains row indexes and may skip missing fiscal years.
        # Align by ticker AND the actual year relative to each latest report.
        dated = annual.assign(_year=annual["period_end"].dt.year)
        dated = dated.drop_duplicates(["ticker", "_year"], keep="last").set_index(["ticker", "_year"])
        target = pd.MultiIndex.from_arrays([latest.index, latest["period_end"].dt.year - offset], names=["ticker", "_year"])
        result = dated.reindex(target)
        result.index = latest.index
        return result

    prior1, prior3 = previous_year(1), previous_year(3)
    metrics = pd.DataFrame(index=pd.Index(base["ticker"], name="ticker"))

    def col(frame: pd.DataFrame, name: str) -> pd.Series:
        return pd.to_numeric(frame[name], errors="coerce").reindex(metrics.index) if name in frame else pd.Series(index=metrics.index, dtype="float64")

    net_income, equity, assets, revenue = col(latest, "net_income"), col(latest, "equity"), col(latest, "total_assets"), col(latest, "revenue")
    operating_cf, liabilities, cash = col(latest, "operating_cash_flow"), col(latest, "total_liabilities"), col(latest, "cash_and_cash_equivalents")
    debt = col(latest, "short_term_borrowings") + col(latest, "long_term_borrowings")
    ebit = col(latest, "ebit")
    ebitda = col(latest, "is_ebitda").combine_first(ebit + col(latest, "depreciation") + col(latest, "amortization"))
    avg_equity, avg_assets = (equity + col(prior1, "equity")) / 2, (assets + col(prior1, "total_assets")) / 2
    pbt = col(latest, "profit_before_tax")
    effective_tax = safe_divide(pbt - net_income, pbt).clip(0, .35).fillna(.20)
    metrics["roe"], metrics["roa"] = safe_divide(net_income, avg_equity), safe_divide(net_income, avg_assets)
    metrics["roic"] = safe_divide(ebit * (1 - effective_tax), equity + debt - cash)
    metrics["profit_margin"], metrics["cash_flow_quality"] = safe_divide(net_income, revenue), safe_divide(operating_cf, net_income)
    metrics["revenue_cagr_3y"], metrics["eps_cagr_3y"] = cagr(revenue, col(prior3, "revenue"), 3), cagr(col(latest, "eps"), col(prior3, "eps"), 3)
    metrics["profit_growth"] = safe_divide(net_income, col(prior1, "net_income")) - 1
    metrics["margin_expansion_3y"] = safe_divide(col(latest, "gross_profit"), revenue) - safe_divide(col(prior3, "gross_profit"), col(prior3, "revenue"))
    metrics["debt_equity"], metrics["net_debt_ebitda"] = safe_divide(debt, equity), safe_divide(debt - cash, ebitda)
    metrics["interest_coverage"] = safe_divide(ebit, col(latest, "interest_expense").abs())
    metrics["current_ratio"] = safe_divide(col(latest, "current_assets"), col(latest, "current_liabilities"))

    def stability(group: pd.DataFrame) -> float:
        values = pd.to_numeric(group.get("net_income"), errors="coerce").dropna().tail(5)
        mean = values.mean()
        return float(values.std(ddof=0) / abs(mean)) if len(values) >= 3 and mean != 0 else np.nan

    metrics["earnings_stability"] = annual.groupby("ticker", observed=True).apply(stability, include_groups=False).reindex(metrics.index)
    snap = pd.DataFrame(index=metrics.index)
    if not snapshot.empty:
        snapshot = snapshot.copy()
        snapshot["ticker"] = snapshot["ticker"].astype(str).str.upper()
        snap = snapshot.sort_values("as_of_utc").drop_duplicates("ticker", keep="last").set_index("ticker").reindex(metrics.index)
    company_index = base.set_index("ticker")
    market_cap = pd.to_numeric(snap["market_cap"], errors="coerce") if "market_cap" in snap else col(company_index, "market_cap")
    metrics["earnings_yield"] = safe_divide(net_income, market_cap)
    snap_pe = pd.to_numeric(snap["pe"], errors="coerce") if "pe" in snap else pd.Series(index=metrics.index, dtype=float)
    snap_pb = pd.to_numeric(snap["pb"], errors="coerce") if "pb" in snap else pd.Series(index=metrics.index, dtype=float)
    metrics["pe"], metrics["pb"] = snap_pe.combine_first(safe_divide(market_cap, net_income)), snap_pb.combine_first(safe_divide(market_cap, equity))
    metrics["ev_ebitda"] = safe_divide(market_cap + debt - cash, ebitda)
    sector = company_index.get("sector", pd.Series(index=metrics.index, dtype="object")).reindex(metrics.index)
    metrics["sector_relative_value"] = safe_divide(metrics["pe"], metrics["pe"].where(metrics["pe"] > 0).groupby(sector).transform("median"))

    scores = fa_factor_scores(metrics)
    for factor in scores:
        metrics[f"{factor}_score"] = scores[factor]
    weighted_total, available_groups = pd.Series(0.0, index=metrics.index), pd.Series(0.0, index=metrics.index)
    for group, factors in FA_GROUP_FACTORS.items():
        metrics[f"{group}_score"] = scores[factors].mean(axis=1, skipna=True)
        metrics[f"{group}_coverage"] = scores[factors].notna().mean(axis=1)
        weight = float(config["fa_group_weights"][group])
        available = metrics[f"{group}_score"].notna()
        weighted_total += metrics[f"{group}_score"].fillna(0) * weight
        available_groups += available.astype(float) * weight
    metrics["fa_score"] = weighted_total / available_groups.replace(0, np.nan)
    metrics["fa_coverage"] = sum(metrics[f"{g}_coverage"] * float(config["fa_group_weights"][g]) for g in FA_GROUP_FACTORS)
    prior_cfo = col(prior1, "operating_cash_flow")
    status = company_index.get("trading_status", pd.Series(index=metrics.index, dtype="object")).astype("string").str.lower()
    restricted = status.str.contains("hạn chế|đình chỉ|restricted|suspend|control", regex=True, na=False)
    reasons = pd.Series("", index=metrics.index, dtype="string")
    for mask, reason in [(net_income.le(0), "net_income<=0"), (equity.le(0), "equity<=0"), (restricted, "restricted_trading"), (operating_cf.lt(0) & prior_cfo.lt(0), "cfo_negative_2y")]:
        reasons = reasons.mask(mask & reasons.eq(""), reason)
    metrics["hard_reject_reason"] = reasons
    metrics["fa_status"] = np.where(metrics["fa_coverage"] >= config["minimum_fa_coverage"], "READY", "INSUFFICIENT_DATA")
    metrics["fa_pass"] = metrics["fa_status"].eq("READY") & metrics["fa_score"].ge(config["buy_fa_score"]) & reasons.eq("")
    return base.merge(metrics.reset_index(), on="ticker", how="left")


def build_signal_fa(companies: pd.DataFrame, annual: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    """Use the same historically available FA fields as the backtest.

    Current market-cap and valuation snapshots remain in the raw database for
    display, but cannot enter a score whose historical counterpart lacks them.
    """
    scoring_companies = companies.copy()
    scoring_companies["market_cap"] = np.nan
    return build_fa(scoring_companies, annual, pd.DataFrame(), config)


def aligned_calendar_return(stock: pd.Series, benchmark: pd.Series, end: pd.Timestamp, months: int) -> tuple[float, float]:
    common_dates = stock.index.intersection(benchmark.index)
    common_dates = common_dates[common_dates <= end]
    if common_dates.empty:
        return np.nan, np.nan
    common_end = common_dates[-1]
    eligible_start = common_dates[common_dates <= common_end - pd.DateOffset(months=months)]
    if eligible_start.empty:
        return np.nan, np.nan
    common_start = eligible_start[-1]
    stock_start, stock_end = stock.get(common_start, np.nan), stock.get(common_end, np.nan)
    bench_start, bench_end = benchmark.get(common_start, np.nan), benchmark.get(common_end, np.nan)
    stock_return = stock_end / stock_start - 1 if pd.notna(stock_start) and stock_start > 0 else np.nan
    bench_return = bench_end / bench_start - 1 if pd.notna(bench_start) and bench_start > 0 else np.nan
    return float(stock_return), float(bench_return)


def build_ta(prices: pd.DataFrame, benchmark: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    prices = prices.loc[prices["analysis_ready"].astype(bool)].copy()
    prices["date"] = pd.to_datetime(prices["date"], errors="coerce")
    prices["analysis_price"] = pd.to_numeric(prices["adjusted_close"], errors="coerce")
    benchmark = benchmark.loc[benchmark["analysis_ready"].astype(bool)].copy() if not benchmark.empty else benchmark.copy()
    benchmark["date"] = pd.to_datetime(benchmark.get("date"), errors="coerce")
    benchmark["analysis_price"] = pd.to_numeric(benchmark.get("adjusted_close"), errors="coerce")
    benchmark_series = benchmark.dropna(subset=["date", "analysis_price"]).drop_duplicates("date", keep="last").set_index("date")["analysis_price"].sort_index()
    regime_ma = int(config.get("regime_ma", 100))
    market_bull = bool(len(benchmark_series) >= regime_ma and benchmark_series.iloc[-1] >= benchmark_series.tail(regime_ma).mean())
    latest_market_date = benchmark_series.index[-1] if not benchmark_series.empty else pd.NaT
    rows: list[dict[str, Any]] = []
    for ticker, group in prices.dropna(subset=["date"]).sort_values("date").groupby("ticker", observed=True):
        group = group.drop_duplicates("date", keep="last").set_index("date").sort_index()
        price = group["analysis_price"].dropna()
        if price.empty:
            continue
        end = price.index[-1]
        returns = {m: aligned_calendar_return(price, benchmark_series, end, m) for m in (3, 6, 12)}
        previous = group.loc[group.index < end]
        previous_prices = pd.to_numeric(previous["analysis_price"], errors="coerce").dropna()
        previous_volumes = pd.to_numeric(previous["volume"], errors="coerce").dropna()
        current_volume = pd.to_numeric(pd.Series([group.loc[end, "volume"]]), errors="coerce").iloc[0]
        avg_vol20 = float(previous_volumes.tail(20).mean()) if len(previous_volumes) >= 20 else np.nan
        high_close20 = float(previous_prices.tail(20).max()) if len(previous_prices) >= 20 else np.nan
        high_close60 = float(previous_prices.tail(60).max()) if len(previous_prices) >= 60 else np.nan
        current = float(price.iloc[-1])
        row: dict[str, Any] = {
            "ticker": str(ticker).upper(), "signal_date": end.strftime("%Y-%m-%d"), "price": current, "close": current,
            "sessions": len(price), "r_3m": returns[3][0], "r_6m": returns[6][0], "r_12m": returns[12][0],
            "return_3m": returns[3][0], "return_6m": returns[6][0], "return_12m": returns[12][0],
            "vnindex_r_6m": returns[6][1], "rs_6m": returns[6][0] - returns[6][1], "relative_strength_6m": returns[6][0] - returns[6][1],
            "ma20": float(price.tail(20).mean()) if len(price) >= 20 else np.nan,
            "ma50": float(price.tail(50).mean()) if len(price) >= 50 else np.nan,
            "ma100": float(price.tail(100).mean()) if len(price) >= 100 else np.nan,
            "ma200": float(price.tail(200).mean()) if len(price) >= 200 else np.nan,
            "volume": float(current_volume) if pd.notna(current_volume) else np.nan,
            "avg_vol20": avg_vol20, "avg_volume_20d": avg_vol20,
            "volume_ratio": current_volume / avg_vol20 if pd.notna(avg_vol20) and avg_vol20 > 0 else np.nan,
            "high_close20": high_close20,
            "high_close60": high_close60,
            "avg_trading_value_20d": float(pd.to_numeric(previous["trading_value"], errors="coerce").tail(20).mean()) if len(previous) >= 20 else np.nan,
            "market_bull": market_bull,
            "price_fresh": bool(pd.notna(latest_market_date) and end == latest_market_date),
        }
        trend_ma = int(config.get("trend_ma", 200))
        trend_ma_key = f"ma{trend_ma}"
        required = ["r_3m", "r_6m", "r_12m", "rs_6m", "ma50", trend_ma_key, "volume_ratio", "high_close20"]
        row["missing_data_reason"] = ", ".join(name for name in required if pd.isna(row.get(name)))
        row["momentum_pass"] = all(pd.notna(row[name]) and row[name] > 0 for name in ("r_3m", "r_6m", "r_12m"))
        row["rs_pass"] = pd.notna(row["rs_6m"]) and row["rs_6m"] > 0
        trend_ma_value = row.get(trend_ma_key)
        row["trend_pass"] = pd.notna(row["ma50"]) and pd.notna(trend_ma_value) and current > row["ma50"] > trend_ma_value
        row["breakout_pass"] = pd.notna(high_close20) and current > high_close20
        row["volume_pass"] = pd.notna(row["volume_ratio"]) and row["volume_ratio"] >= float(config.get("minimum_volume_ratio", 1.5))
        min_conditions = int(config.get("min_buy_conditions", 5))
        condition_flags = ["momentum_pass", "rs_pass", "trend_pass", "breakout_pass", "volume_pass"]
        conditions_met = sum(bool(row[name]) for name in condition_flags)
        row["buy_pass"] = conditions_met >= min_conditions and not row["missing_data_reason"]
        raw_weights = config.get("component_weights", {})
        weights = {
            "momentum_pass": float(raw_weights.get("momentum", 1.0)),
            "rs_pass": float(raw_weights.get("relative_strength", 1.0)),
            "trend_pass": float(raw_weights.get("trend", 1.0)),
            "breakout_pass": float(raw_weights.get("breakout", 1.0)),
            "volume_pass": float(raw_weights.get("volume", 1.0)),
        }
        total_weight = sum(max(0.0, weight) for weight in weights.values()) or 1.0
        row["ta_score"] = 100.0 * sum(
            max(0.0, weights[name]) * bool(row[name]) for name in condition_flags
        ) / total_weight
        row["ta_status"] = "READY" if not row["missing_data_reason"] else "DATA_REVIEW"
        rows.append(row)
    return pd.DataFrame(rows)


def combine(fa: pd.DataFrame, ta: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    combined = fa.merge(ta, on="ticker", how="left")
    # Portfolio ownership is applied later from persisted model state. The
    # market-wide screen itself must never depend on a hand-maintained list.
    combined["is_held"] = False
    ta_ready = combined.get("ta_status", pd.Series("DATA_REVIEW", index=combined.index)).eq("READY")

    # Unified score parameters
    fa_weight = float(config.get("fa_weight", 0.25))
    entry_score = float(config.get("entry_score", 60))
    exit_score = float(config.get("exit_score", 40))
    exit_ma_number = int(config.get("exit_ma", 200))
    max_positions = int(config.get("max_positions", 30))

    # Effective FA: shrink toward neutral (50) based on coverage; hard reject → 0
    raw_fa = pd.to_numeric(combined.get("fa_score"), errors="coerce").fillna(50)
    fa_cov = pd.to_numeric(combined.get("fa_coverage"), errors="coerce").fillna(0).clip(0, 1)
    effective_fa = 50.0 + (raw_fa - 50.0) * fa_cov
    hard_reject = combined.get("hard_reject_reason", pd.Series("", index=combined.index)).fillna("").astype(str).ne("")
    effective_fa = effective_fa.where(~hard_reject, 0.0)
    combined["effective_fa"] = effective_fa

    # Unified score = fa_weight * effective_fa + (1 - fa_weight) * ta_score
    ta_score = pd.to_numeric(combined.get("ta_score"), errors="coerce").fillna(0)
    combined["unified_score"] = fa_weight * effective_fa + (1 - fa_weight) * ta_score
    combined["selection_strength"] = (
        0.15 * pd.to_numeric(combined.get("r_3m"), errors="coerce").fillna(-10)
        + 0.30 * pd.to_numeric(combined.get("r_6m"), errors="coerce").fillna(-10)
        + 0.35 * pd.to_numeric(combined.get("r_12m"), errors="coerce").fillna(-10)
        + 0.20 * pd.to_numeric(combined.get("rs_6m"), errors="coerce").fillna(-10)
    )

    # Exit / trend MA
    exit_ma_key = f"ma{exit_ma_number}"
    exit_ma_col = combined[exit_ma_key] if exit_ma_key in combined.columns else combined.get("ma200", pd.Series(np.nan, index=combined.index))

    has_exit_data = combined["price"].notna() & exit_ma_col.notna()
    combined["has_exit_data"] = has_exit_data
    liquid = pd.to_numeric(combined.get("avg_trading_value_20d"), errors="coerce").ge(
        float(config.get("minimum_average_trading_value_20d", 5_000_000_000))
    )
    market_bull = combined.get("market_bull", pd.Series(False, index=combined.index)).fillna(False).astype(bool)
    price_fresh = combined.get("price_fresh", pd.Series(False, index=combined.index)).fillna(False).astype(bool)
    candidate = (
        combined["unified_score"].ge(entry_score)
        & ~hard_reject
        & ta_ready
        & liquid
        & market_bull
        & price_fresh
    )
    combined["unified_rank"] = np.nan
    ranked_index = combined.loc[candidate].sort_values(
        ["unified_score", "selection_strength"], ascending=[False, False]
    ).index
    combined.loc[ranked_index, "unified_rank"] = np.arange(1, len(ranked_index) + 1)
    unified_buy = candidate & combined["unified_rank"].le(max_positions)
    combined["exit_ma_trigger"] = has_exit_data & combined["price"].lt(exit_ma_col)
    combined["exit_score_trigger"] = combined["unified_score"].lt(exit_score)
    combined["exit_trailing_trigger"] = False
    trailing_stop = float(config.get("trailing_stop_from_60d_high", 1.0))
    if trailing_stop < 1.0:
        combined["exit_trailing_trigger"] = combined["price"].lt(
            pd.to_numeric(combined.get("high_close60"), errors="coerce") * (1.0 - trailing_stop)
        )
    combined["exit_market_trigger"] = bool(config.get("exit_on_market_regime", False)) & ~market_bull
    combined["exit_risk_trigger"] = (
        combined["exit_ma_trigger"] | combined["exit_trailing_trigger"] | combined["exit_market_trigger"]
    )
    combined["entry_eligible"] = candidate
    held_exit = has_exit_data & (combined["exit_risk_trigger"] | combined["exit_score_trigger"])

    conditions = [
        combined["is_held"] & (~has_exit_data | ~price_fresh),
        combined["is_held"] & held_exit,
        combined["is_held"] & has_exit_data & ~held_exit,
        ~combined["is_held"] & ~ta_ready,
        ~combined["is_held"] & unified_buy,
    ]
    combined["final_action"] = np.select(
        conditions,
        ["DATA_REVIEW", "EXIT", "HOLD", "DATA_REVIEW", "BUY"],
        default="WATCH",
    )
    combined["watch_reason"] = np.select(
        [~ta_ready, ~price_fresh, hard_reject, ~market_bull, ~liquid,
         combined["unified_score"].lt(entry_score), candidate & ~unified_buy],
        ["MISSING_TA", "STALE_PRICE", "FA_HARD_REJECT", "MARKET_DEFENSIVE", "ILLIQUID",
         "SCORE_BELOW_ENTRY", "OUTSIDE_TOP_N"],
        default="NONE",
    )
    combined["decision_reason"] = (
        "FA=" + combined["fa_score"].round(1).astype("string").fillna("NA")
        + "; coverage=" + (combined["fa_coverage"] * 100).round(0).astype("string").fillna("0") + "%"
        + "; price_volume=" + combined["ta_score"].round(1).astype("string").fillna("0")
        + "; unified=" + combined["unified_score"].round(1).astype("string").fillna("NA")
        + f"; entry={entry_score}; exit={exit_score}; top={max_positions}"
        + "; liquid=" + liquid.astype(str)
        + "; market_bull=" + market_bull.astype(str)
        + "; price_fresh=" + price_fresh.astype(str)
        + "; reason=" + combined["watch_reason"].astype(str)
        + combined["hard_reject_reason"].fillna("").map(lambda x: f"; reject={x}" if x else "")
        + combined["missing_data_reason"].fillna("").map(lambda x: f"; missing={x}" if x else "")
    )
    combined["market_context"] = np.where(market_bull, "BULL_ABOVE_REGIME_MA", "DEFENSIVE_BELOW_REGIME_MA")
    combined["generated_at_utc"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    action_order = pd.Categorical(combined["final_action"], ["BUY", "HOLD", "WATCH", "DATA_REVIEW", "EXIT"], ordered=True)
    return combined.assign(_order=action_order).sort_values(["_order", "unified_score", "fa_score", "ta_score"], ascending=[True, False, False, False]).drop(columns="_order").reset_index(drop=True)


def load_model_state(database_path: Path) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame]:
    state = empty_state()
    event_columns = ["signal_date", "ticker", "action", "reason_code", "unified_score", "holding_sessions"]
    run_columns = ["signal_date", "generated_at_utc", "position_count", "buy_count", "exit_count"]
    if not database_path.exists():
        return state, pd.DataFrame(columns=event_columns), pd.DataFrame(columns=run_columns)
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            positions = read_table(connection, "model_positions")
            pending = read_table(connection, "model_pending_orders")
            cooldowns = read_table(connection, "model_cooldowns")
            events = read_table(connection, "model_events")
            runs = read_table(connection, "model_runs")
    except sqlite3.DatabaseError:
        return state, pd.DataFrame(columns=event_columns), pd.DataFrame(columns=run_columns)
    for row in positions.to_dict("records"):
        state["positions"][str(row["ticker"])] = {
            "entry_signal_date": str(row.get("entry_signal_date") or ""),
            "entry_date": str(row.get("entry_date") or ""),
            "holding_sessions": int(row.get("holding_sessions") or 0),
        }
    for row in pending.to_dict("records"):
        state["pending_orders"][str(row["ticker"])] = {
            "side": str(row.get("side") or ""), "signal_date": str(row.get("signal_date") or ""),
        }
    for row in cooldowns.to_dict("records"):
        state["cooldowns"][str(row["ticker"])] = int(row.get("remaining_sessions") or 0)
    if not runs.empty and "signal_date" in runs:
        state["last_signal_date"] = str(runs["signal_date"].max())[:10]
    return state, events.reindex(columns=event_columns), runs.reindex(columns=run_columns)


def write_model_state(connection: sqlite3.Connection, state: dict[str, Any], events: pd.DataFrame,
                      prior_events: pd.DataFrame, prior_runs: pd.DataFrame,
                      signal_date: str, generated_at: str, signals: pd.DataFrame) -> None:
    position_rows = [{"ticker": ticker, **value} for ticker, value in state["positions"].items()]
    pending_rows = [{"ticker": ticker, **value} for ticker, value in state["pending_orders"].items()]
    cooldown_rows = [{"ticker": ticker, "remaining_sessions": value}
                     for ticker, value in state["cooldowns"].items() if int(value) > 0]
    pd.DataFrame(position_rows, columns=["ticker", "entry_signal_date", "entry_date", "holding_sessions"]).to_sql(
        "model_positions", connection, if_exists="replace", index=False)
    pd.DataFrame(pending_rows, columns=["ticker", "side", "signal_date"]).to_sql(
        "model_pending_orders", connection, if_exists="replace", index=False)
    pd.DataFrame(cooldown_rows, columns=["ticker", "remaining_sessions"]).to_sql(
        "model_cooldowns", connection, if_exists="replace", index=False)
    event_history = prior_events.loc[prior_events["signal_date"].astype(str).str[:10].ne(signal_date)]
    event_history = pd.concat([event_history, events], ignore_index=True)
    event_history.to_sql("model_events", connection, if_exists="replace", index=False)
    run_history = prior_runs.loc[prior_runs["signal_date"].astype(str).str[:10].ne(signal_date)]
    run = pd.DataFrame([{
        "signal_date": signal_date, "generated_at_utc": generated_at,
        "position_count": len(state["positions"]),
        "buy_count": int(signals["final_action"].eq("BUY").sum()),
        "exit_count": int(signals["final_action"].eq("EXIT").sum()),
    }])
    pd.concat([run_history, run], ignore_index=True).to_sql(
        "model_runs", connection, if_exists="replace", index=False)



def build(database_path: Path, output_dir: Path, config_path: Path) -> dict[str, Any]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    with closing(sqlite3.connect(database_path)) as connection:
        companies, annual = read_table(connection, "companies"), read_table(connection, "financial_annual_wide")
        prices, benchmark = read_table(connection, "price_daily"), read_table(connection, "benchmark_daily")
    signals = combine(build_signal_fa(companies, annual, config), build_ta(prices, benchmark, config), config)
    current_date = str(signals["signal_date"].max())[:10]
    state_path = output_dir / "signals.sqlite"
    state, prior_events, prior_runs = load_model_state(state_path)
    trading_dates = pd.to_datetime(
        benchmark.loc[benchmark["analysis_ready"].astype(bool), "date"], errors="coerce"
    ).dropna().dt.strftime("%Y-%m-%d").tolist()
    signals, state, events = apply_model_portfolio(
        signals, config, state, current_date, trading_dates
    )
    signals["fa_score_basis"] = "ANNUAL_ONLY_ASSUMED_REPORTING_LAG"
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    output_dir.mkdir(parents=True, exist_ok=True)
    parquet_temp, parquet_path = output_dir / "signals_latest.parquet.tmp", output_dir / "signals_latest.parquet"
    signals.to_parquet(parquet_temp, index=False, compression="zstd")
    atomic_replace(parquet_temp, parquet_path)
    database_temp, database_final = output_dir / "signals.sqlite.tmp", output_dir / "signals.sqlite"
    database_temp.unlink(missing_ok=True)
    connection = sqlite3.connect(database_temp)
    try:
        signals.to_sql("signals_latest", connection, if_exists="replace", index=False)
        write_model_state(connection, state, events, prior_events, prior_runs,
                          current_date, generated_at, signals)
        connection.execute("CREATE UNIQUE INDEX idx_signals_ticker ON signals_latest(ticker)")
        connection.execute("CREATE INDEX idx_signals_action ON signals_latest(final_action, fa_score, ta_score)")
        connection.execute("CREATE UNIQUE INDEX idx_model_positions_ticker ON model_positions(ticker)")
        connection.execute("CREATE UNIQUE INDEX idx_model_pending_ticker ON model_pending_orders(ticker)")
        connection.execute("CREATE UNIQUE INDEX idx_model_cooldowns_ticker ON model_cooldowns(ticker)")
        connection.execute("CREATE UNIQUE INDEX idx_model_events_date_ticker ON model_events(signal_date,ticker)")
        connection.execute("CREATE UNIQUE INDEX idx_model_runs_date ON model_runs(signal_date)")
        connection.commit()
    finally:
        connection.close()
    atomic_replace(database_temp, database_final)
    return {
        "generated_at_utc": generated_at, "signal_date": current_date, "tickers": len(signals),
        "actions": signals["final_action"].value_counts(dropna=False).to_dict(), "fa_ready": int(signals["fa_status"].eq("READY").sum()),
        "fa_pass": int(signals["fa_pass"].sum()), "ta_ready": int(signals["ta_status"].eq("READY").sum()),
        "model_positions": len(state["positions"]), "pending_orders": len(state["pending_orders"]),
    }


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    print(json.dumps(build(root / "analysis_data/stocks_analysis.sqlite", root / "analysis_data", root / "strategy_config.json"), ensure_ascii=False, indent=2))
