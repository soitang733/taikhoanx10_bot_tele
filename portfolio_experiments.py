"""Predeclared turnover/risk experiments; never changes production configuration.

All 2025/2026 observations have been seen in earlier research. This is a
retrospective robustness screen, not a fresh out-of-sample proof.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from backtest_engine import build_ta_history
from optimize_unified_strategy import metrics, training_objective
from unified_strategy import fa_eligibility_matrix, fa_matrix, historical_fa_panel, simulate_portfolio


ROOT = Path(__file__).resolve().parent
PERIODS = ("train", "validation_2025", "stress_2026", "full")


def variants(base: dict) -> dict[str, dict]:
    guard = {**base, "exit_score": 30, "minimum_holding_days": 20, "cooldown_days": 10}
    sized = {**base, "volatility_target_daily": 0.025}
    return {
        "baseline": base,
        "turnover_guard": guard,
        "volatility_cap": sized,
        "guard_and_cap": {**guard, "volatility_target_daily": 0.025},
    }


def promotion_checks(baseline: dict, candidate: dict, stress_baseline: dict,
                     stress_candidate: dict) -> dict[str, bool]:
    b, c = baseline, candidate
    return {
        "train_sharpe_higher": c["train"]["sharpe"] > b["train"]["sharpe"],
        "validation_2025_cagr_not_worse": c["validation_2025"]["cagr"] >= b["validation_2025"]["cagr"],
        "validation_2025_sharpe_not_worse": c["validation_2025"]["sharpe"] >= b["validation_2025"]["sharpe"],
        "stress_2026_cagr_not_worse": c["stress_2026"]["cagr"] >= b["stress_2026"]["cagr"],
        "stress_2026_drawdown_not_worse": c["stress_2026"]["maximum_drawdown"] >= b["stress_2026"]["maximum_drawdown"],
        "full_cagr_higher": c["full"]["cagr"] > b["full"]["cagr"],
        "full_sharpe_higher": c["full"]["sharpe"] > b["full"]["sharpe"],
        "full_drawdown_not_worse": c["full"]["maximum_drawdown"] >= b["full"]["maximum_drawdown"],
        "full_turnover_not_higher": c["full"]["annual_turnover"] <= b["full"]["annual_turnover"],
        "capacity_and_double_cost_cagr_not_worse": stress_candidate["full"]["cagr"] >= stress_baseline["full"]["cagr"],
        "capacity_and_double_cost_2026_not_worse": stress_candidate["stress_2026"]["cagr"] >= stress_baseline["stress_2026"]["cagr"],
    }


def main() -> None:
    config = json.loads((ROOT / "strategy_config.json").read_text(encoding="utf-8"))
    database = ROOT / "analysis_data/stocks_analysis.sqlite"
    with database.open("rb") as source:
        database_sha = hashlib.file_digest(source, "sha256").hexdigest()
    with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as connection:
        prices = pd.read_sql_query(
            "SELECT ticker,date,adjusted_close,volume,trading_value FROM price_daily "
            "WHERE analysis_ready=1 AND adjusted_close>0", connection)
        benchmark = pd.read_sql_query(
            "SELECT date,adjusted_close FROM benchmark_daily WHERE analysis_ready=1 AND adjusted_close>0", connection)
        companies = pd.read_sql_query("SELECT * FROM companies", connection)
        annual = pd.read_sql_query("SELECT * FROM financial_annual_wide", connection)
    prices["date"] = pd.to_datetime(prices["date"])
    prices["px"] = pd.to_numeric(prices["adjusted_close"])
    benchmark["date"] = pd.to_datetime(benchmark["date"])
    benchmark["px"] = pd.to_numeric(benchmark["adjusted_close"])
    history = build_ta_history(prices, benchmark)
    dates = pd.DatetimeIndex(sorted(benchmark["date"].unique()))
    tickers = sorted(history["ticker"].unique())
    fields = ("px", "volume", "r3", "r6", "r12", "rs6", "ma50", "ma100", "ma200",
              "high_close20", "high_close60", "volume_ratio", "avg_trading_value20",
              "volatility20", "bm_px", "bm_ma50", "bm_ma100", "bm_ma200")
    matrices = {name: history.pivot(index="date", columns="ticker", values=name)
                .reindex(index=dates, columns=tickers).to_numpy(dtype=float) for name in fields}
    panel = historical_fa_panel(companies, annual, config,
                                int(config.get("reporting_lag_days", 90)))
    fa = fa_matrix(panel, dates, tickers)
    matrices["fa_eligible"] = fa_eligibility_matrix(panel, dates, tickers)
    benchmark_returns = benchmark.drop_duplicates("date").set_index("date")["px"].reindex(dates).pct_change(fill_method=None).fillna(0).to_numpy()
    masks = {
        "train": np.asarray((dates >= "2019-01-01") & (dates < "2025-01-01")),
        "validation_2025": np.asarray(dates.year == 2025),
        "stress_2026": np.asarray(dates.year == 2026),
        "full": np.ones(len(dates), dtype=bool),
    }
    year_masks = [np.asarray(masks["train"] & (dates.year == year)) for year in range(2019, 2025)]
    benchmark_train = metrics(benchmark_returns, np.zeros(len(dates)), masks["train"])
    benchmark_years = [metrics(benchmark_returns, np.zeros(len(dates)), mask) for mask in year_masks]
    configurations = variants(config)
    normal: dict[str, dict] = {}
    objectives: dict[str, float] = {}
    for name, candidate in configurations.items():
        simulation = simulate_portfolio(matrices, fa, candidate)
        normal[name] = {period: metrics(simulation["returns"], simulation["turnover"], masks[period]) for period in PERIODS}
        year_metrics = [metrics(simulation["returns"], simulation["turnover"], mask) for mask in year_masks]
        average_positions = float(simulation["end_positions"][masks["train"]].sum(axis=1).mean())
        objectives[name] = training_objective(normal[name]["train"], benchmark_train,
                                               year_metrics, benchmark_years, average_positions)
        print(f"{name}: train objective={objectives[name]:.4f}, full CAGR={normal[name]['full']['cagr']:.2%}", flush=True)
    winner = max(configurations, key=lambda name: objectives[name])
    stress = {}
    for name in configurations:
        stressed_config = {**configurations[name], "max_adv_participation": 0.05, "portfolio_vnd": 1_000_000_000}
        simulation = simulate_portfolio(matrices, fa, stressed_config, cost_rate=0.003)
        stress[name] = {period: metrics(simulation["returns"], simulation["turnover"], masks[period]) for period in PERIODS}
        stress[name]["fill_diagnostics"] = {
            "rejected_buys": int(simulation["capacity_rejected_buys"].sum()),
            "deferred_exits": int(simulation["capacity_deferred_exits"].sum()),
        }
    all_checks = {
        name: promotion_checks(normal["baseline"], normal[name], stress["baseline"], stress[name])
        for name in configurations if name != "baseline"
    }
    checks = all_checks[winner] if winner != "baseline" else {"train_winner_differs": False}
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "database_sha256": database_sha,
        "latest_market_date": str(dates[-1].date()),
        "selection_policy": "Four predeclared alternatives; training 2019-2024 only. 2025/2026 are previously observed retrospective checks, not untouched holdouts.",
        "execution_stress": "One-way cost 0.30%; all-or-nothing orders capped at 5% of prior 20-day ADV for a VND 1 billion portfolio. No order-book or limit-price data available.",
        "configurations": configurations,
        "train_objectives": objectives,
        "training_winner": winner,
        "normal_metrics": normal,
        "execution_stress_metrics": stress,
        "all_candidate_checks": all_checks,
        "promotion_checks": checks,
        "promote": winner != "baseline" and all(checks.values()),
    }
    path = ROOT / "analysis_data/portfolio_experiments.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)
    print(json.dumps({"winner": winner, "promote": report["promote"], "checks": checks}, indent=2))


if __name__ == "__main__":
    main()
