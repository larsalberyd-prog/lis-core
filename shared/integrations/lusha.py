"""Lusha contact enrichment adapter.

Hämtar mobilnummer + verifierad work-email för en redan-identifierad person
(typiskt efter Apollo-search). Lusha har bättre täckning än Apollo för Nordic
mid-market (mobiltelefoni specifikt).

Endpoint: POST https://api.lusha.com/v2/person
Schema:
    {
      "contacts": [
        {"contactId": "<my-key>",
         "fullName": "First Last",
         "companies": [{"name": "Org", "isCurrent": true}]
        }
      ]
    }

    Alt input per contact:
      - email: "first.last@org.com"       (mest pålitligt)
      - linkedinUrl: "https://..."        (näst bäst)
      - fullName + companies              (fuzzy match)

Response per contact: phones[], phoneNumbers[{number,phoneType,doNotCall}],
emails[], emailAddresses[{email, emailConfidence: A+/A/B/...}], jobTitle{title,
seniority}, location{country, country_iso2}, socialLinks{linkedin, twitter,
github}, previousJob{...}.

Credits: 1 Lusha credit per matched contact (oavsett om email + phone båda
returneras). isCreditCharged-fält i response indikerar om credit drogs.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Literal

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

LUSHA_BASE = "https://api.lusha.com"
DEFAULT_CACHE = Path(__file__).resolve().parents[2] / "demo" / ".lusha_cache.sqlite"


# --- Output schema ---------------------------------------------------------


class LushaPhone(BaseModel):
    number: str
    phone_type: Literal["mobile", "direct", "voip", "other", "unknown"] = "unknown"
    do_not_call: bool = False


class LushaEmail(BaseModel):
    email: str
    email_type: str | None = None  # work, personal
    confidence: str | None = None  # A+, A, B, ...


class LushaContact(BaseModel):
    """Resultat från Lusha-enrichment per person."""

    full_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    title: str | None = None
    seniority: str | None = None
    departments: list[str] = Field(default_factory=list)

    emails: list[LushaEmail] = Field(default_factory=list)
    phones: list[LushaPhone] = Field(default_factory=list)
    linkedin_url: str | None = None

    company_name: str | None = None
    company_id_lusha: int | None = None
    country: str | None = None
    country_iso2: str | None = None

    credit_charged: bool = False
    person_id_lusha: int | None = None
    enriched_at: float | None = None
    raw_error: str | None = None

    @property
    def primary_email(self) -> str | None:
        for e in self.emails:
            if e.confidence in ("A+", "A"):
                return e.email
        return self.emails[0].email if self.emails else None

    @property
    def primary_mobile(self) -> str | None:
        for p in self.phones:
            if p.phone_type == "mobile":
                return p.number
        return self.phones[0].number if self.phones else None


# --- Cache ----------------------------------------------------------------


def _open_cache(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS lusha_cache (
            cache_key TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            stored_at REAL NOT NULL
        )"""
    )
    return conn


def _cache_get(conn, key: str) -> dict | None:
    row = conn.execute("SELECT payload FROM lusha_cache WHERE cache_key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def _cache_set(conn, key: str, payload: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO lusha_cache(cache_key, payload, stored_at) VALUES (?,?,?)",
        (key, json.dumps(payload, ensure_ascii=False), time.time()),
    )
    conn.commit()


# --- HTTP -----------------------------------------------------------------


class LushaError(RuntimeError):
    pass


def _client(api_key: str, timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        base_url=LUSHA_BASE,
        headers={"api_key": api_key, "Content-Type": "application/json"},
        timeout=timeout,
    )


def _call_person_endpoint(client: httpx.Client, contacts: list[dict[str, Any]]) -> dict[str, Any]:
    """POST /v2/person — accepterar upp till 100 contacts per call."""
    resp = client.post("/v2/person", json={"contacts": contacts})
    if resp.status_code not in (200, 201):
        raise LushaError(f"Lusha /v2/person HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()


# --- Parsing --------------------------------------------------------------


_PHONE_TYPE_MAP = {
    "mobile": "mobile",
    "direct": "direct",
    "Mobile": "mobile",
    "Direct": "direct",
    "voip": "voip",
}


def _parse_contact_data(d: dict[str, Any]) -> LushaContact:
    """Map Lusha's API response shape → our LushaContact."""
    phones_in = d.get("phoneNumbers") or []
    phones_out = [
        LushaPhone(
            number=p.get("number", ""),
            phone_type=_PHONE_TYPE_MAP.get(p.get("phoneType", "unknown"), "unknown"),
            do_not_call=p.get("doNotCall", False),
        )
        for p in phones_in if isinstance(p, dict) and p.get("number")
    ]

    emails_in = d.get("emailAddresses") or []
    emails_out = [
        LushaEmail(
            email=e.get("email", ""),
            email_type=e.get("emailType"),
            confidence=e.get("emailConfidence"),
        )
        for e in emails_in if isinstance(e, dict) and e.get("email")
    ]

    location = d.get("location") or {}
    job = d.get("jobTitle") or {}
    social = d.get("socialLinks") or {}

    return LushaContact(
        full_name=d.get("fullName"),
        first_name=d.get("firstName"),
        last_name=d.get("lastName"),
        title=job.get("title"),
        seniority=job.get("seniority"),
        departments=job.get("departments", []) or [],
        emails=emails_out,
        phones=phones_out,
        linkedin_url=social.get("linkedin"),
        company_name=(d.get("previousJob") or {}).get("company", {}).get("name") or None,
        company_id_lusha=d.get("companyId"),
        country=location.get("country"),
        country_iso2=location.get("country_iso2"),
        credit_charged=True,
        person_id_lusha=d.get("personId"),
        enriched_at=time.time(),
    )


# --- High-level adapter ---------------------------------------------------


def enrich_by_email(
    email: str,
    *,
    api_key: str | None = None,
    cache_path: Path | None = None,
) -> LushaContact | None:
    """Mest pålitliga sökväg: vi har redan en email från Apollo, vill ha phone."""
    return _enrich_one({"email": email}, cache_key=f"email:{email.lower()}",
                       api_key=api_key, cache_path=cache_path)


def enrich_by_linkedin(
    linkedin_url: str,
    *,
    api_key: str | None = None,
    cache_path: Path | None = None,
) -> LushaContact | None:
    return _enrich_one({"linkedinUrl": linkedin_url}, cache_key=f"li:{linkedin_url.lower()}",
                       api_key=api_key, cache_path=cache_path)


def enrich_by_name_and_company(
    full_name: str,
    company_name: str,
    *,
    api_key: str | None = None,
    cache_path: Path | None = None,
) -> LushaContact | None:
    body = {"fullName": full_name, "companies": [{"name": company_name, "isCurrent": True}]}
    return _enrich_one(body, cache_key=f"name:{full_name.lower()}@{company_name.lower()}",
                       api_key=api_key, cache_path=cache_path)


def _enrich_one(
    contact_body: dict[str, Any],
    *,
    cache_key: str,
    api_key: str | None,
    cache_path: Path | None,
) -> LushaContact | None:
    api_key = api_key or os.environ.get("LUSHA_API_KEY")
    if not api_key:
        raise LushaError("LUSHA_API_KEY saknas i miljön")
    cache_path = cache_path or DEFAULT_CACHE

    with _open_cache(cache_path) as conn, _client(api_key) as client:
        cached = _cache_get(conn, cache_key)
        if cached is not None:
            if cached.get("data") is None:
                return None
            return LushaContact(**cached["data"])

        contact_body = {"contactId": uuid.uuid4().hex[:12], **contact_body}
        try:
            resp = _call_person_endpoint(client, [contact_body])
        except LushaError as e:
            log.warning("Lusha enrichment failed for %s: %s", cache_key, e)
            return None

        contacts = resp.get("contacts") or {}
        # contacts is a dict keyed on our contactId — just one entry
        entry = next(iter(contacts.values()), {}) if isinstance(contacts, dict) else {}
        data = entry.get("data") if isinstance(entry, dict) else None
        error = entry.get("error") if isinstance(entry, dict) else None

        if not data:
            _cache_set(conn, cache_key, {"data": None, "error": str(error)})
            log.info("Lusha: no data for %s (error: %s)", cache_key, error)
            return None

        contact = _parse_contact_data(data)
        _cache_set(conn, cache_key, {"data": contact.model_dump(), "error": None})
        return contact


def enrich_batch_by_emails(
    emails: Iterable[str],
    *,
    api_key: str | None = None,
    cache_path: Path | None = None,
) -> dict[str, LushaContact | None]:
    """Batch-enrichment med upp till 100 email per call. Snabbare än en-och-en."""
    emails = list(dict.fromkeys(emails))  # dedup, preserve order
    if not emails:
        return {}
    api_key = api_key or os.environ.get("LUSHA_API_KEY")
    if not api_key:
        raise LushaError("LUSHA_API_KEY saknas i miljön")
    cache_path = cache_path or DEFAULT_CACHE

    out: dict[str, LushaContact | None] = {}
    to_fetch: list[tuple[str, str]] = []  # (cache_key, email)

    with _open_cache(cache_path) as conn:
        for email in emails:
            ck = f"email:{email.lower()}"
            cached = _cache_get(conn, ck)
            if cached is not None:
                out[email] = LushaContact(**cached["data"]) if cached.get("data") else None
            else:
                to_fetch.append((ck, email))

        if not to_fetch:
            return out

        with _client(api_key) as client:
            contacts_payload = [
                {"contactId": f"e{idx}", "email": email}
                for idx, (_, email) in enumerate(to_fetch)
            ]
            try:
                resp = _call_person_endpoint(client, contacts_payload)
            except LushaError as e:
                log.warning("Lusha batch failed: %s", e)
                for ck, email in to_fetch:
                    out[email] = None
                return out

            entries = resp.get("contacts") or {}
            for idx, (ck, email) in enumerate(to_fetch):
                entry_key = f"e{idx}"
                entry = entries.get(entry_key, {}) if isinstance(entries, dict) else {}
                data = entry.get("data") if isinstance(entry, dict) else None
                if data:
                    contact = _parse_contact_data(data)
                    _cache_set(conn, ck, {"data": contact.model_dump(), "error": None})
                    out[email] = contact
                else:
                    _cache_set(conn, ck, {"data": None, "error": str(entry.get("error"))})
                    out[email] = None

    return out


# --- CLI quick-probe ------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) > 2 and sys.argv[1] == "email":
        c = enrich_by_email(sys.argv[2])
    elif len(sys.argv) > 2:
        c = enrich_by_name_and_company(sys.argv[1], sys.argv[2])
    else:
        c = enrich_by_name_and_company("Kjetel Digre", "Aker Solutions")

    if c is None:
        print("  no data")
    else:
        print(f"  {c.full_name or '?'} / {c.title or '?'}")
        print(f"    seniority: {c.seniority}, departments: {c.departments}")
        print(f"    email: {c.primary_email} (alla: {[e.email for e in c.emails]})")
        print(f"    mobile: {c.primary_mobile} (alla: {[(p.number, p.phone_type) for p in c.phones]})")
        print(f"    linkedin: {c.linkedin_url}")
        print(f"    country: {c.country}, credit_charged: {c.credit_charged}")
