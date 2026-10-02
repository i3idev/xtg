"""``python -m xtg.bench`` - run the performance scenarios and report."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
from pathlib import Path
from typing import Any

from xtg.bench.runner import (
    DEFAULT_ROWS,
    SCENARIOS,
    BenchResult,
    format_table,
)

#: A scenario must not regress by more than this factor before it is flagged.
DEFAULT_TOLERANCE = 0.20


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xtg bench", description="Benchmark the extraction pipeline."
    )
    parser.add_argument(
        "-s",
        "--scenario",
        action="append",
        choices=[*SCENARIOS, "all"],
        help="Scenario letter(s) to run (default: all).",
    )
    parser.add_argument("-n", "--rows", type=int, default=DEFAULT_ROWS, help="Rows per scenario.")
    parser.add_argument("--save", type=Path, help="Write the results to this JSON file.")
    parser.add_argument(
        "--compare", type=Path, help="Compare against a previously saved JSON file."
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        help="Allowed regression fraction before a scenario is flagged (default: 0.2).",
    )
    parser.add_argument("--json", action="store_true", help="Emit raw JSON.")
    return parser


def _selected(keys: list[str] | None) -> list[str]:
    if not keys or "all" in keys:
        return list("ABCDEFG")
    return [key for key in "ABCDEFG" if key in keys]


async def _run(keys: list[str], rows: int) -> list[BenchResult]:
    """Run the selected scenarios, awaiting the async ones."""
    results: list[BenchResult] = []
    for key in keys:
        _, factory = SCENARIOS[key]
        outcome = factory(rows)
        # Scenarios that touch the (synchronous) Reddit path stay sync.
        results.append(await outcome if inspect.isawaitable(outcome) else outcome)
    return results


def _load(path: Path) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Cannot read baseline {path}: {exc}") from exc
    return {entry["scenario"]: entry for entry in payload.get("results", [])}


def _compare(
    results: list[BenchResult], baseline: dict[str, dict[str, Any]], tolerance: float
) -> list[str]:
    """Flag scenarios slower than the baseline by more than ``tolerance``."""
    lines: list[str] = []
    for result in results:
        previous = baseline.get(result.scenario)
        if previous is None:
            lines.append(f"  {result.scenario}  new scenario, no baseline")
            continue
        before = float(previous.get("throughput", 0.0))
        if before <= 0:
            lines.append(f"  {result.scenario}  baseline has no throughput")
            continue
        change = (result.throughput - before) / before
        mark = "ok " if change >= -tolerance else "REGRESSION"
        lines.append(f"  {result.scenario}  {mark}  {change * 100:+.1f}%")
    return lines


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    keys = _selected(args.scenario)
    results = asyncio.run(_run(keys, args.rows))

    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(
            json.dumps(
                {"rows": args.rows, "results": [result.to_dict() for result in results]},
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"Saved baseline to {args.save}")

    if args.json:
        print(json.dumps([result.to_dict() for result in results], indent=2))
    else:
        print(format_table(results))
        for result in results:
            if result.detail:
                extras = "  ".join(f"{key}={value}" for key, value in result.detail.items())
                print(f"    {result.scenario}: {extras}")

    if args.compare:
        print(f"\nCompared to {args.compare}:")
        for line in _compare(results, _load(args.compare), args.tolerance):
            print(line)

    return 0


if __name__ == "__main__":
    sys.exit(main())
