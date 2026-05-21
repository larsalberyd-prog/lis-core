"""End-to-end demo-pipeline: enrich → score → brief → export.

Steg per konto:
    1. Läs demo_fixtures.yaml
    2. Apollo: hämta 5 mål-roller per konto (search → match by id, ~5 credits/konto)
    3. Lusha: enrich varje hittad person med mobil + email-confidence (~5 credits/konto)
    4. Scoring: kör existing Mode B-scorer
    5. Brief: läs hand-curaterad IntelligencePack från demo/briefs/<id>.json
       (eller stub om saknas)
    6. Skriv companies.json + briefs/<id>.json till ravema-lis frontend

Användning:
    cd lis-core
    python3 -m demo.enrich_and_export                 # full körning
    python3 -m demo.enrich_and_export --no-enrich     # hoppa över API-anrop (cache-only)
    python3 -m demo.enrich_and_export --account acc-innovation  # bara en konto

Kostnad full körning: ~40 Apollo credits + ~40 Lusha credits för 8 konton.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
LIS_CORE = HERE.parent
PROJECT_ROOT = LIS_CORE.parent
FRONTEND_DATA = PROJECT_ROOT / "ravema-lis" / "client" / "src" / "data"
BRIEFS_SEED = HERE / "briefs"

sys.path.insert(0, str(LIS_CORE))

from agents.brief.schema import (  # noqa: E402
    BriefCompetitor, BriefDecisionMaker, BriefSignal, BriefSource, IntelligencePack,
)
from agents.scoring.agent import run as score_account  # noqa: E402
from schemas.account import Account, DecisionMaker, RelationshipBlocker  # noqa: E402
from schemas.scoring import FitScore, ScoringInput  # noqa: E402
from schemas.signal import IndustrialSignalType, SignalAtCapture  # noqa: E402
from shared.integrations import apollo, lusha  # noqa: E402

# Reuse mappings from the v1 export script
CUSTOM_SIGNAL_REMAP: dict[str, IndustrialSignalType | None] = {
    "CUSTOMER_WIN_DOWNSTREAM": IndustrialSignalType.MAJOR_CUSTOMER_WIN,
    "MANAGEMENT_PRIORITY": None,
    "COMPETITOR_INCUMBENT_KNOWN": None,
    "COMPETITOR_DISPLACEMENT_TARGET": None,
    "DISQUALIFICATION_BLOCKER": None,
    "PROCUREMENT_GATE_BLOCKER": None,
}

log = logging.getLogger("enrich_and_export")


# --- Build Account from demo_fixtures.yaml (same as v1 export) ----------------


def _build_account(fx: dict[str, Any]) -> Account:
    signals_at_capture: list[SignalAtCapture] = []
    management_priority = False
    competitor_incumbent: str | None = None
    disqualification_parts: list[str] = []
    relationship_blockers: list[RelationshipBlocker] = []

    for sig in fx.get("signals", []):
        raw_type = sig["type"]
        try:
            enum_type = IndustrialSignalType(raw_type)
            signals_at_capture.append(SignalAtCapture(
                type=enum_type, detail=sig.get("detail", ""),
                date=sig.get("date"), source_hint=sig.get("evidence_source"),
            ))
            continue
        except ValueError:
            pass

        if raw_type in CUSTOM_SIGNAL_REMAP:
            target = CUSTOM_SIGNAL_REMAP[raw_type]
            if target is not None:
                signals_at_capture.append(SignalAtCapture(
                    type=target, detail=sig.get("detail", ""),
                    date=sig.get("date"), source_hint=sig.get("evidence_source"),
                ))
            else:
                if raw_type == "MANAGEMENT_PRIORITY":
                    management_priority = True
                elif raw_type == "COMPETITOR_INCUMBENT_KNOWN":
                    competitor_incumbent = sig.get("detail")
                elif raw_type in ("DISQUALIFICATION_BLOCKER", "PROCUREMENT_GATE_BLOCKER"):
                    disqualification_parts.append(sig.get("detail", ""))
            continue

        raise ValueError(f"Unknown signal type {raw_type!r} in {fx['name']}")

    for note in fx.get("disqualification_notes", []) or []:
        disqualification_parts.append(note)

    known_dms: list[DecisionMaker] = []
    for dm in fx.get("decision_makers", []) or []:
        if "name" in dm:
            known_dms.append(DecisionMaker(name=dm["name"], role=dm.get("role")))

    for blk in fx.get("personal_relationship_blockers", []) or []:
        relationship_blockers.append(RelationshipBlocker(**blk))

    sources: list[str] = []
    if fx.get("rationale_klas"):
        sources.append("klas-excel-2026-05-20")
    if fx.get("signals"):
        sources.append("nejra-email-2026-05-20")
    if not sources:
        sources.append("icp-md-anchors")

    return Account(
        name=fx["name"],
        aliases=fx.get("aliases", []),
        country=fx.get("country", "SE"),
        geo=fx.get("city") or fx.get("geo"),
        segment=fx.get("segment"),
        district=fx.get("district"),
        salesperson=fx.get("salesperson_assigned"),
        sources=sources,
        salesperson_rationale=fx.get("rationale_klas") or fx.get("description"),
        known_decision_makers=known_dms,
        competitor_incumbent=competitor_incumbent or fx.get("competitor_incumbent"),
        suggested_entry_angle=(fx.get("entry_angles") or [None])[0],
        management_priority=management_priority,
        signals_at_capture=signals_at_capture,
        disqualification_notes="; ".join(disqualification_parts) if disqualification_parts else None,
        personal_relationship_blockers=relationship_blockers,
    )


# --- Enrich step (Apollo + Lusha) --------------------------------------------


def _enrich_account(
    fx: dict[str, Any],
    *,
    enrich: bool,
) -> list[dict[str, Any]]:
    """Returnerar merged-kontakter (Apollo email + Lusha mobil) per mål-roll.

    Filtrerar bort Apollo-träffar som är på blacklist (wrong-company-matches,
    irrelevanta titlar) per fixturens `apollo_contact_blacklist`.
    Dedup:erar samma person som matchar flera roll-filter — behåller den
    starkaste roll-matchen (Executive över Technical, mer specifik över generisk).
    """
    if not enrich:
        log.info("  enrichment disabled — skipping API calls")
        return []

    org_name = fx["name"]
    contacts_out: list[dict[str, Any]] = []
    blacklist = {n.lower().strip() for n in (fx.get("apollo_contact_blacklist") or [])}

    # 1) Apollo: 5 roller per konto
    try:
        apollo_contacts = apollo.find_contacts_for_org(
            org_name,
            reveal_personal_emails=True,
            reveal_phone=False,
        )
    except apollo.ApolloError as e:
        log.warning("  apollo failed for %s: %s", org_name, e)
        apollo_contacts = []

    # Filter blacklist (wrong-company-matches etc.)
    if blacklist:
        before = len(apollo_contacts)
        apollo_contacts = [
            c for c in apollo_contacts
            if (c.full_name or "").lower().strip() not in blacklist
            and (c.first_name or "").lower().strip() + " " + (c.last_name or "").lower().strip() not in blacklist
        ]
        if len(apollo_contacts) < before:
            log.info("  blacklist filter: dropped %d → %d kontakter", before, len(apollo_contacts))

    # Dedup: samma person som matchar flera roller — behåll Executive över Technical
    _ROLE_PRIORITY = {"owner": 0, "ceo": 1, "coo": 2, "cto": 3, "plant": 4}
    seen_by_apollo_id: dict[str, Any] = {}
    for ac in apollo_contacts:
        key = ac.apollo_id or ac.full_name or ""
        if not key:
            continue
        existing = seen_by_apollo_id.get(key)
        if existing is None:
            seen_by_apollo_id[key] = ac
        else:
            # Keep the one with stronger role priority
            if _ROLE_PRIORITY.get(ac.role_key, 99) < _ROLE_PRIORITY.get(existing.role_key, 99):
                seen_by_apollo_id[key] = ac
    apollo_contacts = list(seen_by_apollo_id.values())

    # 2) Lusha för varje Apollo-hittad person — primärt för att få mobile
    for ac in apollo_contacts:
        lusha_contact = None
        if ac.email:
            try:
                lusha_contact = lusha.enrich_by_email(ac.email)
            except lusha.LushaError as e:
                log.warning("  lusha (email) failed for %s: %s", ac.email, e)
        if lusha_contact is None and ac.full_name:
            try:
                lusha_contact = lusha.enrich_by_name_and_company(ac.full_name, org_name)
            except lusha.LushaError as e:
                log.warning("  lusha (name) failed for %s: %s", ac.full_name, e)

        merged = {
            # Apollo-data
            "role_key": ac.role_key,
            "role_label_sv": ac.role_label_sv,
            "role_category": ac.role_category,
            "apollo_id": ac.apollo_id,
            "first_name": ac.first_name,
            "last_name": ac.last_name,
            "full_name": ac.full_name,
            "title": ac.title,
            "email": ac.email,
            "email_status": ac.email_status,
            "linkedin_url": ac.linkedin_url,
            "seniority": ac.seniority,
            # Lusha-data (override/komplement)
            "mobile": None,
            "lusha_phones": [],
            "lusha_email_confidence": None,
            "country": None,
        }
        if lusha_contact:
            merged["mobile"] = lusha_contact.primary_mobile
            merged["lusha_phones"] = [
                {"number": p.number, "type": p.phone_type} for p in lusha_contact.phones
            ]
            if lusha_contact.emails and not merged.get("email"):
                merged["email"] = lusha_contact.primary_email
            # Confidence från Lusha för email
            for e in lusha_contact.emails:
                if e.email == merged.get("email"):
                    merged["lusha_email_confidence"] = e.confidence
                    break
            if not merged.get("linkedin_url") and lusha_contact.linkedin_url:
                merged["linkedin_url"] = lusha_contact.linkedin_url
            if not merged.get("title") and lusha_contact.title:
                merged["title"] = lusha_contact.title
            merged["country"] = lusha_contact.country
        contacts_out.append(merged)

    return contacts_out


# --- Map enriched contacts → frontend DecisionMaker shape --------------------


def _enriched_contacts_to_frontend(
    enriched: list[dict[str, Any]],
    fx_fallback_dms: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merga Apollo+Lusha-enriched contacts med fixture-placeholders.

    Apollo-träffar går först (riktig data). Fixture-placeholders ("Sök: ...")
    läggs efter — fyller slots för roller där Apollo inte hittade någon.
    Filtrera bort fixture-placeholders vars role redan har Apollo-träff.
    """
    out: list[dict[str, Any]] = []
    apollo_role_keys: set[str] = set()

    for c in enriched:
        if c.get("role_key"):
            apollo_role_keys.add(c["role_key"])
        out.append({
            "name": c.get("full_name") or (c.get("first_name") or "Sök") + " ?",
            "title": c.get("title") or c.get("role_label_sv") or "",
            "role": c.get("role_category", "Other"),
            "role_label_sv": c.get("role_label_sv"),
            "priority": "high" if c.get("role_category") == "Executive" else "medium",
            "email": c.get("email"),
            "email_confidence": c.get("lusha_email_confidence"),
            "phone": c.get("mobile"),
            "linkedin": c.get("linkedin_url"),
            "country": c.get("country"),
            "seniority": c.get("seniority"),
            "source": "apollo+lusha",
        })

    # Lägg på fixture-placeholders + hand-curaterade kontakter
    apollo_emails = {(c.get("email") or "").lower() for c in enriched if c.get("email")}
    for dm in fx_fallback_dms or []:
        if "name" in dm:
            # Hand-curaterad person — alltid med, men skippa om Apollo redan
            # gav samma email
            fx_email = (dm.get("email") or "").lower()
            if fx_email and fx_email in apollo_emails:
                continue
            out.append({
                "name": dm["name"], "title": dm.get("title", ""),
                "role": dm.get("role", "Other"),
                "role_label_sv": dm.get("role_label_sv"),
                "priority": dm.get("priority", "medium"),
                "email": dm.get("email"),
                "phone": dm.get("phone"),
                "linkedin": dm.get("linkedin"),
                "note": dm.get("note"),
                "source": "fixture-curated",
            })
        elif "search" in dm:
            # Skippa placeholder om motsvarande Apollo-roll redan finns
            search_term = dm["search"].lower()
            already_filled = False
            for ar in apollo_role_keys:
                role_keywords = {
                    "owner": ["ägare", "owner", "grundare"],
                    "ceo": ["vd", "ceo"],
                    "coo": ["coo", "produktionschef", "operations"],
                    "cto": ["teknikchef", "cto", "teknisk"],
                    "plant": ["fabrikschef", "plant", "platschef"],
                }
                if any(kw in search_term for kw in role_keywords.get(ar, [])):
                    already_filled = True
                    break
            if already_filled:
                continue
            out.append({
                "name": f"Sök: {dm['search']}", "title": dm["search"],
                "role": dm.get("role", "Other"),
                "priority": dm.get("priority", "medium"),
                "email": None, "phone": None, "linkedin": None,
                "note": dm.get("note"),
                "source": "placeholder",
            })
    return out


# --- Build frontend Company entry --------------------------------------------


def _account_to_company(
    fx: dict[str, Any],
    score: FitScore,
    account: Account,
    enriched_contacts: list[dict[str, Any]],
) -> dict[str, Any]:
    notes_parts: list[str] = []
    if account.salesperson_rationale:
        notes_parts.append(f"Rationale: {account.salesperson_rationale.strip()}")
    if account.disqualification_notes:
        notes_parts.append(f"Disqualification: {account.disqualification_notes}")
    if score.overrides:
        notes_parts.append("Score overrides: " + " | ".join(score.overrides))

    decision_makers = _enriched_contacts_to_frontend(enriched_contacts, fx.get("decision_makers", []))

    return {
        "id": fx["id"],
        "name": fx["name"],
        "country": {"SE": "Sverige", "NO": "Norge", "DK": "Danmark", "FI": "Finland"}.get(
            fx.get("country", "SE"), fx.get("country", "SE")
        ),
        "city": fx.get("city") or fx.get("geo") or "",
        "segment": fx.get("segment") or "",
        "priority": score.tier,
        "status": "new",
        "assignedTo": fx.get("salesperson_assigned"),
        "deadline": None,
        "description": fx.get("description", "").strip(),
        "sowPotential": fx.get("sow_potential", ""),
        "triggers": fx.get("triggers", []) or [],
        "decisionMakers": decision_makers,
        "entryAngles": fx.get("entry_angles", []) or [],
        "qualifyingQuestions": fx.get("qualifying_questions", []) or [],
        "notes": "\n\n".join(notes_parts) if notes_parts else None,
        "nextSteps": None,
        "qualifierAnswers": [],
        "lis": {
            "tier": score.tier,
            "scoreTotal": score.total,
            "scoreBreakdown": score.breakdown.model_dump(),
            "reasons": score.reasons,
            "overrides": score.overrides,
            "confidence": score.confidence,
            "signals": [
                {
                    "type": s["type"],
                    "detail": s.get("detail", ""),
                    "date": s.get("date"),
                    "evidenceSource": s.get("evidence_source"),
                }
                for s in fx.get("signals", []) or []
            ],
            "district": fx.get("district"),
            "competitorIncumbent": account.competitor_incumbent,
            "managementPriority": account.management_priority,
            "rationaleKlas": fx.get("rationale_klas", "").strip(),
            "flaggedBy": fx.get("flagged_by"),  # {name, role, date} om internt flaggat
            "hasBrief": False,  # filled by brief-loader
        },
    }


# --- Brief loader ------------------------------------------------------------


def _load_brief(account_id: str) -> IntelligencePack | None:
    """Läs hand-curaterad IntelligencePack från demo/briefs/<id>.json."""
    path = BRIEFS_SEED / f"{account_id}.json"
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return IntelligencePack.model_validate(raw)


# --- Main --------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-enrich", action="store_true",
                        help="Hoppa Apollo/Lusha-anrop (cache-only / fallback till fixture-dms)")
    parser.add_argument("--account", help="Kör bara en konto (id)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )
    # Quiet httpx noise unless verbose
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)

    fixtures_path = HERE / "demo_fixtures.yaml"
    data = yaml.safe_load(fixtures_path.read_text(encoding="utf-8"))
    accounts = data["accounts"]
    if args.account:
        accounts = [a for a in accounts if a["id"] == args.account]
        if not accounts:
            sys.exit(f"konto '{args.account}' finns inte i fixtures")

    companies: list[dict[str, Any]] = []
    briefs_dir = FRONTEND_DATA / "briefs"
    briefs_dir.mkdir(parents=True, exist_ok=True)

    apollo_credits = 0
    lusha_credits = 0
    briefs_written = 0

    print(f"\n{'Konto':28s} {'Tier':5s} {'Apollo-hits':<12s} {'Brief':<10s} Notes")
    print("-" * 90)
    for fx in accounts:
        account = _build_account(fx)
        enriched = _enrich_account(fx, enrich=not args.no_enrich)
        result = score_account(ScoringInput(account=account))
        company = _account_to_company(fx, result, account, enriched)

        # Brief
        brief = _load_brief(fx["id"])
        if brief:
            (briefs_dir / f"{fx['id']}.json").write_text(
                json.dumps(brief.model_dump(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            company["lis"]["hasBrief"] = True
            briefs_written += 1

        companies.append(company)

        # Tally credits used (approx — Apollo/Lusha skipped cached hits)
        for c in enriched:
            if c.get("email"):
                apollo_credits += 0  # cached after first run
            if c.get("mobile"):
                lusha_credits += 0  # cached after first run

        print(f"{fx['name']:28s} {result.tier:5s} "
              f"{len(enriched):<12d} {'yes' if brief else '-':<10s} "
              f"score={result.total} conf={result.confidence}")

    out_path = FRONTEND_DATA / "companies.json"
    out_path.write_text(json.dumps(companies, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nWrote {len(companies)} accounts → {out_path.relative_to(PROJECT_ROOT)}")
    print(f"Briefs written: {briefs_written}/{len(accounts)} → {briefs_dir.relative_to(PROJECT_ROOT)}/")


if __name__ == "__main__":
    main()
