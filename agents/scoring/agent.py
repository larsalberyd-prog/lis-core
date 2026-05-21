"""Scoring agent — fit-score per SCORING.md, with Mode B floor logic.

v0 (Mode B per plan): no live enrichment, no real signals. We score from what we have:
  - signals_at_capture (expert-flagged signals from Klas/Nejra email)
  - segment + portfolio fit
  - sales-curation source (icp-md-anchors / klas-excel / nejra-email)
  - management_priority + active_dialogue overrides

The point-based score (SCORING.md §1) still runs, but we layer a **tier floor** on top
that reflects that ICP.md anchors and Klas/Nejra accounts are *pre-vetted strategic
prospects* — they belong in the AA-AAA range by definition until enrichment data either
confirms or downgrades them. This is the "syntetiska vinster" Mode B from the plan.

The LLM is *not* called for numeric scoring (would hallucinate points). Reserved for
narrative `reasons[]` rendering, behind `with_narrative=False` default so eval runs free.
"""

from __future__ import annotations

from schemas.account import Account
from schemas.scoring import (
    FitScore,
    ScoreBreakdown,
    ScoringInput,
    Tier,
    TIER_ORDER,
    tier_from_total,
)
from schemas.signal import IndustrialSignalType

# --- Signal scoring (SCORING.md §4) ----------------------------------------

_SIGNAL_POINTS: dict[IndustrialSignalType, int] = {
    IndustrialSignalType.CAPEX_ANNOUNCEMENT: 10,
    IndustrialSignalType.PLANT_EXPANSION: 7,
    IndustrialSignalType.GEOGRAPHIC_EXPANSION: 5,
    IndustrialSignalType.NEW_FACILITY_BUILDING_PERMIT: 3,
    IndustrialSignalType.HIRE_OPERATIONS_DIRECTOR: 8,
    IndustrialSignalType.HIRE_PRODUCTION_MANAGER: 7,
    IndustrialSignalType.HIRE_MANUFACTURING_ENGINEER: 4,
    IndustrialSignalType.HIRE_AFTER_SALES_MANAGER: 6,
    IndustrialSignalType.HIRE_CNC_OPERATORS_BATCH: 8,
    IndustrialSignalType.HIRE_CNC_ROLE: 4,
    IndustrialSignalType.OWNERSHIP_CHANGE: 3,
    IndustrialSignalType.CEO_TRANSITION: 2,
    IndustrialSignalType.OWNERSHIP_GROUP_INVESTMENT: 4,
    IndustrialSignalType.CERTIFICATION_CHANGE: 4,
    IndustrialSignalType.MAJOR_CUSTOMER_WIN: 5,
    IndustrialSignalType.MAJOR_CUSTOMER_LOSS: 3,
    IndustrialSignalType.SUPPLIER_SWITCH_SIGNAL: 6,
    IndustrialSignalType.REGULATORY_PRESSURE: 3,
    IndustrialSignalType.QUARTERLY_REPORT_TRIGGER: 2,
    IndustrialSignalType.PUBLIC_PROCUREMENT_WIN: 5,
    IndustrialSignalType.FUNDING_GRANT: 4,
    IndustrialSignalType.ACTIVE_SALES_DIALOGUE: 15,
}

_STRONG_CAPEX_SIGNALS = {
    IndustrialSignalType.CAPEX_ANNOUNCEMENT,
    IndustrialSignalType.PLANT_EXPANSION,
    IndustrialSignalType.GEOGRAPHIC_EXPANSION,
}

_HIRE_SIGNALS = {
    IndustrialSignalType.HIRE_OPERATIONS_DIRECTOR,
    IndustrialSignalType.HIRE_PRODUCTION_MANAGER,
    IndustrialSignalType.HIRE_CNC_OPERATORS_BATCH,
    IndustrialSignalType.HIRE_CNC_ROLE,
    IndustrialSignalType.HIRE_AFTER_SALES_MANAGER,
    IndustrialSignalType.HIRE_MANUFACTURING_ENGINEER,
}

_DIM_CAP_SIGNALS = 30
_DIM_CAP_STRATEGIC = 10
_DIM_CAP_FIRMOGRAPHIC = 30
_DIM_CAP_CAPACITY = 20

# --- Strategic dimension (SCORING.md §6) -----------------------------------

_STRATEGIC_SEGMENTS = {"SE-DEF-AERO", "NO-DEF"}
_REFERENCEABLE_LOGOS = {
    "Saab Aeronautics",
    "Saab Dynamics",
    "Kongsberg Defence & Aerospace",
    "Kongsberg Maritime",
    "Volvo Cars Torslanda",
    "Volvo Cars Skövde",
    "Scania",
    "Hitachi Energy Ludvika",
    "ABB Robotics",
    "Sandvik Coromant",
    "BAE Systems Hägglunds",
    "GKN Aerospace Engine Systems",
    "Brunvoll",
    "Aker Solutions",
}
_GEO_GAP_REGIONS = {"Stavanger", "Trondheim", "Bergen"}

# --- Source-based curation tags --------------------------------------------

_SRC_ICP_ANCHOR = "icp-md-anchors"
_SRC_KLAS = "klas-excel-2026-05-20"
_SRC_NEJRA = "nejra-email-2026-05-20"
_SRC_CEO_FLAG = "ceo-priority"

# --- Segment partial-fit table (firmographic Mode B) -----------------------

_KNOWN_SEGMENT_FIT = {
    "SE-DEF-AERO": 25,
    "NO-DEF": 25,
    "SE-HEAVY": 22,
    "NO-OG": 22,
    "NO-MARINE": 22,
    "SE-MEDTECH": 20,
    "SE-AUTO-EV": 20,
    "SE-JOBSHOP-GGVV": 18,
}

# --- Capacity proxy from expected_purchase_frequency -----------------------

_FREQUENCY_CAPACITY = {"high": 15, "medium": 10, "low": 3, "unknown": 0}


def _max_tier(a: Tier, b: Tier) -> Tier:
    return a if TIER_ORDER.index(a) >= TIER_ORDER.index(b) else b


def _min_tier(a: Tier, b: Tier) -> Tier:
    return a if TIER_ORDER.index(a) <= TIER_ORDER.index(b) else b


def _score_signals(account: Account) -> tuple[int, list[str]]:
    raw = 0
    reasons: list[str] = []
    for s in account.signals_at_capture:
        pts = _SIGNAL_POINTS.get(s.type, 0)
        if pts == 0:
            continue
        raw += pts
        reasons.append(f"+{pts} p: {s.type.value} — {s.detail}")

    if account.competitor_incumbent and (
        account.displacement_window_signal or account.suggested_entry_angle
    ):
        raw += 6
        signal_text = account.displacement_window_signal or account.suggested_entry_angle or ""
        reasons.append(
            f"+6 p: displacement-fönster vs {account.competitor_incumbent} ({signal_text})"
        )

    return min(raw, _DIM_CAP_SIGNALS), reasons


def _score_strategic(account: Account) -> tuple[int, list[str]]:
    pts = 0
    reasons: list[str] = []
    if account.segment in _STRATEGIC_SEGMENTS:
        pts += 3
        reasons.append(f"+3 p: strategiskt segment {account.segment}")
    if {account.name, *account.aliases} & _REFERENCEABLE_LOGOS:
        pts += 3
        reasons.append("+3 p: referensbar logo")
    if account.management_priority:
        pts += 3
        reasons.append("+3 p: CEO-flaggad (management_priority)")
    if account.geo in _GEO_GAP_REGIONS:
        pts += 1
        reasons.append(f"+1 p: strategiskt geo-gap ({account.geo})")
    return min(pts, _DIM_CAP_STRATEGIC), reasons


def _score_firmographic_partial(account: Account) -> tuple[int, list[str]]:
    """Mode B firmographic: segment-match + sales-curation source bonus.

    Real firmographic scoring (NACE, employees, revenue, geo distance, certifications)
    lands with the enrichment agent in Sprint 1+. Until then, sales-curated source is
    our best proxy — Klas and Nejra hand-picked these accounts as ICP-fit.
    """
    pts = 0
    reasons: list[str] = []

    if account.segment and account.segment in _KNOWN_SEGMENT_FIT:
        seg_pts = _KNOWN_SEGMENT_FIT[account.segment]
        pts += seg_pts
        reasons.append(f"+{seg_pts} p (partial): segment-fit för {account.segment}")
    else:
        pts += 8
        reasons.append("+8 p (partial): okänt segment, neutral baseline")

    if _SRC_ICP_ANCHOR in account.sources:
        pts += 5
        reasons.append("+5 p: ICP.md anchor-konto (expert-curated strategic)")
    elif _SRC_NEJRA in account.sources and _SRC_KLAS in account.sources:
        pts += 5
        reasons.append("+5 p: dubbel-curated av Klas + Nejra")
    elif _SRC_NEJRA in account.sources:
        pts += 4
        reasons.append("+4 p: Nejra-validerad (säljar-curated)")
    elif _SRC_KLAS in account.sources:
        pts += 3
        reasons.append("+3 p: Klas-curated (säljchef ICP-lista)")

    return min(pts, _DIM_CAP_FIRMOGRAPHIC), reasons


def _score_capacity_proxy(account: Account) -> tuple[int, list[str]]:
    """Capacity (Mode B): proxy from expected_purchase_frequency.

    Real capacity scoring (revenue growth, EBITDA, fixed-assets, solidity, credit rating)
    requires Allabolag/Proff enrichment — not available in v0. Frequency is the säljar-
    estimated proxy for deal cadence.
    """
    pts = _FREQUENCY_CAPACITY.get(account.expected_purchase_frequency, 0)
    if pts == 0:
        return 0, []
    return pts, [
        f"+{pts} p (proxy): expected_purchase_frequency={account.expected_purchase_frequency}"
    ]


# --- Tier floor / ceiling --------------------------------------------------


def _compute_tier_ceiling(account: Account) -> Tier | None:
    """Cap from above. Returns max allowed tier, or None for no ceiling.

    Anti-fit triggers (disqualification, personal relationship blocker) hard-cap at B.
    """
    if account.disqualification_notes or account.personal_relationship_blockers:
        return "B"
    return None


def _compute_tier_floor(account: Account) -> tuple[Tier, list[str]]:
    """Lift from below — Mode B "syntetiska vinster" logic.

    Returns the minimum tier this account should receive, and the reasoning trail.
    Order matters: stronger conditions override weaker ones.
    """
    floor: Tier = "C"
    notes: list[str] = []

    has_active = any(
        s.type == IndustrialSignalType.ACTIVE_SALES_DIALOGUE for s in account.signals_at_capture
    )
    if has_active:
        notes.append("FLOOR = AAA: aktiv affär pågår")
        return "AAA", notes

    if account.management_priority:
        notes.append("FLOOR = AAA: CEO/management-flaggad")
        return "AAA", notes

    has_strong_capex = any(
        s.type in _STRONG_CAPEX_SIGNALS for s in account.signals_at_capture
    )
    has_hire = any(s.type in _HIRE_SIGNALS for s in account.signals_at_capture)
    has_displacement = bool(
        account.competitor_incumbent
        and (account.displacement_window_signal or account.suggested_entry_angle)
    )
    is_anchor = _SRC_ICP_ANCHOR in account.sources
    is_klas = _SRC_KLAS in account.sources
    is_nejra = _SRC_NEJRA in account.sources
    is_dual = is_klas and is_nejra

    if is_anchor and has_strong_capex:
        floor = "AAA"
        notes.append("FLOOR = AAA: ICP-anchor + capex/expansion-signal")
    elif is_anchor and (has_hire or has_displacement):
        floor = "AAA"
        notes.append("FLOOR = AAA: ICP-anchor + signal/displacement")
    elif is_anchor:
        floor = "AA"
        notes.append("FLOOR = AA: ICP.md anchor-konto (Sprint 1 promo → AAA om signaler)")
    elif is_dual and (has_displacement or has_strong_capex or has_hire):
        floor = "AAA"
        notes.append("FLOOR = AAA: Klas+Nejra dual-curated + signal/displacement")
    elif is_dual:
        floor = "AA"
        notes.append("FLOOR = AA: Klas+Nejra dual-curated")
    elif is_nejra and (has_displacement or has_strong_capex or has_hire):
        floor = "AAA"
        notes.append("FLOOR = AAA: Nejra-validerad + signal/displacement")
    elif is_nejra:
        floor = "AA"
        notes.append("FLOOR = AA: Nejra-validerad")
    elif is_klas:
        floor = "A"
        notes.append("FLOOR = A: Klas-curated")
    return floor, notes


def run(input: ScoringInput, *, with_narrative: bool = False) -> FitScore:
    """Score one account. Deterministic; LLM only if with_narrative=True."""
    account = input.account

    firmographic, fr_reasons = _score_firmographic_partial(account)
    capacity, cap_reasons = _score_capacity_proxy(account)
    signals_pts, sig_reasons = _score_signals(account)
    strategic, strat_reasons = _score_strategic(account)
    engagement = 0  # event-driven, not available without app-tracking

    breakdown = ScoreBreakdown(
        firmographic=firmographic,
        capacity=capacity,
        signals=signals_pts,
        engagement=engagement,
        strategic=strategic,
    )
    point_tier = tier_from_total(breakdown.total)

    floor_tier, floor_notes = _compute_tier_floor(account)
    ceiling_tier = _compute_tier_ceiling(account)

    tier: Tier = _max_tier(point_tier, floor_tier)
    overrides: list[str] = list(floor_notes)

    if ceiling_tier is not None:
        capped = _min_tier(tier, ceiling_tier)
        if capped != tier:
            tier = capped
            notes_text = account.disqualification_notes or "personal_relationship_blocker"
            overrides.append(f"CEILING → {ceiling_tier}: {notes_text}")
        else:
            overrides.append(f"CEILING noted: {ceiling_tier} (no change, point-tier already at/below)")

    reasons = fr_reasons + cap_reasons + sig_reasons + strat_reasons
    confidence = "low" if (capacity == 0 and signals_pts == 0) else "medium"

    return FitScore(
        account_name=account.name,
        breakdown=breakdown,
        total=breakdown.total,
        tier=tier,
        reasons=reasons,
        overrides=overrides,
        confidence=confidence,
    )
