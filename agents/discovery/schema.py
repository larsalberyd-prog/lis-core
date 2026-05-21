"""Discovery-agentens I/O-kontrakt.

Discovery tar emot råa events (typiskt redan klassificerade till `ClassifiedSignal`
av signals-agenten) och producerar `Account`-stubbar — minimal Account-data med
det vi kan härleda från employer-fält + Brreg/NACE-lookup. Stubbarna är inputs
till enrichment-agenten i nästa steg.
"""

from __future__ import annotations

from typing import Callable, Literal

from pydantic import BaseModel, Field

from schemas.account import Account

from agents.signals.schema import ClassifiedSignal

# Resolver = callable(name | None, orgnr | None, country) -> ResolvedCompany | None
# Injekteras i tester så vi inte träffar Brreg-API i CI.


class ResolvedCompany(BaseModel):
    """Det vi lärt oss av Brreg/JobTech om en employer."""

    name: str
    orgnr: str | None = None
    country: Literal["SE", "NO", "DK", "FI"] = "SE"
    municipality: str | None = None
    nace_code: str | None = None
    nace_text: str | None = None
    employees: int | None = None
    is_cnc_relevant: bool = False
    source_url: str | None = None


DiscoveryDecision = Literal["new_account", "known_account", "rejected_not_cnc", "rejected_anti_portfolio", "rejected_missing_employer"]


class DiscoveredAccount(BaseModel):
    """En signal som producerade (eller borde producerat) ett konto.

    Decisions:
      - new_account: ny stub redo för enrichment + scoring
      - known_account: signal ska attacheras till existerande Account
      - rejected_not_cnc: NACE / kontext säger att employer inte är CNC-relevant
      - rejected_anti_portfolio: employer matchar anti-portfolio (FC Maskin etc)
      - rejected_missing_employer: signal saknar employer-namn, kan inte routas
    """

    decision: DiscoveryDecision
    account: Account | None = None
    matched_known_name: str | None = None
    rejection_reason: str | None = None
    classified_signal: ClassifiedSignal


class DiscoveryAgentInput(BaseModel):
    classified_signals: list[ClassifiedSignal]
    known_account_names: list[str] = Field(default_factory=list)
    anti_portfolio_keywords: list[str] = Field(default_factory=list)


class DiscoveryAgentOutput(BaseModel):
    discovered: list[DiscoveredAccount] = Field(default_factory=list)

    @property
    def new_accounts(self) -> list[Account]:
        return [d.account for d in self.discovered if d.decision == "new_account" and d.account]

    @property
    def signals_for_known(self) -> list[DiscoveredAccount]:
        return [d for d in self.discovered if d.decision == "known_account"]


# Resolver-signatur som agenten konsumerar. Default-impl träffar Brreg/JobTech;
# tester passar in en fake.
Resolver = Callable[[str | None, str | None, str], ResolvedCompany | None]
