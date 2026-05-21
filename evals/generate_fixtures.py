"""Generate eval fixtures + golden expectations from spec/prospects_seed.yaml.

Reads the merged Klas + Nejra + ICP.md anchor list and emits:
  - evals/fixtures/<slug>.json — Account JSON (the input)
  - evals/golden/<slug>.json   — expected tier + notes (the golden truth)

Run once whenever prospects_seed.yaml changes:
    python -m evals.generate_fixtures
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SEED_PATH = ROOT.parent / "spec" / "prospects_seed.yaml"
FIXTURES_DIR = ROOT / "evals" / "fixtures"
GOLDEN_DIR = ROOT / "evals" / "golden"


def _slug(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s[:80]


def _to_account_dict(record: dict, kind: str) -> dict:
    """Map seed-yaml record → Account schema fields. Extra keys are silently dropped
    (Pydantic strict=False default)."""
    decision_makers = []
    for dm in record.get("known_decision_makers", []) or []:
        if isinstance(dm, dict):
            decision_makers.append({"name": dm.get("name", ""), "role": dm.get("role")})

    blockers = []
    for b in record.get("personal_relationship_blockers", []) or []:
        if isinstance(b, dict):
            blockers.append(
                {
                    "role": b.get("role", "unknown"),
                    "history": b.get("history"),
                    "attitude": b.get("attitude"),
                }
            )

    signals = []
    for s in record.get("signals_at_capture", []) or []:
        if isinstance(s, dict) and "type" in s:
            signals.append(
                {
                    "type": s["type"],
                    "detail": s.get("detail", ""),
                    "date": str(s["date"]) if s.get("date") is not None else None,
                    "source_hint": s.get("source_hint"),
                }
            )

    return {
        "name": record["name"],
        "aliases": record.get("aliases", []) or [],
        "country": record.get("country", "SE"),
        "geo": record.get("geo"),
        "segment": record.get("segment"),
        "district": record.get("district"),
        "salesperson": record.get("salesperson"),
        "sources": record.get("sources", []) or [],
        "salesperson_rationale": record.get("salesperson_rationale"),
        "known_decision_makers": decision_makers,
        "expected_purchase_frequency": record.get("expected_frequency", "unknown") or "unknown",
        "competitor_incumbent": record.get("competitor_incumbent"),
        "displacement_window_signal": record.get("displacement_window_signal"),
        "suggested_entry_angle": record.get("suggested_entry_angle"),
        "management_priority": bool(record.get("management_priority", False)),
        "coordination_flags": record.get("coordination_flags", []) or [],
        "signals_at_capture": signals,
        "disqualification_notes": record.get("disqualification_notes"),
        "personal_relationship_blockers": blockers,
        "notes_for_eval": record.get("notes_for_eval"),
    }


def main() -> int:
    if not SEED_PATH.exists():
        print(f"ERROR: seed not found: {SEED_PATH}", file=sys.stderr)
        return 1

    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)

    # Clean previous run to avoid stale fixtures
    for p in FIXTURES_DIR.glob("*.json"):
        p.unlink()
    for p in GOLDEN_DIR.glob("*.json"):
        p.unlink()

    with SEED_PATH.open(encoding="utf-8") as f:
        seed = yaml.safe_load(f)

    sections = [
        ("prospect", seed.get("prospects", []) or []),
        ("anchor", seed.get("anchor_accounts", []) or []),
    ]

    count_in = 0
    count_out = 0
    for kind, records in sections:
        for rec in records:
            if not rec.get("name"):
                continue
            count_in += 1
            slug = f"{kind}-{_slug(rec['name'])}"
            account = _to_account_dict(rec, kind)
            expected = rec.get("expected_tier")
            if not expected:
                # No golden truth → skip; fixture would be untestable
                continue

            (FIXTURES_DIR / f"{slug}.json").write_text(
                json.dumps(account, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (GOLDEN_DIR / f"{slug}.json").write_text(
                json.dumps(
                    {
                        "expected_tier": expected,
                        "expected_frequency": rec.get("expected_frequency"),
                        "kind": kind,
                        "notes_for_eval": rec.get("notes_for_eval"),
                        "calibration_note": rec.get("_calibration_note"),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            count_out += 1

    print(f"Read {count_in} records; wrote {count_out} fixture pairs to evals/fixtures + evals/golden")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
