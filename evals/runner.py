"""Eval runner — runs the scoring agent across all fixtures, diffs vs golden.

Pass criterion: predicted tier within ±1 of expected_tier (SCORING.md tier ladder).
Threshold is read from pyproject.toml [tool.lis-core].eval_threshold (default 0.85).

CLI:
    python -m evals.runner                  # print summary + table of misses
    python -m evals.runner --json           # machine-readable report
    python -m evals.runner --strict         # require exact tier match
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from agents.scoring import run as score_run
from schemas.account import Account
from schemas.scoring import ScoringInput, Tier, tier_distance

ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = ROOT / "evals" / "fixtures"
GOLDEN_DIR = ROOT / "evals" / "golden"
DEFAULT_THRESHOLD = 0.85


@dataclass
class EvalCase:
    slug: str
    name: str
    predicted: Tier
    expected: Tier
    distance: int
    points: int
    passed: bool
    overrides: list[str]
    kind: str
    notes: str | None


def _load_pair(fixture_path: Path) -> tuple[Account, dict]:
    account = Account.model_validate_json(fixture_path.read_text(encoding="utf-8"))
    golden = json.loads((GOLDEN_DIR / fixture_path.name).read_text(encoding="utf-8"))
    return account, golden


def run_all(*, strict: bool = False) -> tuple[list[EvalCase], float]:
    if not FIXTURES_DIR.exists() or not any(FIXTURES_DIR.glob("*.json")):
        raise SystemExit(
            "No fixtures found. Run `python -m evals.generate_fixtures` first."
        )

    cases: list[EvalCase] = []
    for fx in sorted(FIXTURES_DIR.glob("*.json")):
        account, golden = _load_pair(fx)
        result = score_run(ScoringInput(account=account))
        expected: Tier = golden["expected_tier"]
        d = tier_distance(result.tier, expected)
        passed = (d == 0) if strict else (d <= 1)
        cases.append(
            EvalCase(
                slug=fx.stem,
                name=account.name,
                predicted=result.tier,
                expected=expected,
                distance=d,
                points=result.total,
                passed=passed,
                overrides=result.overrides,
                kind=golden.get("kind", "?"),
                notes=golden.get("notes_for_eval"),
            )
        )

    pass_rate = sum(1 for c in cases if c.passed) / len(cases)
    return cases, pass_rate


def _print_human(cases: list[EvalCase], pass_rate: float, threshold: float) -> None:
    misses = [c for c in cases if not c.passed]
    print(f"\n=== Eval results: {len(cases)} fixtures ===")
    print(f"Pass rate: {pass_rate:.1%}  (threshold {threshold:.0%})")
    print(f"Status:    {'PASS' if pass_rate >= threshold else 'FAIL'}")
    print()
    if misses:
        print(f"--- Misses ({len(misses)}) ---")
        misses.sort(key=lambda c: (-c.distance, c.name))
        for c in misses:
            print(
                f"  [{c.kind:8}] {c.name:42}  predicted={c.predicted:3}  expected={c.expected:3}  dist={c.distance}  pts={c.points}"
            )
            if c.notes:
                print(f"               notes: {c.notes}")
            if c.overrides:
                print(f"               overrides: {c.overrides}")
    else:
        print("No misses.")
    print()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="Emit JSON report")
    parser.add_argument("--strict", action="store_true", help="Require exact tier match")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    args = parser.parse_args()

    cases, pass_rate = run_all(strict=args.strict)

    if args.json:
        report = {
            "pass_rate": pass_rate,
            "threshold": args.threshold,
            "passed": pass_rate >= args.threshold,
            "strict": args.strict,
            "count": len(cases),
            "cases": [asdict(c) for c in cases],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(cases, pass_rate, args.threshold)

    return 0 if pass_rate >= args.threshold else 1


if __name__ == "__main__":
    sys.exit(main())
