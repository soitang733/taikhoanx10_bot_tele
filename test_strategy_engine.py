import unittest

import numpy as np
import pandas as pd

from backtest_engine import ledger_statistics, performance, trade_ledger, trade_statistics
from strategy_engine import aligned_calendar_return, build_fa, build_signal_fa, build_ta, combine
from unified_strategy import simulate_portfolio


class StrategyEngineTests(unittest.TestCase):
    def test_calendar_return_uses_common_previous_trading_date(self):
        dates = pd.to_datetime(["2025-01-29", "2025-04-29", "2025-04-30"])
        stock = pd.Series([100.0, 119.0, 120.0], index=dates)
        benchmark = pd.Series([1000.0, 1040.0, 1050.0], index=dates)
        stock_return, benchmark_return = aligned_calendar_return(stock, benchmark, dates[-1], 3)
        self.assertAlmostEqual(stock_return, 0.20)
        self.assertAlmostEqual(benchmark_return, 0.05)

    def test_buy_rule_uses_previous_20_sessions_for_breakout_and_volume(self):
        dates = pd.bdate_range("2024-01-02", periods=300)
        close = np.linspace(100.0, 200.0, len(dates))
        close[-1] = 220.0
        volume = np.full(len(dates), 1_000.0)
        volume[-1] = 2_000.0
        prices = pd.DataFrame({
            "ticker": "AAA", "date": dates, "adjusted_close": close,
            "volume": volume, "trading_value": close * volume, "analysis_ready": 1,
        })
        benchmark = pd.DataFrame({
            "date": dates, "adjusted_close": np.linspace(1000.0, 1050.0, len(dates)), "analysis_ready": 1,
        })
        result = build_ta(prices, benchmark, {"minimum_volume_ratio": 1.5}).iloc[0]
        self.assertAlmostEqual(result["avg_vol20"], 1_000.0)
        self.assertAlmostEqual(result["volume_ratio"], 2.0)
        self.assertTrue(result["breakout_pass"])
        self.assertTrue(result["buy_pass"])

    def test_stale_price_cannot_generate_buy(self):
        dates = pd.bdate_range("2024-01-02", periods=300)
        close = np.linspace(100.0, 200.0, len(dates))
        close[-1] = 220.0
        prices = pd.DataFrame({"ticker": "AAA", "date": dates,
                               "adjusted_close": close, "volume": 1_000_000,
                               "trading_value": close * 1_000_000, "analysis_ready": 1})
        benchmark_dates = dates.append(pd.DatetimeIndex([dates[-1] + pd.offsets.BDay(1)]))
        benchmark = pd.DataFrame({"date": benchmark_dates,
                                  "adjusted_close": np.linspace(1000, 1200, len(benchmark_dates)),
                                  "analysis_ready": 1})
        config = {"fa_weight": .2, "entry_score": 60, "exit_score": 35,
                  "max_positions": 30, "minimum_average_trading_value_20d": 2e9,
                  "minimum_volume_ratio": .8}
        ta = build_ta(prices, benchmark, config)
        self.assertFalse(bool(ta.iloc[0]["price_fresh"]))
        fa = pd.DataFrame({"ticker": ["AAA"], "fa_score": [80.], "fa_coverage": [1.],
                           "hard_reject_reason": [""], "fa_status": ["READY"]})
        result = combine(fa, ta, config).iloc[0]
        self.assertEqual(result["final_action"], "WATCH")
        self.assertEqual(result["watch_reason"], "STALE_PRICE")

    def test_fa_hard_reject_cannot_generate_buy(self):
        ta = pd.DataFrame({
            "ticker": ["AAA"], "price": [120.0], "ma200": [100.0],
            "ta_score": [100.0], "ta_status": ["READY"], "price_fresh": [True],
            "market_bull": [True], "avg_trading_value_20d": [10_000_000_000.0],
            "r_3m": [.1], "r_6m": [.2], "r_12m": [.3], "rs_6m": [.1],
            "high_close60": [115.0],
        })
        fa = pd.DataFrame({
            "ticker": ["AAA"], "fa_score": [90.0], "fa_coverage": [1.0],
            "hard_reject_reason": ["cfo_negative_2y"], "fa_status": ["READY"],
            "missing_data_reason": [""],
        })
        config = {"fa_weight": .2, "entry_score": 60, "exit_score": 40,
                  "max_positions": 30, "minimum_average_trading_value_20d": 5e9}
        result = combine(fa, ta, config).iloc[0]
        self.assertEqual(result["final_action"], "WATCH")
        self.assertEqual(result["watch_reason"], "FA_HARD_REJECT")

    def test_backtest_fa_hard_reject_eligibility_blocks_entry(self):
        m, fa, config = self.execution_fixture([100, 100, 100, 100])
        m["fa_eligible"] = np.zeros_like(fa, dtype=bool)
        result = simulate_portfolio(m, fa, config, cost_rate=0)
        self.assertFalse(result["end_positions"].any())

    def test_live_fa_does_not_use_unbacktestable_current_market_cap(self):
        companies = pd.DataFrame({"ticker": ["AAA"], "market_cap": [10_000.]})
        annual = pd.DataFrame({"ticker": ["AAA"], "period_end": ["2025-12-31"],
                               "net_income": [100.], "equity": [1000.],
                               "total_assets": [2000.], "revenue": [1500.]})
        config = {"fa_group_weights": {"quality": .4, "growth": .25, "value": .2, "safety": .15},
                  "minimum_fa_coverage": .5, "buy_fa_score": 65}
        first = build_signal_fa(companies, annual, config).iloc[0]
        companies["market_cap"] = 100_000.
        second = build_signal_fa(companies, annual, config).iloc[0]
        self.assertTrue(pd.isna(first["earnings_yield"]))
        self.assertEqual(first["fa_score"], second["fa_score"])

    def test_unified_portfolio_enforces_top_n_exit_priority_and_cooldown(self):
        days, tickers = 35, 3
        full = np.ones((days, tickers), dtype=float)
        matrices = {
            "px": full * 120,
            "r3": full * .10,
            "r6": full * .20,
            "r12": full * .30,
            "rs6": full * .10,
            "ma50": full * 110,
            "ma100": full * 105,
            "ma200": full * 100,
            "high_close20": full * 115,
            "volume_ratio": full * 2,
            "avg_trading_value20": full * 10_000_000_000,
            "bm_px": full * 1200,
            "bm_ma100": full * 1100,
            "returns": np.zeros((days, tickers), dtype=float),
        }
        matrices["px"][5, 0] = 90  # mandatory risk exit below MA200
        result = simulate_portfolio(matrices, full * 80, {
            "fa_weight": .25, "entry_score": 60, "exit_score": 40,
            "minimum_volume_ratio": 1, "exit_ma": 200, "regime_ma": 100,
            "max_positions": 2, "minimum_average_trading_value_20d": 5_000_000_000,
            "minimum_holding_days": 20, "cooldown_days": 5,
        })
        self.assertLessEqual(result["positions"].sum(axis=1).max(), 2)
        self.assertEqual(result["end_positions"][6, 0], 0)
        self.assertEqual(result["positions"][7, 0], 0)
        self.assertTrue(np.isfinite(result["turnover"]).all())

    def test_market_risk_off_can_force_portfolio_to_cash(self):
        days, tickers = 8, 1
        full = np.ones((days, tickers), dtype=float)
        matrices = {
            "px": full * 120, "r3": full * .10, "r6": full * .20, "r12": full * .30,
            "rs6": full * .10, "ma50": full * 110, "ma100": full * 105,
            "ma200": full * 100, "high_close20": full * 115, "volume_ratio": full * 2,
            "avg_trading_value20": full * 10_000_000_000, "bm_px": full * 1200,
            "bm_ma100": full * 1100, "returns": np.zeros((days, tickers), dtype=float),
        }
        matrices["bm_px"][3:] = 1000
        result = simulate_portfolio(matrices, full * 80, {
            "fa_weight": .2, "entry_score": 60, "exit_score": 40,
            "minimum_volume_ratio": 1, "exit_ma": 200, "regime_ma": 100,
            "exit_on_market_regime": True, "max_positions": 1,
            "minimum_average_trading_value_20d": 5_000_000_000,
            "minimum_holding_days": 20, "cooldown_days": 5,
        })
        self.assertEqual(result["positions"][3, 0], 1)
        self.assertEqual(result["positions"][4, 0], 1)
        self.assertEqual(result["end_positions"][4, 0], 0)

    def test_equal_unified_scores_use_continuous_strength_as_tiebreaker(self):
        days, tickers = 4, 2
        full = np.ones((days, tickers), dtype=float)
        matrices = {
            "px": full * 120, "r3": full * .10, "r6": full * .20, "r12": full * .30,
            "rs6": full * .10, "ma50": full * 110, "ma100": full * 105,
            "ma200": full * 100, "high_close20": full * 115, "volume_ratio": full * 2,
            "avg_trading_value20": full * 10_000_000_000, "bm_px": full * 1200,
            "bm_ma100": full * 1100, "returns": np.zeros((days, tickers), dtype=float),
        }
        matrices["r12"][:, 1] = .60
        result = simulate_portfolio(matrices, full * 80, {
            "fa_weight": .2, "entry_score": 60, "exit_score": 40,
            "minimum_volume_ratio": 1, "exit_ma": 200, "regime_ma": 100,
            "exit_on_market_regime": False, "max_positions": 1,
            "minimum_average_trading_value_20d": 5_000_000_000,
            "minimum_holding_days": 20, "cooldown_days": 5,
        })
        self.assertEqual(result["end_positions"][1, 0], 0)
        self.assertEqual(result["end_positions"][1, 1], 1)

    def test_rolling_high_trailing_stop_exits_next_session(self):
        days, tickers = 6, 1
        full = np.ones((days, tickers), dtype=float)
        matrices = {
            "px": full * 120, "r3": full * .10, "r6": full * .20, "r12": full * .30,
            "rs6": full * .10, "ma50": full * 90, "ma100": full * 85,
            "ma200": full * 80, "high_close20": full * 110, "high_close60": full * 120,
            "volume_ratio": full * 2, "avg_trading_value20": full * 10_000_000_000,
            "bm_px": full * 1200, "bm_ma100": full * 1100,
            "returns": np.zeros((days, tickers), dtype=float),
        }
        matrices["px"][2:] = 100
        result = simulate_portfolio(matrices, full * 80, {
            "fa_weight": .2, "entry_score": 60, "exit_score": 40,
            "minimum_volume_ratio": 1, "exit_ma": 200, "regime_ma": 100,
            "trailing_stop_from_60d_high": .15, "exit_on_market_regime": False,
            "max_positions": 1, "minimum_average_trading_value_20d": 5_000_000_000,
            "minimum_holding_days": 20, "cooldown_days": 5,
        })
        self.assertEqual(result["positions"][2, 0], 1)
        self.assertEqual(result["positions"][3, 0], 1)
        self.assertEqual(result["end_positions"][3, 0], 0)

    def execution_fixture(self, prices):
        full = np.ones((len(prices), 1))
        matrices = {
            "px": np.asarray(prices, dtype=float).reshape(-1, 1),
            "r3": full * .1, "r6": full * .2, "r12": full * .3, "rs6": full * .1,
            "ma50": full * 90, "ma200": full * 80, "high_close20": full * 95,
            "volume_ratio": full * 2, "volume": full * 1000,
            "avg_trading_value20": full * 10e9, "bm_px": full * 1200, "bm_ma100": full * 1100,
        }
        config = {"fa_weight": .2, "entry_score": 60, "exit_score": 35, "minimum_volume_ratio": 1,
                  "max_positions": 1, "minimum_holding_days": 0, "cooldown_days": 5}
        return matrices, full * 80, config

    def test_next_close_fill_does_not_earn_entry_day_jump_and_charges_both_sides(self):
        m, fa, config = self.execution_fixture([100, 200, 220, 220, 220])
        m["ma200"][2:] = 300  # Exit decided day 2, executed day 3.
        result = simulate_portfolio(m, fa, config, cost_rate=.01)
        self.assertAlmostEqual(result["returns"][1], 1 / 1.01 - 1)
        self.assertAlmostEqual(result["returns"][2], .10)
        self.assertAlmostEqual(result["returns"][3], -.01)
        self.assertAlmostEqual(np.prod(1 + result["returns"]), 1.1 * .99 / 1.01)
        dates = pd.bdate_range("2026-01-01", periods=5)
        ledger = trade_ledger(pd.DataFrame(result["end_positions"], index=dates, columns=["AAA"]),
                              pd.DataFrame(m["px"], index=dates, columns=["AAA"]), .01)
        self.assertEqual(ledger.iloc[0]["entry_date"], dates[1])
        self.assertEqual(ledger.iloc[0]["exit_date"], dates[3])
        self.assertAlmostEqual(ledger.iloc[0]["net_return"], 1.1 * .99 / 1.01 - 1)

    def test_missing_quote_preserves_position_and_catches_up_on_resume(self):
        m, fa, config = self.execution_fixture([100, 100, np.nan, 120, 120])
        result = simulate_portfolio(m, fa, config, cost_rate=0)
        self.assertEqual(result["stale_positions"][2], 1)
        self.assertEqual(result["end_positions"][2, 0], 1)
        self.assertAlmostEqual(result["returns"][3], .2)

    def test_zero_volume_cannot_fill_and_unused_slots_stay_cash(self):
        m, fa, config = self.execution_fixture([100, 100, 100, 100])
        m["volume"][1] = 0
        config["max_positions"] = 2
        result = simulate_portfolio(m, fa, config, cost_rate=0)
        self.assertEqual(result["end_positions"][1, 0], 0)
        self.assertEqual(result["missing_buy_quotes"][1], 1)
        self.assertEqual(result["end_positions"][3, 0], 1)
        self.assertAlmostEqual(result["cash_weights"][3], .5)

    def test_drawdown_includes_initial_capital_and_no_fake_winning_trade(self):
        dates = pd.bdate_range("2026-01-01", periods=2)
        result = performance(pd.Series([-.1, 0], index=dates), pd.Series(0., index=dates))
        self.assertAlmostEqual(result["maximum_drawdown"], -.1)
        ledger = pd.DataFrame({"status": ["CLOSED", "OPEN"], "net_return": [-.1, None], "gross_return": [-.08, None]})
        stats = ledger_statistics(ledger)
        self.assertIsNone(stats["max_win_return"])
        self.assertEqual(stats["open_trade_count"], 1)

    def test_fa_uses_exact_years_and_does_not_invent_zero_debt(self):
        companies = pd.DataFrame({"ticker": ["AAA", "BBB"]})
        rows = []
        for ticker, years in (("AAA", [2022, 2024, 2025]), ("BBB", [2020, 2021, 2023, 2025])):
            for year in years:
                rows.append({"ticker": ticker, "period_end": f"{year}-12-31",
                             "revenue": 200 if year == 2025 else 100, "net_income": 20 if year == 2025 else 10,
                             "equity": 100, "total_assets": 200, "operating_cash_flow": 20,
                             "short_term_borrowings": None, "long_term_borrowings": None})
        config = {"fa_group_weights": {"quality": .4, "growth": .25, "value": .2, "safety": .15},
                  "minimum_fa_coverage": .5, "buy_fa_score": 65}
        result = build_fa(companies, pd.DataFrame(rows), pd.DataFrame(), config).set_index("ticker")
        self.assertAlmostEqual(result.loc["AAA", "revenue_cagr_3y"], 2 ** (1 / 3) - 1)
        self.assertAlmostEqual(result.loc["AAA", "profit_growth"], 1)
        self.assertAlmostEqual(result.loc["AAA", "roe"], .2)
        self.assertTrue(pd.isna(result.loc["BBB", "revenue_cagr_3y"]))
        self.assertTrue(pd.isna(result.loc["BBB", "profit_growth"]))
        self.assertTrue(result["debt_equity"].isna().all())

    def test_trade_statistics_use_next_session_model_fills_and_exclude_open_trades(self):
        dates = pd.date_range("2026-01-01", periods=6)
        positions = pd.DataFrame({"AAA": [0, 1, 1, 0, 1, 1]}, index=dates)
        close = pd.DataFrame({"AAA": [100.0, 110.0, 99.0, 120.0, 130.0, 140.0]}, index=dates)
        result = trade_statistics(positions, close)
        self.assertEqual(result["trade_count"], 1)
        self.assertEqual(result["open_trade_count"], 1)
        self.assertAlmostEqual(result["average_trade_return_before_cost"], -0.01)


if __name__ == "__main__":
    unittest.main()
