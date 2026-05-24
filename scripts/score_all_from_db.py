"""Batch-scorar alla companies i ravema_lis-DB via lis-core/agents/scoring.

Detta är bryggan mellan Python-agentlagret och TS-appen. Demo-läge: körs ad-hoc
(inte via tRPC realtime), persisterar resultat i companies.scoreTotal +
companies.scoreBreakdown. UI:t läser från DB. För Sprint 2 kan detta lyftas
till tRPC `scoring.recompute({companyId?})` (per DELTA §2.2).

Kör så här:
    cd lis-core
    python3 scripts/score_all_from_db.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Lägg lis-core på sys.path så `from agents...` och `from schemas...` funkar
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pymysql
from agents.scoring import run as score_run
from schemas.account import Account, DecisionMaker
from schemas.scoring import ScoringInput
from schemas.signal import IndustrialSignalType, SignalAtCapture

DB = dict(host="127.0.0.1", port=3309, user="ravema",
          password="ravema_dev_local", database="ravema_lis")


def signal_payload_to_at_capture(payload: dict) -> SignalAtCapture | None:
    """Convert a seed-payload back to a SignalAtCapture, skipping unknown types."""
    if not payload:
        return None
    sig_type_str = payload.get("type", "").upper().strip()
    try:
        sig_type = IndustrialSignalType(sig_type_str)
    except ValueError:
        return None
    return SignalAtCapture(
        type=sig_type,
        detail=payload.get("detail", ""),
        date=payload.get("date"),
        source_hint=payload.get("source_hint"),
    )


def company_row_to_account(row, signals_rows) -> Account:
    """Build an Account from a companies-row + matching signals-rows."""
    sources = [row["source"]] if row.get("source") else []
    if row.get("category") == "anchor":
        sources.append("icp-md-anchors")

    sigs: list[SignalAtCapture] = []
    for sr in signals_rows:
        # Payload was stored as JSON string by drizzle/MySQL
        payload = sr["payload"]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        atc = signal_payload_to_at_capture(payload or {})
        if atc:
            sigs.append(atc)

    return Account(
        name=row["name"],
        country=row.get("country") or "SE",
        geo=row.get("city"),
        segment=row.get("icpSegment"),
        sources=sources,
        signals_at_capture=sigs,
    )


def main():
    conn = pymysql.connect(cursorclass=pymysql.cursors.DictCursor, **DB)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, country, city, icpSegment, source, category, focus "
        "FROM companies ORDER BY id"
    )
    companies = cur.fetchall()
    print(f"Scoring {len(companies)} companies from lis-core/agents/scoring...")

    updated = 0
    tier_dist: dict[str, int] = {}
    for c in companies:
        cur.execute(
            "SELECT signalType, source, title, payload FROM signals WHERE companyId=%s",
            (c["id"],),
        )
        sigs_rows = cur.fetchall()

        account = company_row_to_account(c, sigs_rows)
        inp = ScoringInput(account=account)
        fit = score_run(inp)

        breakdown_json = {
            "dimensions": fit.breakdown.model_dump(),
            "total": fit.total,
            "tier": fit.tier,
            "reasons": fit.reasons,
            "overrides": fit.overrides,
            "confidence": fit.confidence,
            "engine": "lis-core/agents/scoring",
            "engine_version": "v0-mode-b",
        }
        cur.execute(
            "UPDATE companies SET scoreTotal=%s, scoreBreakdown=%s, focus=%s, scoredAt=NOW() WHERE id=%s",
            (fit.total, json.dumps(breakdown_json, ensure_ascii=False), fit.tier, c["id"]),
        )
        updated += 1
        tier_dist[fit.tier] = tier_dist.get(fit.tier, 0) + 1

    conn.commit()
    print(f"Updated: {updated}")
    print("Tier distribution (real scoring):")
    for t in ["AAA", "AA", "A", "B", "C"]:
        print(f"  {t}: {tier_dist.get(t, 0)}")

    # Sample output
    cur.execute(
        "SELECT name, focus, scoreTotal, JSON_EXTRACT(scoreBreakdown, '$.reasons') reasons, "
        "JSON_EXTRACT(scoreBreakdown, '$.overrides') overrides "
        "FROM companies ORDER BY scoreTotal DESC LIMIT 5"
    )
    print("\nTop 5 by score:")
    for r in cur.fetchall():
        print(f"  {r['focus']:3}  {r['scoreTotal']:3}p  {r['name']}")
        for reason in (json.loads(r['reasons']) if r['reasons'] else []):
            print(f"        • {reason}")

    conn.close()


if __name__ == "__main__":
    main()
