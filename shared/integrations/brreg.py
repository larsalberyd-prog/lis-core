"""Brønnøysundregistrene — Norwegian company register.

Endpoint: https://data.brreg.no/enhetsregisteret/api/enheter
Docs: https://data.brreg.no/enhetsregisteret/api/docs/index.html

Free, no key. Used by discovery to resolve NO Account-stubs from a name or
org-number, and by enrichment to pull NACE code + employee count.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

BRREG_ENHETER = "https://data.brreg.no/enhetsregisteret/api/enheter"

# NACE-koder (NO uses same 2007 standard as SE) we treat as CNC-relevant.
CNC_NACE_PREFIXES: tuple[str, ...] = (
    "25.",  # Metal products
    "28.",  # Machinery & equipment
    "29.",  # Motor vehicles
    "30.",  # Other transport equipment (ships, aircraft)
    "33.1",  # Repair of metal products / machinery
)


@dataclass
class BrregEnhet:
    orgnr: str
    name: str
    nace_code: str | None
    nace_text: str | None
    employees: int | None
    municipality: str | None
    municipality_no: str | None
    org_form: str | None
    incorporated: str | None
    source_url: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_cnc_relevant(self) -> bool:
        if not self.nace_code:
            return False
        return any(self.nace_code.startswith(p) for p in CNC_NACE_PREFIXES)


def _to_enhet(item: dict[str, Any]) -> BrregEnhet:
    nace = item.get("naeringskode1") or {}
    forretningsadr = item.get("forretningsadresse") or {}
    orgform = item.get("organisasjonsform") or {}
    orgnr = str(item.get("organisasjonsnummer", ""))
    return BrregEnhet(
        orgnr=orgnr,
        name=item.get("navn", "") or "",
        nace_code=nace.get("kode"),
        nace_text=nace.get("beskrivelse"),
        employees=item.get("antallAnsatte"),
        municipality=forretningsadr.get("kommune"),
        municipality_no=forretningsadr.get("kommunenummer"),
        org_form=orgform.get("kode"),
        incorporated=item.get("stiftelsesdato"),
        source_url=f"https://virksomhet.brreg.no/nb/oppslag/enheter/{orgnr}",
        raw=item,
    )


def search_by_name(
    name: str,
    *,
    limit: int = 20,
    municipality_no: str | None = None,
    client: httpx.Client | None = None,
) -> list[BrregEnhet]:
    """Search Brønnøysund for companies matching `name`. Most relevant first."""
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=20.0, headers={"accept": "application/json"})

    params: dict[str, Any] = {"navn": name, "size": limit}
    if municipality_no:
        params["kommunenummer"] = municipality_no

    try:
        r = client.get(BRREG_ENHETER, params=params)
        r.raise_for_status()
        payload = r.json()
        embedded = payload.get("_embedded") or {}
        enheter = embedded.get("enheter", [])
        return [_to_enhet(e) for e in enheter]
    finally:
        if owns_client:
            client.close()


def lookup_by_orgnr(
    orgnr: str,
    *,
    client: httpx.Client | None = None,
) -> BrregEnhet | None:
    """Direct lookup by 9-digit Norwegian org-number."""
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=20.0, headers={"accept": "application/json"})

    try:
        r = client.get(f"{BRREG_ENHETER}/{orgnr}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return _to_enhet(r.json())
    finally:
        if owns_client:
            client.close()
