"""Backtest the unified FA + price/volume strategy with next-session execution."""

from __future__ import annotations

import json
import hashlib
import math
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from unified_strategy import (
    SIMULATION_VERSION,
    fa_eligibility_matrix,
    fa_matrix,
    historical_fa_panel,
    simulate_portfolio,
)


def performance(returns: pd.Series, turnover: pd.Series) -> dict[str, float | None]:
    returns = returns.dropna()
    if returns.empty:
        return {name: None for name in ("cagr", "sharpe", "maximum_drawdown", "positive_month_rate", "annual_turnover")}
    years = len(returns) / 252.0
    equity = (1.0 + returns).cumprod()
    volatility = returns.std(ddof=0)
    monthly = (1.0 + returns).resample("ME").prod() - 1.0
    drawdown = equity / equity.cummax().clip(lower=1.0) - 1.0
    return {
        "cagr": float(equity.iloc[-1] ** (1.0 / years) - 1.0) if years > 0 and equity.iloc[-1] > 0 else None,
        "sharpe": float(math.sqrt(252) * returns.mean() / volatility) if volatility > 0 else None,
        "maximum_drawdown": float(drawdown.min()),
        "positive_month_rate": float(monthly.gt(0).mean()),
        "annual_turnover": float(turnover.reindex(returns.index).fillna(0).sum() / years) if years > 0 else None,
    }


def calendar_return(dates: pd.DatetimeIndex, values: np.ndarray, months: int) -> np.ndarray:
    targets = dates - pd.DateOffset(months=months)
    positions = np.searchsorted(dates.values, targets.values, side="right") - 1
    result = np.full(len(values), np.nan, dtype=float)
    valid = (positions >= 0) & np.isfinite(values)
    bases = np.full(len(values), np.nan, dtype=float)
    bases[valid] = values[positions[valid]]
    valid &= np.isfinite(bases) & (bases > 0)
    result[valid] = values[valid] / bases[valid] - 1.0
    return result


def build_ta_history(
    prices: pd.DataFrame,
    benchmark: pd.DataFrame,
    volume_ratio: float = 1.0,
    min_buy_conditions: int = 5,
    trend_ma: int = 200,
) -> pd.DataFrame:
    """Build historical features; stock and VNINDEX returns share exact dates."""
    benchmark = benchmark.sort_values("date").drop_duplicates("date", keep="last")
    benchmark_dates = pd.DatetimeIndex(benchmark["date"])
    benchmark_px = pd.Series(benchmark["px"].to_numpy(dtype=float), index=benchmark_dates)
    benchmark_mas = {window: benchmark_px.rolling(window, min_periods=window).mean() for window in (50, 100, 200)}
    frames: list[pd.DataFrame] = []
    for ticker, group in prices.sort_values("date").groupby("ticker", observed=True):
        group = group.drop_duplicates("date", keep="last").set_index("date").sort_index()
        dates = pd.DatetimeIndex(group.index)
        stock = pd.to_numeric(group["px"], errors="coerce")
        common_dates = dates.intersection(benchmark_dates)
        stock_common, benchmark_common = stock.reindex(common_dates), benchmark_px.reindex(common_dates)
        frame = pd.DataFrame(index=dates)
        frame["ticker"], frame["px"] = ticker, stock.to_numpy(dtype=float)
        frame["volume"] = pd.to_numeric(group["volume"], errors="coerce")
        for months, label in ((3, "r3"), (6, "r6"), (12, "r12")):
            values = pd.Series(calendar_return(common_dates, stock_common.to_numpy(dtype=float), months), index=common_dates)
            frame[label] = values.reindex(dates, method="ffill").to_numpy()
        benchmark_r6 = pd.Series(calendar_return(common_dates, benchmark_common.to_numpy(dtype=float), 6), index=common_dates)
        frame["benchmark_r6"] = benchmark_r6.reindex(dates, method="ffill").to_numpy()
        frame["rs6"] = frame["r6"] - frame["benchmark_r6"]
        frame["bm_px"] = benchmark_px.reindex(dates, method="ffill").to_numpy()
        for window in (50, 100, 200):
            frame[f"bm_ma{window}"] = benchmark_mas[window].reindex(dates, method="ffill").to_numpy()
            frame[f"ma{window}"] = frame["px"].rolling(window, min_periods=window).mean()
        frame["high_close20"] = frame["px"].shift(1).rolling(20, min_periods=20).max()
        frame["high_close60"] = frame["px"].shift(1).rolling(60, min_periods=60).max()
        frame["volatility20"] = frame["px"].pct_change(fill_method=None).rolling(20, min_periods=20).std(ddof=0)
        volumes = pd.to_numeric(group["volume"], errors="coerce")
        frame["volume_ratio"] = volumes / volumes.shift(1).rolling(20, min_periods=20).mean()
        values = pd.to_numeric(group["trading_value"], errors="coerce")
        frame["avg_trading_value20"] = values.shift(1).rolling(20, min_periods=20).mean()
        frames.append(frame.reset_index(names="date"))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def trade_statistics(positions: pd.DataFrame, close: pd.DataFrame) -> dict[str, float | int | None]:
    """Return realized, pre-cost trade returns using the model's next-session fills.

    ``positions[t]`` earns the close-to-close return from ``t - 1`` to ``t``.
    An entry observed at ``t`` was therefore filled at close ``t - 1``; an exit
    observed at ``t`` was filled at close ``t - 1``. Open positions are not
    presented as completed trades.
    """
    trade_returns: list[float] = []
    open_trade_count = 0
    for ticker in positions.columns:
        state = positions[ticker].fillna(0)
        changes = state.diff().fillna(state)
        entries = np.flatnonzero(changes.to_numpy() > 0)
        exits = np.flatnonzero(changes.to_numpy() < 0)
        for entry_index in entries:
            later = exits[exits > entry_index]
            if not len(later):
                open_trade_count += 1
                continue
            exit_index = int(later[0])
            if entry_index == 0 or exit_index == 0:
                continue
            entry_price = close.iloc[entry_index - 1][ticker]
            exit_price = close.iloc[exit_index - 1][ticker]
            if pd.notna(entry_price) and pd.notna(exit_price) and entry_price > 0:
                trade_returns.append(float(exit_price / entry_price - 1.0))
    values = np.asarray(trade_returns)
    winners = values[values > 0]
    losers = values[values <= 0]
    return {
        "trade_count": int(len(values)),
        "open_trade_count": open_trade_count,
        "winning_trade_rate": float((values > 0).mean()) if len(values) else None,
        "average_trade_return_before_cost": float(values.mean()) if len(values) else None,
        "average_win_return": float(winners.mean()) if len(winners) else None,
        "average_loss_return": float(losers.mean()) if len(losers) else None,
        "max_win_return": float(values.max()) if len(values) else None,
        "max_loss_return": float(values.min()) if len(values) else None,
    }


def trade_ledger(end_positions: pd.DataFrame, close: pd.DataFrame, cost_rate: float) -> pd.DataFrame:
    """Actual closing fills, including open holdings; no assumed terminal sale."""
    columns = ["ticker", "entry_date", "exit_date", "status", "entry_price", "exit_price",
               "gross_return", "net_return", "holding_sessions"]
    records = []
    close = close.reindex(index=end_positions.index, columns=end_positions.columns)
    for ticker in end_positions:
        changes = end_positions[ticker].diff().fillna(end_positions[ticker])
        entries = np.flatnonzero(changes.to_numpy() > 0)
        exits = np.flatnonzero(changes.to_numpy() < 0)
        prices = close[ticker].to_numpy()
        for entry in entries:
            later = exits[exits > entry]
            exit_index = int(later[0]) if len(later) else None
            gross = prices[exit_index] / prices[entry] - 1 if exit_index is not None else None
            records.append({
                "ticker": ticker, "entry_date": end_positions.index[entry],
                "exit_date": end_positions.index[exit_index] if exit_index is not None else None,
                "status": "CLOSED" if exit_index is not None else "OPEN",
                "entry_price": prices[entry], "exit_price": prices[exit_index] if exit_index is not None else None,
                "gross_return": gross,
                "net_return": (1 + gross) * (1 - cost_rate) / (1 + cost_rate) - 1 if gross is not None else None,
                "holding_sessions": exit_index - entry if exit_index is not None else len(close) - 1 - entry,
            })
    return pd.DataFrame(records, columns=columns)


def ledger_statistics(ledger: pd.DataFrame) -> dict[str, object]:
    closed = ledger.loc[ledger["status"].eq("CLOSED")]
    net = closed["net_return"].astype(float)
    wins, losses = net[net > 0], net[net < 0]
    return {
        "trade_count": len(closed), "open_trade_count": int(ledger["status"].eq("OPEN").sum()),
        "winning_trade_rate": float((net > 0).mean()) if len(net) else None,
        "average_trade_return_before_cost": float(closed["gross_return"].mean()) if len(closed) else None,
        "average_trade_return_after_cost": float(net.mean()) if len(net) else None,
        "average_win_return": float(wins.mean()) if len(wins) else None,
        "average_loss_return": float(losses.mean()) if len(losses) else None,
        "max_win_return": float(wins.max()) if len(wins) else None,
        "max_loss_return": float(losses.min()) if len(losses) else None,
        "trade_return_basis": "after buy and sell costs; open holdings excluded",
    }


def run(database: Path, output_dir: Path, top_n: int | None = None, cost_rate: float = 0.0015, compare_components: bool = False) -> dict[str, object]:
    root = database.parent.parent
    config = json.loads((root / "strategy_config.json").read_text(encoding="utf-8"))
    if top_n is not None:
        config["max_positions"] = top_n
    with closing(sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)) as connection:
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
    history = build_ta_history(prices, benchmark)
    dates = pd.DatetimeIndex(sorted(benchmark["date"].unique()))
    tickers = sorted(history["ticker"].unique())

    def matrix(column: str) -> np.ndarray:
        return history.pivot(index="date", columns="ticker", values=column).reindex(index=dates, columns=tickers).to_numpy(dtype=float)

    fields = ["px", "volume", "r3", "r6", "r12", "rs6", "ma50", "ma100", "ma200", "high_close20", "high_close60", "volume_ratio", "avg_trading_value20", "bm_px", "bm_ma50", "bm_ma100", "bm_ma200"]
    matrices = {name: matrix(name) for name in fields}
    matrices["returns"] = pd.DataFrame(matrices["px"], index=dates, columns=tickers).pct_change(fill_method=None).to_numpy()
    panel = historical_fa_panel(companies, annual, config, int(config.get("reporting_lag_days", 90)))
    historical_fa = fa_matrix(panel, dates, tickers)
    matrices["fa_eligible"] = fa_eligibility_matrix(panel, dates, tickers)
    simulation = simulate_portfolio(matrices, historical_fa, config, cost_rate)
    net_returns = pd.Series(simulation["returns"], index=dates)
    turnover = pd.Series(simulation["turnover"], index=dates)
    positions = pd.DataFrame(simulation["end_positions"], index=dates, columns=tickers)
    close = pd.DataFrame(matrices["px"], index=dates, columns=tickers)
    benchmark_series = benchmark.drop_duplicates("date", keep="last").set_index("date")["px"].sort_index()
    benchmark_returns = benchmark_series.pct_change(fill_method=None).reindex(dates).fillna(0)
    equity = pd.DataFrame({
        "date": dates, "strategy_return": net_returns.values, "benchmark_return": benchmark_returns.values,
        "turnover": turnover.values, "positions": positions.sum(axis=1).values,
        "cash_weight": simulation["cash_weights"], "stale_positions": simulation["stale_positions"],
        "strategy_equity": (1 + net_returns).cumprod().values,
        "benchmark_equity": (1 + benchmark_returns).cumprod().values,
    })
    output_dir.mkdir(parents=True, exist_ok=True)
    equity.to_csv(output_dir / "backtest_equity.csv", index=False, encoding="utf-8-sig")
    ledger = trade_ledger(positions, close, cost_rate)
    ledger.to_csv(output_dir / "backtest_trades.csv", index=False, encoding="utf-8-sig")
    strategy_metrics = {**performance(net_returns, turnover), **ledger_statistics(ledger)}
    benchmark_metrics = performance(benchmark_returns, pd.Series(0.0, index=dates))
    phase_metrics: dict[str, object] = {}
    for label, start, end in (("2021-2022", "2021-01-01", "2022-12-31"), ("2022-2023", "2022-01-01", "2023-12-31"), ("2024-present", "2024-01-01", "2099-12-31")):
        phase = net_returns.loc[start:end]
        phase_metrics[label] = {
            "strategy": performance(phase, turnover.loc[phase.index]),
            "vnindex": performance(benchmark_returns.loc[phase.index], pd.Series(0.0, index=phase.index)),
        }
    optimization_path = root / "analysis_data" / "strategy_optimization.json"
    research_evaluation: dict[str, object] = {}
    if optimization_path.exists():
        optimization = json.loads(optimization_path.read_text(encoding="utf-8"))
        best = optimization.get("best", {})
        best_config = best.get("config", {}) if isinstance(best, dict) else {}
        config_matches = bool(best_config) and optimization.get("simulation_version") == SIMULATION_VERSION and all(config.get(key) == value for key, value in best_config.items())
        research_evaluation = {
            "config_matches_optimization": config_matches,
            "selection_rule": optimization.get("selection_rule"),
            "train_2019_2024": best.get("train") if config_matches else None,
            "train_benchmark": best.get("train_benchmark") if config_matches else None,
            "validation_2025": best.get("validation") if config_matches else None,
            "validation_2025_benchmark": best.get("validation_benchmark") if config_matches else None,
            "stress_2026": best.get("test_2026") if config_matches else None,
            "stress_2026_benchmark": best.get("test_2026_benchmark") if config_matches else None,
        }
    # Recompute displayed periods under this engine instead of copying stale optimization metrics.
    for label, start, end in (("train_2019_2024", "2019-01-01", "2024-12-31"),
                              ("validation_2025", "2025-01-01", "2025-12-31"),
                              ("stress_2026", "2026-01-01", "2099-12-31")):
        period = net_returns.loc[start:end]
        research_evaluation[label] = performance(period, turnover.reindex(period.index))
        benchmark_key = "train_benchmark" if label == "train_2019_2024" else label + "_benchmark"
        research_evaluation[benchmark_key] = performance(benchmark_returns.reindex(period.index), pd.Series(0., index=period.index))
    report: dict[str, object] = {
        "generated_at_utc": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "data_latest_date": str(dates[-1].date()),
        "simulation_version": SIMULATION_VERSION,
        "execution": "Signal after close t; fill at close t+1; first price return t+1 to t+2. Fractional adjusted units, cash divided among vacant slots; existing holdings not rebalanced.",
        "data_diagnostics": {"stale_position_sessions": int(simulation["stale_positions"].sum()),
                             "cancelled_buys_missing_quote": int(simulation["missing_buy_quotes"].sum()),
                             "pending_exits_at_end": int(simulation["unfilled_exits"][-1])},
        "strategy": "Unified FA + price/volume score; Top-N; liquidity and market-regime controls",
        "config": {key: config[key] for key in ("fa_weight", "component_weights", "entry_score", "exit_score", "minimum_volume_ratio", "exit_ma", "trailing_stop_from_60d_high", "regime_ma", "exit_on_market_regime", "max_positions", "minimum_average_trading_value_20d", "minimum_holding_days", "cooldown_days", "reporting_lag_days")},
        "transaction_cost_one_way": cost_rate,
        "strategy_metrics": strategy_metrics,
        "vnindex_metrics": benchmark_metrics,
        "alpha_cagr": strategy_metrics["cagr"] - benchmark_metrics["cagr"] if strategy_metrics["cagr"] is not None and benchmark_metrics["cagr"] is not None else None,
        "phase_metrics": phase_metrics,
        "research_evaluation": research_evaluation,
        "lookahead_control": "Signals use close t and fill at close t+1. No entry-day price return. Annual FA uses the configured reporting lag; current snapshots are excluded.",
        "fa_backtest_status": f"ASSUMED_{int(config.get('reporting_lag_days', 90))}D_REPORTING_LAG",
        "survivorship_bias_status": "CURRENT_LISTED_UNIVERSE_ONLY",
        "research_valid": False,
        "research_scope": "Ước lượng nghiên cứu có kiểm soát trên tập doanh nghiệp đang niêm yết, dùng độ trễ báo cáo FA theo cấu hình.",
    }
    if compare_components:
        from strategy_ablation import evaluate
        comparison = evaluate(matrices, historical_fa, dates, benchmark_returns, config, cost_rate)
        (output_dir / "strategy_ablation.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        report["component_comparison"] = comparison
    robust_path = output_dir / "robust_optimization.json"
    if robust_path.exists():
        research = json.loads(robust_path.read_text(encoding="utf-8"))
        with database.open("rb") as stream:
            database_digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if (research.get("simulation_version") == SIMULATION_VERSION
                and research.get("database_sha256") == database_digest
                and config in (research.get("baseline_config"), research.get("selected_config"))):
            report["robust_optimization"] = research
    (output_dir / "backtest_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--compare-components", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    print(json.dumps(run(root / "analysis_data/stocks_analysis.sqlite", root / "analysis_data", compare_components=args.compare_components), ensure_ascii=True, indent=2))
