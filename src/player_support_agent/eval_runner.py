"""Offline evaluation scaffold for player-support cases.

This runner intentionally does not call Gmail, ClickHouse, or a model. It only
compares local fixture expected/actual fields so anonymized cases can be added
later without changing the evaluation harness.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


from .paths import default_eval_fixtures_dir

DEFAULT_FIXTURES_DIR = str(default_eval_fixtures_dir())


def compare_case(case: dict[str, Any]) -> dict[str, Any]:
    """Compare one fixture's expected and actual dictionaries."""

    expected = case.get("expected") or {}
    actual = case.get("actual") or {}
    keys = sorted(set(expected) | set(actual))
    mismatches = [
        {
            "field": key,
            "expected": expected.get(key),
            "actual": actual.get(key),
        }
        for key in keys
        if expected.get(key) != actual.get(key)
    ]
    return {
        "id": case.get("id"),
        "passed": not mismatches,
        "mismatches": mismatches,
    }


def load_cases(fixtures_dir: str | Path = DEFAULT_FIXTURES_DIR) -> list[dict[str, Any]]:
    """Load JSON fixture cases from a directory."""

    base = Path(fixtures_dir)
    if not base.exists():
        return []
    cases: list[dict[str, Any]] = []
    for path in sorted(base.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            cases.extend(item for item in data if isinstance(item, dict))
        elif isinstance(data, dict):
            cases.append(data)
    return cases


def run_eval(fixtures_dir: str | Path = DEFAULT_FIXTURES_DIR) -> dict[str, Any]:
    """Run fixture comparisons and return a compact report."""

    cases = load_cases(fixtures_dir)
    results = [compare_case(case) for case in cases]
    passed = sum(1 for result in results if result["passed"])
    return {
        "fixture_count": len(cases),
        "passed": passed,
        "failed": len(cases) - passed,
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run offline support-case eval fixtures.")
    parser.add_argument("--fixtures-dir", default=DEFAULT_FIXTURES_DIR)
    args = parser.parse_args()
    print(json.dumps(run_eval(args.fixtures_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
