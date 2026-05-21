"""Brief-agent — genererar Intelligence Pack per konto.

Två run-modes:
    run(account) → IntelligencePack via LLM (kräver ANTHROPIC_API_KEY)
    write_static(account_id) → hand-curaterad IntelligencePack (för demo utan API-nyckel)

Båda producerar samma `IntelligencePack`-objekt så frontend renderar identiskt.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import (
    BriefCompetitor,
    BriefDecisionMaker,
    BriefSignal,
    BriefSource,
    IntelligencePack,
)

log = logging.getLogger(__name__)
PROMPT_PATH = Path(__file__).parent / "prompt.md"


def _load_prompt() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def run(
    account_data: dict[str, Any],
    *,
    model: str = "claude-sonnet-4-6",
) -> IntelligencePack:
    """Genererar brief via LLM. Kräver ANTHROPIC_API_KEY + shared.llm.

    account_data ska innehålla:
      - account_id, name, segment, country, description
      - enriched_contacts: lista av Apollo+Lusha-merged ApolloContact-dicts
      - signals_at_capture: lista av SignalAtCapture-dicts
      - klas_rationale, sow_potential, etc.
    """
    try:
        from shared.llm import call_claude  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "Brief LLM-mode requires shared.llm.call_claude. "
            "Use write_static() for demo-mode without API access."
        ) from e

    system_prompt = _load_prompt()
    user_prompt = (
        "Producera EN IntelligencePack för följande konto. "
        "Returnera STRIKT JSON enligt schemat — inga prolog/markdown utanför JSON.\n\n"
        f"```json\n{json.dumps(account_data, ensure_ascii=False, indent=2)}\n```"
    )

    raw = call_claude(
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
        model=model,
        max_tokens=4000,
    )

    # raw är hela LLM-svaret — extrahera JSON
    text = raw.get("text", "") if isinstance(raw, dict) else str(raw)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(f"No JSON in LLM response: {text[:200]}")
    payload = json.loads(text[start : end + 1])
    pack = IntelligencePack.model_validate(payload)
    pack.generated_at = datetime.now(timezone.utc).isoformat()
    pack.model_used = model
    return pack


def write_static(pack: IntelligencePack, output_dir: Path) -> Path:
    """Sparar IntelligencePack som JSON-fil för frontend-konsumtion.

    output_dir/<account_id>.json
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{pack.account_id}.json"
    if not pack.generated_at:
        pack.generated_at = datetime.now(timezone.utc).isoformat()
    out_path.write_text(
        json.dumps(pack.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_path


def write_all(packs: list[IntelligencePack], output_dir: Path) -> list[Path]:
    return [write_static(p, output_dir) for p in packs]
