"""Time-ordering and cash-flow regressions for optimization."""
from copy import deepcopy
import unittest

import numpy as np
import pandas as pd

from robust_optimizer import promotion_checks, rank_prefix
from portfolio_experiments import promotion_checks as experimental_checks, variants
import test_strategy_engine as strategy_tests
from unified_strategy import simulate_portfolio


class RobustOptimizerTests(unittest.TestCase):
    def test_experimental_variants_do_not_mutate_baseline(self):
        base = {'exit_score': 35, 'minimum_holding_days': 10, 'cooldown_days': 5}
        cases = variants(base)
        self.assertEqual(base, cases['baseline'])
        self.assertEqual(cases['turnover_guard']['exit_score'], 30)
        self.assertNotIn('volatility_target_daily', base)

    def test_experimental_promotion_rejects_worse_validation(self):
        periods = {key: {'cagr': .1, 'sharpe': 1., 'maximum_drawdown': -.2,
                         'annual_turnover': 8.} for key in ('train', 'validation_2025', 'stress_2026', 'full')}
        candidate = deepcopy(periods)
        candidate['validation_2025'] = {**periods['validation_2025'], 'cagr': .09}
        checks = experimental_checks(periods, candidate, periods, candidate)
        self.assertFalse(checks['validation_2025_cagr_not_worse'])

    def test_volatility_cap_uses_previous_session_only(self):
        fixture = strategy_tests.StrategyEngineTests()
        m, fa, config = fixture.execution_fixture([100, 100, 100, 100])
        m['volatility20'] = np.array([[.01], [.04], [.01], [.01]])
        config['volatility_target_daily'] = .02
        result = simulate_portfolio(m, fa, config, cost_rate=0)
        self.assertAlmostEqual(result['end_positions'][1, 0], 1)
        self.assertAlmostEqual(result['cash_weights'][1], 0)
        changed = deepcopy(m)
        changed['volatility20'][1, 0] = .10
        repeat = simulate_portfolio(changed, fa, config, cost_rate=0)
        self.assertAlmostEqual(result['cash_weights'][1], repeat['cash_weights'][1])

    def test_capacity_limit_rejects_oversized_buy(self):
        fixture = strategy_tests.StrategyEngineTests()
        m, fa, config = fixture.execution_fixture([100, 100, 100])
        m['avg_trading_value20'][:] = 2_000_000_000
        config.update({'max_adv_participation': .05, 'portfolio_vnd': 1_000_000_000,
                       'minimum_average_trading_value_20d': 1_000_000_000})
        result = simulate_portfolio(m, fa, config, cost_rate=0)
        self.assertEqual(result['end_positions'].sum(), 0)
        self.assertGreater(result['capacity_rejected_buys'].sum(), 0)

    def test_ranking_cannot_see_returns_after_cutoff(self):
        dates = pd.bdate_range('2019-01-01', '2022-12-31')
        rng = np.random.default_rng(8)
        rows = [{'id': i, 'returns': rng.normal(.0005, .01, len(dates)),
                 'turnover': np.zeros(len(dates)), 'holdings': np.full(len(dates), 10.)} for i in range(3)]
        benchmark = rng.normal(.0001, .01, len(dates))
        original = rank_prefix(rows, dates, benchmark, '2021-01-01')
        modified = deepcopy(rows)
        for row in modified:
            row['returns'][dates >= '2021-01-01'] = 10 * (row['id'] + 1)
        changed_benchmark = benchmark.copy()
        changed_benchmark[dates >= '2021-01-01'] = -0.9
        self.assertEqual(original, rank_prefix(modified, dates, changed_benchmark, '2021-01-01'))

    def test_future_price_change_cannot_change_earlier_fills(self):
        fixture = strategy_tests.StrategyEngineTests()
        m, fa, config = fixture.execution_fixture([100, 110, 120, 130, 140, 150])
        original = simulate_portfolio(m, fa, config)
        changed = deepcopy(m)
        changed['px'][4:] *= 10
        result = simulate_portfolio(changed, fa, config)
        np.testing.assert_array_equal(original['returns'][:4], result['returns'][:4])
        np.testing.assert_array_equal(original['end_positions'][:4], result['end_positions'][:4])

    def test_exit_waits_for_a_tradable_quote(self):
        fixture = strategy_tests.StrategyEngineTests()
        m, fa, config = fixture.execution_fixture([100, 100, 70, np.nan, 60])
        result = simulate_portfolio(m, fa, config, cost_rate=0)
        self.assertEqual(result['end_positions'][3, 0], 1)
        self.assertEqual(result['end_positions'][4, 0], 0)
        self.assertAlmostEqual(np.prod(1 + result['returns']), .6)

    def test_promotion_rejects_recent_stress_collapse(self):
        metric = lambda cagr, sharpe, drawdown: {
            'cagr': cagr, 'sharpe': sharpe, 'maximum_drawdown': drawdown,
            'annual_turnover': 1,
        }
        periods = {
            'train': metric(.10, 1.0, -.20),
            'validation_2025': metric(.20, 1.2, -.15),
            'stress_2026': metric(-.10, -.8, -.16),
            'full': metric(.12, .85, -.30),
        }
        selected = {
            'train': metric(.16, 1.1, -.18),
            'validation_2025': metric(.25, 1.3, -.14),
            'stress_2026': metric(-.49, -3.7, -.38),
            'full': metric(.08, .60, -.41),
        }
        evaluations = {
            'baseline': {'normal': periods},
            'selected': {
                'normal': selected,
                'double_cost': {'validation_2025': metric(.23, 1.1, -.15)},
                'fa_lag_plus90': {'validation_2025': metric(.22, 1.0, -.15)},
            },
        }
        walk_forward = [
            {'selected': metric(.1, 1.1, -.1), 'baseline': metric(.1, 1.0, -.1)},
            {'selected': metric(.1, .9, -.1), 'baseline': metric(.1, 1.0, -.1)},
            {'selected': metric(.1, 1.1, -.1), 'baseline': metric(.1, 1.0, -.1)},
        ]
        local = [{'metrics': metric(.1, 1.1, -.1)}]
        checks = promotion_checks(evaluations, walk_forward, local, selected_id=3)
        self.assertFalse(checks['stress_2026_drawdown_within_5pp'])
        self.assertFalse(checks['full_sharpe_not_worse'])
        self.assertFalse(all(checks.values()))


if __name__ == '__main__':
    unittest.main()
