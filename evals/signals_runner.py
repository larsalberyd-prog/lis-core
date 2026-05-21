"""Signals eval runner — runs the deterministic classifier against
hand-curated event/expected-signals pairs in evals/signals_seed.yaml.

Pass criterion: the predicted signal-type SET must equal the expected
set. Order-independent. Extra signals are misses; missing signals are misses.
(Strict by design — signals_seed is intentionally small and high-precision.)

CLI:
    python -m evals.signals_runner             # human-readable
    python -m evals.signals_runner --json      # machine-readable
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

from agents.signals.agent import run as signals_run
from agents.signals.schema import RawEvent, SignalsAgentInput

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "evals" / "signals_seed.yaml"
DEFAULT_THRESHOLD = 0.85


@dataclass
class SignalCase:
    case_id: str
    expected: list[str]
    predicted: list[str]
    matched_keywords: list[list[str]]
    quote_samples: list[str]
    passed: bool


def _load_cases() -> list[dict]:
    with SEED.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("cases", []) or []


def _run_case(record: dict) -> SignalCase:
    event = RawEvent(
        source_type=record["source_type"],
        source_url=record.get("source_url"),
        text=record["text"],
        employer_name=record.get("employer_name"),
        occupation=record.get("occupation"),
    )
    output = signals_run(SignalsAgentInput(events=[event], use_llm=False))
    predicted = [cs.signal.signal_type.value for cs in output.classified]
    expected = list(record.get("expected_signals", []) or [])
    passed = sorted(predicted) == sorted(expected)
    return SignalCase(
        case_id=record["id"],
        expected=expected,
        predicted=predicted,
        matched_keywords=[cs.matched_keywords for cs in output.classified],
        quote_samples=[cs.signal.evidence[0].source_quote for cs in output.classified],
        passed=passed,
    )


def run_all() -> tuple[list[SignalCase], float]:
    cases_raw = _load_cases()
    if not cases_raw:
        raise SystemExit(f"No signal cases in {SEED}")
    cases = [_run_case(rec) for rec in cases_raw]
    pass_rate = sum(1 for c in cases if c.passed) / len(cases)
    return cases, pass_rate


def _print_human(cases: list[SignalCase], pass_rate: float, threshold: float) -> None:
    print(f"\n=== Signals eval: {len(cases)} cases ===")
    print(f"Pass rate: {pass_rate:.1%}  (threshold {threshold:.0%})")
    print(f"Status:    {'PASS' if pass_rate >= threshold else 'FAIL'}\n")
    misses = [c for c in cases if not c.passed]
    if misses:
        print(f"--- Misses ({len(misses)}) ---")
        for c in misses:
            print(f"  [{c.case_id}]")
            print(f"    expected:  {c.expected}")
            print(f"    predicted: {c.predicted}")
            if c.matched_keywords:
                print(f"    keywords:  {c.matched_keywords}")
    else:
        print("No misses.")
    print()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    args = parser.parse_args()

    cases, pass_rate = run_all()
    if args.json:
        report = {
            "pass_rate": pass_rate,
            "threshold": args.threshold,
            "passed": pass_rate >= args.threshold,
            "count": len(cases),
            "cases": [asdict(c) for c in cases],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_human(cases, pass_rate, args.threshold)
    return 0 if pass_rate >= args.threshold else 1


if __name__ == "__main__":
    sys.exit(main())
