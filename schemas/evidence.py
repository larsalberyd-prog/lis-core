from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl

SourceType = Literal[
    "jobtech",
    "nav_pam_stilling",
    "brreg",
    "allabolag",
    "proff",
    "press",
    "cision",
    "mynewsdesk",
    "ted",
    "doffin",
    "vinnova",
    "tillvaxtverket",
    "innovasjon_norge",
    "linkedin",
    "expert_input",
    "crm_dynamics365",
    "seed_yaml",
]


class Evidence(BaseModel):
    """A single piece of evidence backing a claim. Every claim must carry at least one.

    Validator pipeline checks: URL format, reachability, and that source_quote is a
    substring of the fetched page (fuzzy ≥0.85). Hallucinations die at the schema gate.
    """

    source_url: HttpUrl | None = None
    source_quote: str = Field(min_length=10, max_length=500)
    extracted_at: datetime
    source_type: SourceType
