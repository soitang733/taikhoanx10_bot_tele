"""Optimize one unified FA + price/volume strategy without validation leakage."""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from backtest_engine import build_ta_history
from unified_strategy import (
    SIMULATION_VERSION, fa_eligibility_matrix, fa_matrix,
    historical_fa_panel, simulate_portfolio,
)


def metrics(returns: np.ndarray, turnover: np.ndarray, mask: np.ndarray) -> dict[str, float]:
    r = np.nan_to_num(returns[mask], nan=0.0)
    t = np.nan_to_num(turnover[mask], nan=0.0)
    years = len(r) / 252.0
    equity = np.cumprod(1.0 + r)
    cagr = equity[-1] ** (1.0 / years) - 1.0 if years > 0 and len(equity) and equity[-1] > 0 else -1.0
    volatility = np.std(r)
    sharpe = math.sqrt(252) * np.mean(r) / volatility if volatility > 0 else 0.0
    drawdown = equity / np.maximum(1.0, np.maximum.accumulate(equity)) - 1.0
    return {
        "cagr": float(cagr), "sharpe": float(sharpe),
        "maximum_drawdown": float(np.min(drawdown)) if len(drawdown) else 0.0,
        "annual_turnover": float(np.sum(t) / years) if years > 0 else 0.0,
    }


def training_objective(
    train: dict[str, float],
    benchmark: dict[str, float],
    folds: list[dict[str, float]],
    benchmark_folds: list[dict[str, float]],
    average_positions: float,
) -> float:
    """Reward stable, benchmark-relative training results without future leakage."""
    if train["annual_turnover"] > 18 or average_positions < 5:
        return -100.0 - train["annual_turnover"]
    fold_sharpes = np.asarray([item["sharpe"] for item in folds], dtype=float)
    fold_alpha = np.asarray(
        [item["cagr"] - base["cagr"] for item, base in zip(folds, benchmark_folds)],
        dtype=float,
    )
    worst_drawdown = min(item["maximum_drawdown"] for item in folds)
    return (
        0.45 * float(fold_sharpes.mean())
        + 0.25 * float(fold_sharpes.min())
        + 2.00 * float(fold_alpha.mean())
        + 1.00 * float(fold_alpha.min())
        + 0.50 * (train["cagr"] - benchmark["cagr"])
        - 0.50 * abs(worst_drawdown)
        - 0.02 * train["annual_turnover"]
    )


def candidate_configs(base: dict[str, object], count: int = 250) -> list[dict[str, object]]:
    """Build a deterministic broad random search plus the current configuration."""
    profiles = {
        "balanced": {"momentum": 20, "relative_strength": 20, "trend": 20, "breakout": 20, "volume": 20},
        "momentum_quality": {"momentum": 30, "relative_strength": 25, "trend": 25, "breakout": 15, "volume": 5},
        "trend_breakout": {"momentum": 20, "relative_strength": 20, "trend": 30, "breakout": 25, "volume": 5},
        "momentum_breakout": {"momentum": 30, "relative_strength": 20, "trend": 20, "breakout": 25, "volume": 5},
    }
    rng = np.random.default_rng(20260921)
    candidates: list[dict[str, object]] = []
    seen: set[str] = set()

    def add(candidate: dict[str, object]) -> None:
        key = json.dumps(candidate, sort_keys=True)
        if key not in seen:
            seen.add(key)
            candidates.append(candidate)

    add({
        "fa_weight": float(base.get("fa_weight", 0.25)),
        "component_weights": base.get("component_weights", profiles["balanced"]),
        "entry_score": float(base.get("entry_score", 60)),
        "exit_score": float(base.get("exit_score", 40)),
        "minimum_volume_ratio": float(base.get("minimum_volume_ratio", 1.0)),
        "exit_ma": int(base.get("exit_ma", 200)),
        "trailing_stop_from_60d_high": float(base.get("trailing_stop_from_60d_high", 1.0)),
        "regime_ma": int(base.get("regime_ma", 100)),
        "exit_on_market_regime": bool(base.get("exit_on_market_regime", False)),
        "max_positions": int(base.get("max_positions", 30)),
        "minimum_average_trading_value_20d": float(base.get("minimum_average_trading_value_20d", 5_000_000_000)),
        "minimum_holding_days": int(base.get("minimum_holding_days", 20)),
        "cooldown_days": int(base.get("cooldown_days", 5)),
    })
    profile_values = list(profiles.values())
    while len(candidates) < count:
        entry_score = int(rng.choice([45, 50, 55, 60, 65, 70]))
        gap = int(rng.choice([10, 15, 20, 25, 30]))
        add({
            "fa_weight": float(rng.choice([0.10, 0.20, 0.30, 0.40])),
            "component_weights": profile_values[int(rng.integers(len(profile_values)))],
            "entry_score": entry_score,
            "exit_score": max(20, entry_score - gap),
            "minimum_volume_ratio": float(rng.choice([0.8, 1.0, 1.2, 1.5])),
            "exit_ma": int(rng.choice([50, 100, 200])),
            "trailing_stop_from_60d_high": float(rng.choice([0.10, 0.15, 0.20, 1.0])),
            "regime_ma": int(rng.choice([50, 100, 200])),
            "exit_on_market_regime": bool(rng.choice([False, True])),
            "max_positions": int(rng.choice([20, 30, 50, 75])),
            "minimum_average_trading_value_20d": float(rng.choice([2e9, 5e9, 10e9])),
            "minimum_holding_days": int(rng.choice([10, 20, 40])),
            "cooldown_days": int(rng.choice([0, 5, 10])),
        })
    return candidates


def run(root: Path) -> dict[str, object]:
    database = root / "analysis_data/stocks_analysis.sqlite"
    config = json.loads((root / "strategy_config.json").read_text(encoding="utf-8"))
    with sqlite3.connect(database) as connection:
        prices = pd.read_sql_query(
            "SELECT ticker,date,adjusted_close,volume,trading_value FROM price_daily "
            "WHERE analysis_ready=1 AND adjusted_close IS NOT NULL", connection,
        )
        benchmark = pd.read_sql_query(
            "SELECT date,adjusted_close FROM benchmark_daily WHERE analysis_ready=1 AND adjusted_close IS NOT NULL", connection,
        )
        companies = pd.read_sql_query("SELECT * FROM companies", connection)
        annual = pd.read_sql_query("SELECT * FROM financial_annual_wide", connection)
    prices["date"], benchmark["date"] = pd.to_datetime(prices["date"]), pd.to_datetime(benchmark["date"])
    prices["px"], benchmark["px"] = pd.to_numeric(prices["adjusted_close"], errors="coerce"), pd.to_numeric(benchmark["adjusted_close"], errors="coerce")
    history = build_ta_history(prices, benchmark, 1.0)
    dates = pd.DatetimeIndex(sorted(benchmark["date"].unique()))
    tickers = sorted(history["ticker"].unique())

    def matrix(column: str) -> np.ndarray:
        return history.pivot(index="date", columns="ticker", values=column).reindex(index=dates, columns=tickers).to_numpy(dtype=float)

    fields = ["px", "volume", "r3", "r6", "r12", "rs6", "ma50", "ma100", "ma200", "high_close20", "high_close60", "volume_ratio", "avg_trading_value20", "bm_px", "bm_ma50", "bm_ma100", "bm_ma200"]
    matrices = {name: matrix(name) for name in fields}
    matrices["returns"] = pd.DataFrame(matrices["px"], index=dates, columns=tickers).pct_change(fill_method=None).to_numpy()
    panel = historical_fa_panel(companies, annual, config, reporting_lag_days=int(config.get("reporting_lag_days", 90)))
    historical_fa = fa_matrix(panel, dates, tickers)
    matrices["fa_eligible"] = fa_eligibility_matrix(panel, dates, tickers)

    train_mask = np.asarray((dates >= "2019-01-01") & (dates <= "2024-12-31"))
    validation_mask = np.asarray((dates >= "2025-01-01") & (dates <= "2025-12-31"))
    test_mask = np.asarray(dates >= "2026-01-01")
    training_folds = [
        np.asarray((dates >= "2019-01-01") & (dates <= "2020-12-31")),
        np.asarray((dates >= "2021-01-01") & (dates <= "2021-12-31")),
        np.asarray((dates >= "2022-01-01") & (dates <= "2022-12-31")),
        np.asarray((dates >= "2023-01-01") & (dates <= "2023-12-31")),
        np.asarray((dates >= "2024-01-01") & (dates <= "2024-12-31")),
    ]
    benchmark_returns = benchmark.drop_duplicates("date", keep="last").set_index("date")["px"].sort_index().pct_change(fill_method=None).reindex(dates).fillna(0).to_numpy()
    zero_turnover = np.zeros(len(dates), dtype=float)
    benchmark_train = metrics(benchmark_returns, zero_turnover, train_mask)
    benchmark_folds = [metrics(benchmark_returns, zero_turnover, mask) for mask in training_folds]
    benchmark_validation = metrics(benchmark_returns, zero_turnover, validation_mask)
    benchmark_test = metrics(benchmark_returns, zero_turnover, test_mask)
    results: list[dict[str, object]] = []
    for candidate in candidate_configs(config):
        simulation = simulate_portfolio(matrices, historical_fa, candidate, cost_rate=0.0015)
        train = metrics(simulation["returns"], simulation["turnover"], train_mask)
        folds = [metrics(simulation["returns"], simulation["turnover"], mask) for mask in training_folds]
        validation = metrics(simulation["returns"], simulation["turnover"], validation_mask)
        test = metrics(simulation["returns"], simulation["turnover"], test_mask)
        average_positions = float(simulation["positions"][train_mask].sum(axis=1).mean())
        results.append({
            "config": candidate,
            "objective": float(training_objective(train, benchmark_train, folds, benchmark_folds, average_positions)),
            "train": train,
            "train_benchmark": benchmark_train,
            "training_folds": folds,
            "training_benchmark_folds": benchmark_folds,
            "train_average_positions": average_positions,
            "validation": validation,
            "validation_benchmark": benchmark_validation,
            "validation_average_positions": float(simulation["positions"][validation_mask].sum(axis=1).mean()),
            "test_2026": test,
            "test_2026_benchmark": benchmark_test,
            "test_2026_average_positions": float(simulation["positions"][test_mask].sum(axis=1).mean()),
        })
    results.sort(key=lambda item: item["objective"], reverse=True)
    report = {
        "simulation_version": SIMULATION_VERSION,
        "method": "One unified FA + price/volume score, Top-N portfolio, liquidity and market-regime controls",
        "fa_timing": "Annual reports become usable 90 calendar days after period_end; current snapshots are excluded from history.",
        "selection_rule": "Parameters rank on 2019-2024; 2025 is validation. 2026 is a post-design stress diagnostic already observed and must not be called an untouched holdout.",
        "objective_formula": "0.45*mean_fold_sharpe + 0.25*worst_fold_sharpe + 2*mean_fold_alpha + worst_fold_alpha + 0.5*train_alpha - 0.5*worst_drawdown - 0.02*turnover",
        "search": "Deterministic 250-candidate focused search with continuous strength tie-breaking and a reproducible 60-session trailing stop.",
        "cost_rate_one_way": 0.0015,
        "best": results[0],
        "top_10_training": results[:10],
    }
    (root / "analysis_data/strategy_optimization.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    print(json.dumps(run(root), ensure_ascii=False, indent=2))
