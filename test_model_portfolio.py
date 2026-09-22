import unittest

import pandas as pd

from model_portfolio import apply_model_portfolio, empty_state


def signals(score_a=80.0, score_b=70.0, *, risk_a=False, score_exit_a=False):
    return pd.DataFrame([
        {"ticker": "AAA", "unified_score": score_a, "selection_strength": 2.0,
         "ta_status": "READY", "price_fresh": True, "has_exit_data": True,
         "entry_eligible": score_a >= 60, "exit_risk_trigger": risk_a,
         "exit_score_trigger": score_exit_a, "exit_ma_trigger": risk_a,
         "exit_trailing_trigger": False, "exit_market_trigger": False,
         "watch_reason": "NONE"},
        {"ticker": "BBB", "unified_score": score_b, "selection_strength": 1.0,
         "ta_status": "READY", "price_fresh": True, "has_exit_data": True,
         "entry_eligible": score_b >= 60, "exit_risk_trigger": False,
         "exit_score_trigger": False, "exit_ma_trigger": False,
         "exit_trailing_trigger": False, "exit_market_trigger": False,
         "watch_reason": "NONE"},
    ])


CONFIG = {"max_positions": 1, "minimum_holding_days": 10, "cooldown_days": 5}


class ModelPortfolioTests(unittest.TestCase):
    def test_buy_is_pending_then_becomes_held_next_session(self):
        first, state, _ = apply_model_portfolio(
            signals(), CONFIG, empty_state(), "2026-09-21", ["2026-09-21"])
        self.assertEqual(first.set_index("ticker").loc["AAA", "final_action"], "BUY")
        self.assertEqual(state["pending_orders"]["AAA"]["side"], "BUY")
        self.assertEqual(state["positions"], {})
        second, state, _ = apply_model_portfolio(
            signals(), CONFIG, state, "2026-09-22", ["2026-09-21", "2026-09-22"])
        self.assertEqual(second.set_index("ticker").loc["AAA", "final_action"], "HOLD")
        self.assertEqual(second.set_index("ticker").loc["AAA", "screen_action"], "BUY")
        self.assertEqual(state["positions"]["AAA"]["holding_sessions"], 0)

    def test_same_day_rerun_does_not_execute_or_age_pending_buy(self):
        _, state, _ = apply_model_portfolio(
            signals(), CONFIG, empty_state(), "2026-09-21", ["2026-09-21"])
        rerun, state, _ = apply_model_portfolio(
            signals(), CONFIG, state, "2026-09-21", ["2026-09-21"])
        self.assertEqual(rerun.set_index("ticker").loc["AAA", "final_action"], "BUY")
        self.assertNotIn("AAA", state["positions"])

    def test_risk_exit_is_immediate_and_sale_starts_cooldown(self):
        state = {"last_signal_date": "2026-09-21",
                 "positions": {"AAA": {"entry_signal_date": "2026-09-20",
                                          "entry_date": "2026-09-21", "holding_sessions": 0}},
                 "pending_orders": {}, "cooldowns": {}}
        result, state, _ = apply_model_portfolio(
            signals(risk_a=True), CONFIG, state, "2026-09-22", ["2026-09-21", "2026-09-22"])
        self.assertEqual(result.set_index("ticker").loc["AAA", "final_action"], "EXIT")
        self.assertEqual(state["pending_orders"]["AAA"]["side"], "SELL")
        _, state, _ = apply_model_portfolio(
            signals(), CONFIG, state, "2026-09-23",
            ["2026-09-21", "2026-09-22", "2026-09-23"])
        self.assertNotIn("AAA", state["positions"])
        self.assertEqual(state["cooldowns"]["AAA"], 5)

    def test_score_exit_waits_for_minimum_holding_period(self):
        state = {"last_signal_date": "2026-09-21",
                 "positions": {"AAA": {"entry_signal_date": "2026-09-01",
                                          "entry_date": "2026-09-02", "holding_sessions": 7}},
                 "pending_orders": {}, "cooldowns": {}}
        early, state, _ = apply_model_portfolio(
            signals(score_a=30, score_exit_a=True), CONFIG, state, "2026-09-22",
            ["2026-09-21", "2026-09-22"])
        row = early.set_index("ticker").loc["AAA"]
        self.assertEqual(row["final_action"], "HOLD")
        self.assertTrue(row["exit_score_blocked_by_min_hold"])
        state["positions"]["AAA"]["holding_sessions"] = 9
        mature, _, _ = apply_model_portfolio(
            signals(score_a=30, score_exit_a=True), CONFIG, state, "2026-09-23",
            ["2026-09-22", "2026-09-23"])
        self.assertEqual(mature.set_index("ticker").loc["AAA", "final_action"], "EXIT")

    def test_pending_sell_waits_for_a_fresh_tradable_quote(self):
        state = {"last_signal_date": "2026-09-21",
                 "positions": {"AAA": {"entry_signal_date": "2026-09-01",
                                          "entry_date": "2026-09-02", "holding_sessions": 12}},
                 "pending_orders": {"AAA": {"side": "SELL", "signal_date": "2026-09-21"}},
                 "cooldowns": {}}
        stale = signals()
        stale.loc[stale["ticker"].eq("AAA"), "price_fresh"] = False
        result, state, _ = apply_model_portfolio(
            stale, CONFIG, state, "2026-09-22", ["2026-09-21", "2026-09-22"])
        self.assertIn("AAA", state["positions"])
        self.assertEqual(state["pending_orders"]["AAA"]["side"], "SELL")
        self.assertEqual(result.set_index("ticker").loc["AAA", "final_action"], "DATA_REVIEW")


if __name__ == "__main__":
    unittest.main()
