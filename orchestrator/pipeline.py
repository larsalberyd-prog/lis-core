"""End-to-end pipeline glue: raw events → signals → discovery → scoring.

Detta är minimal-orkestrering för Sprint 1 (v1.0). Ingen state-maskin, ingen
async, inga retries — bara sekventiella funktionsanrop. Mer komplex
orkestrering (parallelisering, partial-failure-recovery, observability) kommer
i Sprint 2 när vi har riktiga nightly-körningar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from agents.discovery.agent import run as discovery_run
from agents.discovery.schema import (
    DiscoveredAccount,
    DiscoveryAgentInput,
    Resolver,
)
from agents.scoring import run as scoring_run
from agents.signals.agent import run as signals_run
from agents.signals.schema import RawEvent, SignalsAgentInput
from schemas.account import Account
from schemas.scoring import FitScore, ScoringInput


@dataclass
class PipelineResult:
    new_accounts_with_scores: list[tuple[Account, FitScore]] = field(default_factory=list)
    known_account_signals: list[DiscoveredAccount] = field(default_factory=list)
    rejected: list[DiscoveredAccount] = field(default_factory=list)
    skipped_event_count: int = 0
    classified_signal_count: int = 0


def run_pipeline(
    events: Sequence[RawEvent],
    *,
    known_accounts: Sequence[Account] = (),
    anti_portfolio_keywords: Sequence[str] = (),
    resolver: Resolver | None = None,
    use_llm_signals: bool = False,
) -> PipelineResult:
    """Kör hela kedjan synkront.

    `known_accounts` är listan vi redan har i CRM/seed — discovery dedupar
    mot den och routar signaler till befintliga konton istället för att skapa
    nya stubbar.
    """

    # 1. Signals
    signals_out = signals_run(SignalsAgentInput(events=list(events), use_llm=use_llm_signals))

    # 2. Discovery
    known_names = list({a.name for a in known_accounts} | {alias for a in known_accounts for alias in a.aliases})
    discovery_out = discovery_run(
        DiscoveryAgentInput(
            classified_signals=signals_out.classified,
            known_account_names=known_names,
            anti_portfolio_keywords=list(anti_portfolio_keywords),
        ),
        resolver=resolver,
    )

    # 3. Scoring per ny stub
    result = PipelineResult(
        skipped_event_count=signals_out.skipped_event_count,
        classified_signal_count=len(signals_out.classified),
    )
    for disc in discovery_out.discovered:
        if disc.decision == "new_account" and disc.account is not None:
            score = scoring_run(ScoringInput(account=disc.account))
            result.new_accounts_with_scores.append((disc.account, score))
        elif disc.decision == "known_account":
            result.known_account_signals.append(disc)
        else:
            result.rejected.append(disc)

    return result
