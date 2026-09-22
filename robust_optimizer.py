"""Reproducible unified-strategy search with chronological diagnostics.

No network or config mutation. Select on <=2024, then evaluate only the frozen
finalist and baseline on later data. All periods have been observed before;
this is retrospective research, not a claim of pristine out-of-sample evidence.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import numpy as np
import pandas as pd

from backtest_engine import build_ta_history
from optimize_unified_strategy import candidate_configs, metrics, training_objective
from unified_strategy import (
    SIMULATION_VERSION, fa_eligibility_matrix, fa_matrix,
    historical_fa_panel, simulate_portfolio,
)


def save_json(path, payload):
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def rank_prefix(rows, dates, benchmark, cutoff):
    """Every score depends only on returns dated before the supplied cutoff."""
    mask = np.asarray((dates >= '2019-01-01') & (dates < cutoff))
    folds = [np.asarray(mask & (dates.year == year)) for year in sorted(set(dates[mask].year))]
    folds = [fold for fold in folds if fold.sum() >= 100]
    if mask.sum() < 252 or not folds:
        raise ValueError('Insufficient training history')
    zero = np.zeros(len(dates))
    bm = metrics(benchmark, zero, mask)
    bm_folds = [metrics(benchmark, zero, fold) for fold in folds]
    scores = []
    for row in rows:
        train = metrics(row['returns'], row['turnover'], mask)
        annual = [metrics(row['returns'], row['turnover'], fold) for fold in folds]
        average_positions = float(row['holdings'][mask].mean())
        objective = training_objective(train, bm, annual, bm_folds, average_positions)
        scores.append((objective, row['id'], train, annual, average_positions))
    return sorted(scores, key=lambda item: (-item[0], item[1]))


def neighbors(config):
    """Predetermined local perturbations; they do not enter candidate selection."""
    changes = [('entry_score', -5), ('entry_score', 5), ('exit_score', -5), ('exit_score', 5),
               ('max_positions', -5), ('max_positions', 5), ('minimum_holding_days', -5),
               ('minimum_holding_days', 5), ('fa_weight', -.05), ('fa_weight', .05)]
    for field, delta in changes:
        candidate = deepcopy(config)
        candidate[field] += delta
        if field == 'max_positions':
            candidate[field] = max(1, candidate[field])
        if field == 'minimum_holding_days':
            candidate[field] = max(0, candidate[field])
        if field == 'fa_weight':
            candidate[field] = min(.9, max(.05, candidate[field]))
        yield f'{field}{delta:+}', candidate


def promotion_checks(evaluations, walk_forward, local_results, selected_id):
    """Conservative gates applied only after the training winner is frozen.

    A candidate must not buy a good 2025 result by taking materially more risk
    in the partial 2026 stress period or over the complete sample.
    """
    baseline = evaluations['baseline']['normal']
    selected = evaluations['selected']['normal']
    local_sharpe = float(np.median([row['metrics']['sharpe'] for row in local_results]))
    wf_wins = sum(row['selected']['sharpe'] >= row['baseline']['sharpe'] for row in walk_forward)
    required_wf_wins = len(walk_forward) // 2 + 1
    return {
        'training_candidate_differs': selected_id != 0,
        'validation_cagr_not_worse': selected['validation_2025']['cagr'] >= baseline['validation_2025']['cagr'],
        'validation_sharpe_not_worse': selected['validation_2025']['sharpe'] >= baseline['validation_2025']['sharpe'],
        'validation_drawdown_within_2pp': selected['validation_2025']['maximum_drawdown'] >= baseline['validation_2025']['maximum_drawdown'] - .02,
        'walk_forward_majority_years_not_worse': wf_wins >= required_wf_wins,
        'neighbors_median_sharpe_above_baseline': local_sharpe >= baseline['train']['sharpe'],
        'validation_double_cost_positive': evaluations['selected']['double_cost']['validation_2025']['cagr'] > 0,
        'validation_delayed_fa_positive': evaluations['selected']['fa_lag_plus90']['validation_2025']['cagr'] > 0,
        'stress_2026_sharpe_not_worse_by_0_25': selected['stress_2026']['sharpe'] >= baseline['stress_2026']['sharpe'] - .25,
        'stress_2026_drawdown_within_5pp': selected['stress_2026']['maximum_drawdown'] >= baseline['stress_2026']['maximum_drawdown'] - .05,
        'full_cagr_not_worse': selected['full']['cagr'] >= baseline['full']['cagr'],
        'full_sharpe_not_worse': selected['full']['sharpe'] >= baseline['full']['sharpe'],
        'full_drawdown_within_2pp': selected['full']['maximum_drawdown'] >= baseline['full']['maximum_drawdown'] - .02,
    }


def run(root: Path, count: int = 384):
    if not 2 <= count <= 2000:
        raise ValueError('Use between 2 and 2000 candidates')
    started = time.monotonic()
    output = root / 'analysis_data'
    progress = output / 'optimization_progress.json'
    def update(stage, completed=0, total=count):
        payload = {'stage': stage, 'completed': completed, 'total': total,
                   'elapsed_seconds': round(time.monotonic() - started, 1),
                   'updated_at_utc': datetime.now(timezone.utc).isoformat()}
        save_json(progress, payload)
        print(json.dumps(payload), flush=True)
    update('load_features')
    config = json.loads((root / 'strategy_config.json').read_text(encoding='utf-8'))
    database = output / 'stocks_analysis.sqlite'
    with database.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    with closing(sqlite3.connect(f'file:{database.as_posix()}?mode=ro', uri=True)) as connection:
        prices = pd.read_sql_query('SELECT ticker,date,adjusted_close,volume,trading_value FROM price_daily WHERE analysis_ready=1 AND adjusted_close>0', connection)
        benchmark = pd.read_sql_query('SELECT date,adjusted_close FROM benchmark_daily WHERE analysis_ready=1 AND adjusted_close>0', connection)
        companies = pd.read_sql_query('SELECT * FROM companies', connection)
        annual = pd.read_sql_query('SELECT * FROM financial_annual_wide', connection)
    for frame in (prices, benchmark):
        frame['date'] = pd.to_datetime(frame['date'])
        frame['px'] = pd.to_numeric(frame['adjusted_close'])
    history = build_ta_history(prices, benchmark)
    dates = pd.DatetimeIndex(sorted(benchmark['date'].unique()))
    tickers = sorted(history['ticker'].unique())
    fields = ['px', 'volume', 'r3', 'r6', 'r12', 'rs6', 'ma50', 'ma100', 'ma200', 'high_close20',
              'high_close60', 'volume_ratio', 'avg_trading_value20', 'bm_px', 'bm_ma50', 'bm_ma100', 'bm_ma200']
    matrices = {field: history.pivot(index='date', columns='ticker', values=field).reindex(index=dates, columns=tickers).to_numpy(dtype=float) for field in fields}
    panel = historical_fa_panel(companies, annual, config, int(config.get('reporting_lag_days', 90)))
    fa = fa_matrix(panel, dates, tickers)
    matrices['fa_eligible'] = fa_eligibility_matrix(panel, dates, tickers)
    # Stress a slower FA availability assumption without reusing current snapshots.
    lag_panel = panel.copy()
    lag_panel['effective_date'] += pd.Timedelta(days=90)
    fa_delayed = fa_matrix(lag_panel, dates, tickers)
    fa_eligible_delayed = fa_eligibility_matrix(lag_panel, dates, tickers)
    bm = benchmark.set_index('date')['px'].reindex(dates).pct_change(fill_method=None).fillna(0).to_numpy()
    train_end = int(np.searchsorted(dates, pd.Timestamp('2025-01-01')))
    train_dates = dates[:train_end]
    train_m = {field: values[:train_end] for field, values in matrices.items()}
    candidates = [{**config, **candidate} for candidate in candidate_configs(config, count)]
    rows = []
    for index, candidate in enumerate(candidates):
        simulation = simulate_portfolio(train_m, fa[:train_end], candidate)
        rows.append({'id': index, 'returns': simulation['returns'], 'turnover': simulation['turnover'],
                     'holdings': simulation['end_positions'].sum(axis=1)})
        if index % 16 == 0 or index == count - 1:
            update('training_search', index + 1)
    ranking = rank_prefix(rows, train_dates, bm[:train_end], '2025-01-01')
    selected_id = ranking[0][1]
    selected = candidates[selected_id]
    update('walk_forward', count)
    walk_forward = []
    for year in (2021, 2022, 2023, 2024):
        choice = rank_prefix(rows, train_dates, bm[:train_end], f'{year}-01-01')[0][1]
        mask = np.asarray(dates.year == year)
        local_m = {key: value[mask] for key, value in matrices.items()}
        result = {'year': year, 'candidate_id': choice, 'trained_through': f'{year-1}-12-31'}
        for name, candidate in (('selected', candidates[choice]), ('baseline', config)):
            sim = simulate_portfolio(local_m, fa[mask], candidate)
            result[name] = metrics(sim['returns'], sim['turnover'], np.ones(mask.sum(), dtype=bool))
        result['vnindex'] = metrics(bm[mask], np.zeros(mask.sum()), np.ones(mask.sum(), dtype=bool))
        walk_forward.append(result)
    update('cost_and_fa_stress', count)
    periods = {'train': np.asarray((dates >= '2019-01-01') & (dates < '2025-01-01')),
               'validation_2025': np.asarray(dates.year == 2025), 'stress_2026': np.asarray(dates.year == 2026),
               'full': np.ones(len(dates), dtype=bool)}
    evaluations = {}
    for name, candidate in (('baseline', config), ('selected', selected)):
        evaluations[name] = {}
        for scenario, cost, fa_input in (('normal', .0015, fa), ('double_cost', .003, fa), ('fa_lag_plus90', .0015, fa_delayed)):
            scenario_matrices = matrices
            if scenario == 'fa_lag_plus90':
                scenario_matrices = {**matrices, 'fa_eligible': fa_eligible_delayed}
            sim = simulate_portfolio(scenario_matrices, fa_input, candidate, cost)
            evaluations[name][scenario] = {period: metrics(sim['returns'], sim['turnover'], mask) for period, mask in periods.items() if mask.any()}
    local_results = []
    train_mask = np.asarray(train_dates >= '2019-01-01')
    for name, candidate in neighbors(selected):
        sim = simulate_portfolio(train_m, fa[:train_end], candidate)
        local_results.append({'change': name, 'metrics': metrics(sim['returns'], sim['turnover'], train_mask)})
    gates = promotion_checks(evaluations, walk_forward, local_results, selected_id)
    report = {
        'simulation_version': SIMULATION_VERSION, 'database_sha256': digest,
        'generated_at_utc': datetime.now(timezone.utc).isoformat(), 'latest_date': str(dates[-1].date()),
        'candidate_count': count, 'seed': 20260921, 'selected_id': selected_id,
        'baseline_config': config, 'selected_config': selected,
        'selection_rule': 'Fixed candidates; stability objective on 2019-2024 only. Freeze winner before looking at 2025/2026. No picking a runner-up based on later data.',
        'walk_forward_policy': 'Each annual test starts in cash, using past-computed features; parameters selected solely on earlier years. No stitched claim of a continuous self-financing portfolio.',
        'limitations': 'Retrospective repeated research; 2025/2026 already observed. Current listed universe and assumed FA dates. No claim of untouched holdout or globally optimal strategy.',
        'evaluation': evaluations, 'walk_forward': walk_forward, 'local_sensitivity': local_results,
        'top_training': [{'id': item[1], 'objective': item[0], 'metrics': item[2], 'year_metrics': item[3], 'average_positions': item[4], 'config': candidates[item[1]]} for item in ranking[:10]],
        'promotion_checks': gates, 'eligible_for_promotion': all(gates.values()),
        'production_config_changed': False,
    }
    save_json(output / 'robust_optimization.json', report)
    save_json(output / 'strategy_candidate.json', selected)
    update('complete', count)
    print(json.dumps({'selected_id': selected_id, 'eligible_for_promotion': all(gates.values()), 'checks': gates, 'evaluation': evaluations}, indent=2), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--count', type=int, default=384)
    args = parser.parse_args()
    run(Path(__file__).resolve().parent, args.count)
