"""Intelligence Pack schema — strukturerad brief per konto.

Matchar spec/Intelligence pack.png-layout: hypotes, källor, beslutsfattare,
pristrend, köpsignaler, prospekteringsplan, kvalificeringsfrågor, konkurrenter.

Allt typsäkert via Pydantic så LLM-genererade briefs aldrig kan saknas
required fields (validator kastar då output:en).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Urgency = Literal["sälj_nu", "varma_ledet", "långsiktig", "monitor"]


class BriefSource(BaseModel):
    """En källa/citation som backar upp en claim i briefen."""

    label: str  # ex. "Allabolag årsredovisning 2024", "Nejra e-post 2026-05-20"
    detail: str | None = None  # specifik citat eller siffra
    url: str | None = None


class BriefSignal(BaseModel):
    """En köp-/timing-signal med datum + bevis-spårning."""

    headline: str  # ex. "125 MSEK nyemission dec 2025"
    detail: str | None = None
    date: str | None = None  # ISO-stil eller "Q4 2025"
    impact: Literal["high", "medium", "low"] = "medium"
    linked_decision_maker: str | None = None  # ex. "Ny produktionschef" → fullt namn om känt


class BriefDecisionMaker(BaseModel):
    """En beslutsfattare som ska kontaktas — gruppen per Ravemas 5 målroller."""

    role_label: str  # ex. "Ägare", "VD / CEO"
    role_category: Literal["Executive", "Technical"]
    name: str | None = None
    title: str | None = None
    email: str | None = None
    mobile: str | None = None
    linkedin: str | None = None
    why_relevant: str | None = None  # 1-mening varför just denna person


class BriefCompetitor(BaseModel):
    """Konkurrent som äger kontot idag eller hotar Mazak-affären."""

    name: str  # ex. "DMG MORI", "Okuma", "Hermle"
    incumbent_machines: str | None = None  # ex. "DMG NHX, NLX-svarvar"
    displacement_angle: str | None = None  # vår vinkel


class IntelligencePack(BaseModel):
    """Strukturerad brief — renderas direkt i ravema-lis CompanyDetail-vy."""

    account_id: str  # matchar Company.id (ex. "acc-innovation")
    account_name: str
    headline_hypothesis: str  # ex. "Aker Solutions — Mazak displacement i subsea-segmentet"
    subtitle: str | None = None  # 1-rads förklaring

    urgency: Urgency = "varma_ledet"
    urgency_reason: str  # vad gör den deadline-driven

    # Sektion 1: 🎯 Hypotes
    hypothesis: str  # 3-5 meningar med affärs-hypotesen i klartext

    # Sektion 2: 📚 Källor
    sources: list[BriefSource] = Field(default_factory=list)

    # Sektion 3: 🛍 Nuvarande beslutsfattare (grupperade per roll)
    decision_makers: list[BriefDecisionMaker] = Field(default_factory=list)

    # Sektion 4: 📈 Pristrend / marknadssignaler
    market_trend: str | None = None

    # Sektion 5: 🧭 Köpsignaler
    buying_signals: list[BriefSignal] = Field(default_factory=list)

    # Sektion 6: 🛍 Prospekteringsplan (konkreta nästa steg)
    prospecting_plan: list[str] = Field(default_factory=list)

    # Sektion 7: 🔍 Kvalificeringsfrågor
    qualifying_questions: list[str] = Field(default_factory=list)

    # Sektion 8: 🎁 Konkurrenter
    competitors: list[BriefCompetitor] = Field(default_factory=list)

    # Meta — för "Generera om"-knapp och fötter
    generated_at: str | None = None  # ISO timestamp
    model_used: str | None = None  # ex. "hand-written (claude opus 4.7 in-session)" eller "claude-sonnet-4-6"
    cost_credits: float = 0.0
