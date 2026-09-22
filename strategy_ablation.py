"""Controlled component-removal experiments inspired by the supplied ZIP.

All variants retain the unified FA/price-volume method. Only training data is
used to rank variants. Never mutate production strategy_config.json.
"""
from copy import deepcopy

import numpy as np
import pandas as pd

from unified_strategy import CONDITION_NAMES, SIMULATION_VERSION, simulate_portfolio


def scenarios(config):
    variants = {"baseline": deepcopy(config)}
    for name in CONDITION_NAMES:
        candidate = deepcopy(config)
        weights = dict(candidate.get("component_weights") or {key: 1 for key in CONDITION_NAMES})
        weights[name] = 0
        candidate["component_weights"] = weights
        variants["without_" + name] = candidate
    variants["without_trailing_stop"] = {**deepcopy(config), "trailing_stop_from_60d_high": 1.0}
    variants["exit_ma50"] = {**deepcopy(config), "exit_ma": 50}
    return variants


def evaluate(matrices, fa, dates, benchmark_returns, config, cost_rate):
    from backtest_engine import performance

    def summarize(simulation, mask):
        index = dates[mask]
        result = performance(pd.Series(simulation["returns"][mask], index=index),
                             pd.Series(simulation["turnover"][mask], index=index))
        result["average_positions"] = float(simulation["end_positions"][mask].sum(axis=1).mean())
        result["average_cash_weight"] = float(simulation["cash_weights"][mask].mean())
        return result

    variants = scenarios(config)
    train = np.asarray((dates >= "2019-01-01") & (dates <= "2024-12-31"))
    if not train.any():
        return {"status": "unavailable", "reason": "no 2019-2024 training observations"}
    # Truncate inputs before running candidate selection: later prices cannot influence it.
    boundary = int(np.flatnonzero(train)[-1]) + 1
    training_results = []
    for name, candidate in variants.items():
        sim = simulate_portfolio({key: value[:boundary] for key, value in matrices.items()}, fa[:boundary], candidate, cost_rate)
        mask = train[:boundary]
        index = dates[:boundary][mask]
        result = performance(pd.Series(sim["returns"][mask], index=index), pd.Series(sim["turnover"][mask], index=index))
        average_positions = float(sim["end_positions"][mask].sum(axis=1).mean())
        eligible = result["sharpe"] is not None and average_positions >= 5
        training_results.append({"scenario": name, "metrics": result, "average_positions": average_positions, "eligible": eligible})
    eligible = [row for row in training_results if row["eligible"]]
    best = max(eligible, key=lambda row: (row["metrics"]["sharpe"], row["metrics"]["maximum_drawdown"])) if eligible else training_results[0]
    selected = best["scenario"]
    evaluations = {}
    periods = {"train_2019_2024": train,
               "validation_2025": np.asarray((dates >= "2025-01-01") & (dates <= "2025-12-31")),
               "stress_2026": np.asarray(dates >= "2026-01-01")}
    for name in dict.fromkeys(("baseline", selected)):
        sim = simulate_portfolio(matrices, fa, variants[name], cost_rate)
        stress = simulate_portfolio(matrices, fa, variants[name], cost_rate * 2)
        evaluations[name] = {
            label: {"strategy": summarize(sim, mask), "double_cost": summarize(stress, mask),
                    "vnindex": performance(benchmark_returns.loc[dates[mask]], pd.Series(0., index=dates[mask]))}
            for label, mask in periods.items() if mask.any()
        }
    return {
        "simulation_version": SIMULATION_VERSION,
        "selection_rule": "Highest training Sharpe, then shallower drawdown; average holdings >= 5. Baseline wins exact ties. Validation does not select parameters.",
        "scope": "Remove one score component and renormalize remaining weights; entry/exit controls and continuous tie-break remain. This is a score ablation, not removal of every use of that variable.",
        "period_policy": "Continuous portfolio with warm-up and carried holdings. 2025/2026 have been observed previously; neither is an untouched holdout.",
        "cost_rate_one_way": cost_rate, "training": training_results,
        "selected_on_training": selected, "selected_config": variants[selected],
        "evaluation": evaluations, "production_config_changed": False,
    }
