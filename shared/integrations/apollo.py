"""Apollo.io contact enrichment adapter.

Hämtar beslutsfattare för Ravemas 5 målroller (Ägare, VD/CEO, COO/Produktionschef,
Teknikchef, Fabrikschef) per konto. Cache:ar resultat lokalt så vi inte slösar
credits vid omkörning.

Flow:
    1. mixed_people/api_search  — gratis, returnerar masked records (first_name +
       title + apollo_id, men last_name + email maskade)
    2. people/match              — 1 credit per call, unmask:ar last_name + email
       + LinkedIn-URL + phone-history för en specifik person

Cache:
    SQLite-fil per (org_name, role_key, first_name) → full enriched dict.
    Re-runs läser från cache utan att rör Apollo-API:n.

Cost target:
    8 demo-konton × 5 roller = 40 credits max (om alla roller hittas).
    Av Apollos Basic-plans 2500 credits → 1.6% av kvoten.

Användning:
    from shared.integrations.apollo import find_contacts_for_org
    contacts = find_contacts_for_org("Aker Solutions", role_keys=("ceo","coo"))
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Literal

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

APOLLO_BASE = "https://api.apollo.io/api/v1"
DEFAULT_CACHE = Path(__file__).resolve().parents[2] / "demo" / ".apollo_cache.sqlite"

# --- Target roles per project-ravema-target-roles --------------------------

RoleKey = Literal["owner", "ceo", "coo", "cto", "plant"]

TARGET_ROLES: dict[RoleKey, list[str]] = {
    "owner": ["Owner", "Founder", "Co-Founder", "Principal", "Ägare", "Grundare", "Huvudägare"],
    "ceo": ["CEO", "Chief Executive Officer", "President", "Managing Director",
            "VD", "Verkställande direktör", "Koncern-VD", "Adm. direktør"],
    "coo": ["COO", "Chief Operating Officer", "Operations Director",
            "Head of Operations", "Head of Production",
            "Produktionschef", "Driftchef", "Operations Manager"],
    "cto": ["CTO", "Chief Technology Officer", "Technical Director",
            "Engineering Director", "VP Engineering", "R&D Director",
            "Teknikchef", "Teknisk chef", "R&D-chef"],
    "plant": ["Plant Manager", "Factory Manager", "Site Manager",
              "Production Manager", "Manufacturing Manager",
              "Fabrikschef", "Platschef", "Anläggningschef", "Bruks-chef"],
}

ROLE_LABELS_SV: dict[RoleKey, str] = {
    "owner": "Ägare",
    "ceo": "VD / CEO",
    "coo": "COO / Produktionschef",
    "cto": "Teknikchef",
    "plant": "Fabrikschef",
}

ROLE_CATEGORY: dict[RoleKey, Literal["Executive", "Technical"]] = {
    "owner": "Executive",
    "ceo": "Executive",
    "coo": "Technical",
    "cto": "Technical",
    "plant": "Technical",
}


# --- Output schema ----------------------------------------------------------


class ApolloContact(BaseModel):
    """Slutgiltig kontakt-post efter enrichment, säker att rendra i frontend."""

    role_key: RoleKey
    role_label_sv: str
    role_category: Literal["Executive", "Technical"]

    apollo_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    title: str | None = None

    email: str | None = None
    email_status: str | None = None  # "verified", "guessed", "unavailable"
    linkedin_url: str | None = None
    phone_numbers: list[str] = Field(default_factory=list)

    seniority: str | None = None
    organization_name: str | None = None
    organization_id: str | None = None

    enrichment_credits_used: int = 0
    enriched_at: float | None = None


# --- Cache layer ------------------------------------------------------------


def _open_cache(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS apollo_cache (
            cache_key TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            stored_at REAL NOT NULL
        )"""
    )
    return conn


def _cache_get(conn: sqlite3.Connection, key: str) -> dict | None:
    row = conn.execute(
        "SELECT payload FROM apollo_cache WHERE cache_key=?", (key,)
    ).fetchone()
    return json.loads(row[0]) if row else None


def _cache_set(conn: sqlite3.Connection, key: str, kind: str, payload: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO apollo_cache(cache_key, kind, payload, stored_at) VALUES (?,?,?,?)",
        (key, kind, json.dumps(payload, ensure_ascii=False), time.time()),
    )
    conn.commit()


# --- HTTP layer -------------------------------------------------------------


class ApolloError(RuntimeError):
    pass


def _client(api_key: str, timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        base_url=APOLLO_BASE,
        headers={
            "Content-Type": "application/json",
            "X-Api-Key": api_key,
            "Cache-Control": "no-cache",
        },
        timeout=timeout,
    )


def _search_org_role(client: httpx.Client, org_name: str, titles: list[str], per_page: int = 5) -> list[dict[str, Any]]:
    """Free Apollo search — returns masked records with first_name + title + id."""
    body = {
        "q_organization_name": org_name,
        "person_titles": titles,
        "page": 1,
        "per_page": per_page,
    }
    resp = client.post("/mixed_people/api_search", json=body)
    if resp.status_code != 200:
        log.warning("Apollo search failed for %s / %s: %d", org_name, titles[:2], resp.status_code)
        return []
    data = resp.json()
    return data.get("people", []) or []


def _match_person(
    client: httpx.Client,
    org_name: str,
    apollo_id: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    email: str | None = None,
    reveal_personal_emails: bool = False,
    reveal_phone: bool = False,
) -> dict[str, Any] | None:
    """Apollo /people/match — 1 credit per unmasked person.

    Prefer matching by `apollo_id` (most reliable, esp. for Nordic data where
    fuzzy first_name+org-match often returns empty fields). Fall back to
    first_name+org or email if no id available.
    """
    params: dict[str, Any] = {}
    if reveal_personal_emails:
        params["reveal_personal_emails"] = "true"
    if reveal_phone:
        params["reveal_phone_number"] = "true"

    body: dict[str, Any] = {}
    if apollo_id:
        body["id"] = apollo_id
    else:
        body["organization_name"] = org_name
        if email:
            body["email"] = email
        if first_name:
            body["first_name"] = first_name
        if last_name:
            body["last_name"] = last_name

    resp = client.post("/people/match", params=params, json=body)
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        log.warning("Apollo match failed for %s / id=%s name=%s: %d %s",
                    org_name, apollo_id, first_name, resp.status_code, resp.text[:200])
        return None
    data = resp.json()
    person = data.get("person")
    if not person or not isinstance(person, dict):
        return None
    return person


# --- High-level adapter -----------------------------------------------------


def _extract_phones(match_payload: dict[str, Any]) -> list[str]:
    phones: list[str] = []
    raw_phones = match_payload.get("phone_numbers") or []
    if isinstance(raw_phones, list):
        for ph in raw_phones:
            if isinstance(ph, dict):
                num = ph.get("sanitized_number") or ph.get("raw_number")
                if num and num not in phones:
                    phones.append(num)
            elif isinstance(ph, str):
                phones.append(ph)
    return phones


def _shape_contact(
    role_key: RoleKey,
    search_hit: dict[str, Any],
    match_payload: dict[str, Any] | None,
    credits_used: int,
) -> ApolloContact:
    """Merge masked search-hit with optional unmasked match-payload into ApolloContact."""
    src = match_payload or {}
    org = src.get("organization") if isinstance(src.get("organization"), dict) else None

    first_name = src.get("first_name") or search_hit.get("first_name")
    last_name = src.get("last_name")  # only present after match
    full_name = src.get("name")
    if full_name is None and first_name and last_name:
        full_name = f"{first_name} {last_name}"

    return ApolloContact(
        role_key=role_key,
        role_label_sv=ROLE_LABELS_SV[role_key],
        role_category=ROLE_CATEGORY[role_key],
        apollo_id=src.get("id") or search_hit.get("id"),
        first_name=first_name,
        last_name=last_name,
        full_name=full_name,
        title=src.get("title") or search_hit.get("title"),
        email=src.get("email"),
        email_status=src.get("email_status"),
        linkedin_url=src.get("linkedin_url"),
        phone_numbers=_extract_phones(src),
        seniority=src.get("seniority"),
        organization_name=(org or {}).get("name") if org else search_hit.get("organization", {}).get("name"),
        organization_id=(org or {}).get("id") if org else None,
        enrichment_credits_used=credits_used,
        enriched_at=time.time() if match_payload else None,
    )


def find_contacts_for_org(
    org_name: str,
    role_keys: Iterable[RoleKey] | None = None,
    *,
    api_key: str | None = None,
    cache_path: Path | None = None,
    enrich: bool = True,
    reveal_personal_emails: bool = False,
    reveal_phone: bool = False,
    search_per_role: int = 3,
) -> list[ApolloContact]:
    """Hämta beslutsfattare för en organisation, en per målroll.

    Plockar bästa kandidaten per roll (första search-träffen vars title innehåller
    en av role-keywords) och enrich:ar den via /people/match.

    Args:
        org_name: Bolagsnamn att söka i Apollo (matchas mot organization).
        role_keys: Delmängd av målroller att hämta (default: alla 5).
        api_key: Apollo API-key (default: $APOLLO_API_KEY).
        cache_path: Path till SQLite-cache (default: lis-core/demo/.apollo_cache.sqlite).
        enrich: Om False, returneras bara masked search-results (0 credits).
        reveal_personal_emails / reveal_phone: Apollo flags som kan kosta extra.
        search_per_role: Hur många kandidater hämtas per roll innan vi väljer en.

    Returns:
        Lista med ApolloContact (en per roll som hittades). Tomma roller hoppas över.
    """
    api_key = api_key or os.environ.get("APOLLO_API_KEY")
    if not api_key:
        raise ApolloError("APOLLO_API_KEY saknas i miljön")
    cache_path = cache_path or DEFAULT_CACHE
    role_keys = list(role_keys) if role_keys else list(TARGET_ROLES.keys())

    out: list[ApolloContact] = []

    with _open_cache(cache_path) as conn, _client(api_key) as client:
        for role_key in role_keys:
            search_cache_key = f"search:{org_name.lower()}:{role_key}"
            cached_search = _cache_get(conn, search_cache_key)
            if cached_search is None:
                hits = _search_org_role(client, org_name, TARGET_ROLES[role_key], per_page=search_per_role)
                _cache_set(conn, search_cache_key, "search", {"hits": hits})
            else:
                hits = cached_search["hits"]

            if not hits:
                log.info("Apollo: %s / %s — no candidates", org_name, role_key)
                continue

            # Pick the first hit whose title contains one of the role-keywords (case-insensitive)
            chosen = None
            role_lower = [t.lower() for t in TARGET_ROLES[role_key]]
            for h in hits:
                title = (h.get("title") or "").lower()
                if any(rl in title for rl in role_lower):
                    chosen = h
                    break
            if chosen is None:
                chosen = hits[0]  # fall back to highest-ranked

            if not enrich:
                out.append(_shape_contact(role_key, chosen, None, 0))
                continue

            # Enrich via /people/match — 1 credit. Prefer id-based match (more
            # reliable for Nordic data than first_name fuzzy match).
            chosen_id = chosen.get("id")
            chosen_first_name = chosen.get("first_name")
            match_cache_key = f"match:{org_name.lower()}:{role_key}:id={chosen_id or '?'}"
            cached_match = _cache_get(conn, match_cache_key)
            if cached_match is None:
                payload = _match_person(
                    client, org_name,
                    apollo_id=chosen_id,
                    first_name=chosen_first_name,  # fallback only
                    reveal_personal_emails=reveal_personal_emails,
                    reveal_phone=reveal_phone,
                )
                _cache_set(conn, match_cache_key, "match", {"payload": payload})
                credits_used = 1 if payload else 0
            else:
                payload = cached_match["payload"]
                credits_used = 0  # already paid for; cache hit

            out.append(_shape_contact(role_key, chosen, payload, credits_used))

    return out


# --- CLI quick-probe --------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    name = sys.argv[1] if len(sys.argv) > 1 else "Aker Solutions"
    contacts = find_contacts_for_org(name)
    print(f"\n{name}: {len(contacts)} kontakter")
    total_credits = 0
    for c in contacts:
        total_credits += c.enrichment_credits_used
        print(f"  [{c.role_label_sv:25s}] {c.full_name or '(no name)':30s} | {(c.title or '?')[:40]:40s} | email: {c.email or '-'} | linkedin: {'yes' if c.linkedin_url else '-'}")
    print(f"  credits använda denna run: {total_credits}")
