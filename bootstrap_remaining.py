"""Resume the full-history bootstrap and rebuild analysis tables when it advances."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import prepare_analysis_data
import strategy_engine
import vn_stock_scraper_complete as scraper
from daily_data_pipeline import atomic_json, pipeline_lock


REQUIRED_FILES = (
    "metadata.json",
    "fa_annual.csv",
    "fa_quarterly.csv",
    "ta_daily.csv",
    "corporate_actions.csv",
    "diagnostics.json",
    "coverage_report.csv",
)


def is_complete(raw_dir: Path, symbol: str) -> bool:
    symbol_dir = raw_dir / symbol
    return all((symbol_dir / filename).exists() for filename in REQUIRED_FILES)


def main() -> None:
    parser = argparse.ArgumentParser(description="Resume all-ticker historical bootstrap")
    parser.add_argument("--raw-dir", type=Path, default=Path("scraper_output"))
    parser.add_argument("--analysis-dir", type=Path, default=Path("analysis_data"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Rebuild analysis after a complete raw mirror even when no ticker needed bootstrap",
    )
    args = parser.parse_args()
    raw_dir = args.raw_dir.resolve()
    analysis_dir = args.analysis_dir.resolve()
    analysis_dir.mkdir(parents=True, exist_ok=True)
    state_path = analysis_dir / "bootstrap_state.json"
    started = datetime.now(timezone.utc)

    with pipeline_lock(analysis_dir / "data_update.lock"):
        symbols = scraper.get_all_symbols("ALL")
        before = [symbol for symbol in symbols if is_complete(raw_dir, symbol)]
        remaining_before = [symbol for symbol in symbols if symbol not in set(before)]
        state = {
            "status": "running",
            "started_at_utc": started.isoformat(),
            "universe_size": len(symbols),
            "completed_before": len(before),
            "remaining_before": len(remaining_before),
        }
        atomic_json(state_path, state)
        try:
            if not remaining_before and not args.rebuild:
                finished = datetime.now(timezone.utc)
                state.update(
                    status="complete",
                    finished_at_utc=finished.isoformat(),
                    duration_seconds=round((finished - started).total_seconds(), 2),
                    completed_after=len(before),
                    remaining_after=[],
                    analysis_manifest=None,
                    strategy_manifest=None,
                    skipped="raw mirror already complete; use --rebuild to force a rebuild",
                )
                atomic_json(state_path, scraper.json_safe(state))
                print(json.dumps(state, ensure_ascii=False, indent=2))
                return
            if remaining_before:
                scraper.scrape_batch(
                    symbols=symbols,
                    years=10,
                    ta_years=8,
                    output_dir=str(raw_dir),
                    workers=max(1, args.workers),
                    resume=True,
                )
            after = [symbol for symbol in symbols if is_complete(raw_dir, symbol)]
            remaining_after = [symbol for symbol in symbols if symbol not in set(after)]
            analysis_manifest = None
            strategy_manifest = None
            if not remaining_after:
                analysis_manifest = prepare_analysis_data.build(raw_dir, analysis_dir)
                strategy_manifest = strategy_engine.build(
                    analysis_dir / "stocks_analysis.sqlite",
                    analysis_dir,
                    Path(__file__).resolve().parent / "strategy_config.json",
                )
            finished = datetime.now(timezone.utc)
            state.update(
                status="complete" if not remaining_after else "partial",
                finished_at_utc=finished.isoformat(),
                duration_seconds=round((finished - started).total_seconds(), 2),
                completed_after=len(after),
                remaining_after=remaining_after,
                analysis_manifest=analysis_manifest,
                strategy_manifest=strategy_manifest,
                analysis_rebuild_deferred=bool(remaining_after),
            )
            atomic_json(state_path, scraper.json_safe(state))
            print(json.dumps(state, ensure_ascii=False, indent=2))
        except BaseException as exc:
            state.update(
                status="failed",
                finished_at_utc=datetime.now(timezone.utc).isoformat(),
                error=f"{type(exc).__name__}: {exc}",
            )
            atomic_json(state_path, scraper.json_safe(state))
            raise


if __name__ == "__main__":
    main()
