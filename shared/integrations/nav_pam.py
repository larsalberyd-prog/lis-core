"""NAV pam-stilling — Norwegian public job-ads feed.

Endpoint: https://arbeidsplassen.nav.no/public-feed/api/v1/ads
Docs: https://github.com/navikt/pam-public-feed-api

Free, no key. Same shape as jobtech.py: pull, normalise, hand to signals
agent. Norwegian CNC vocabulary differs from Swedish (dreier, fres, sponfri
bearbeiding, automasjonsingeniør) so we keep a separate term list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

NAV_FEED_URL = "https://arbeidsplassen.nav.no/public-feed/api/v1/ads"

DEFAULT_QUERY_TERMS: tuple[str, ...] = (
    "CNC",
    "CNC-operatør",
    "dreier",
    "maskineringsoperatør",
    "fres",
    "verktøymaker",
    "produksjonsleder",
    "automasjonsingeniør",
    "vedlikeholdstekniker mekanikk",
    "sponfri bearbeiding",
)


@dataclass
class NavJobAd:
    ad_id: str
    title: str
    description: str
    employer_name: str | None
    employer_orgnr: str | None
    workplace_city: str | None
    workplace_county: str | None
    published: datetime | None
    expires: datetime | None
    occupation: str | None
    source_url: str
    raw: dict[str, Any] = field(default_factory=dict)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _to_navad(item: dict[str, Any]) -> NavJobAd:
    employer = item.get("employer") or {}
    location = item.get("locationList") or item.get("locations") or []
    first_loc = location[0] if location else {}
    cats = item.get("categoryList") or item.get("categories") or []
    occ = cats[0].get("name") if cats else None
    return NavJobAd(
        ad_id=str(item.get("uuid", item.get("id", ""))),
        title=item.get("title", "") or "",
        description=item.get("description", "") or "",
        employer_name=employer.get("name"),
        employer_orgnr=employer.get("orgnr") or employer.get("organisationNumber"),
        workplace_city=first_loc.get("city") or first_loc.get("municipal"),
        workplace_county=first_loc.get("county"),
        published=_parse_dt(item.get("published") or item.get("publishedDate")),
        expires=_parse_dt(item.get("expires") or item.get("expirationDate")),
        occupation=occ,
        source_url=item.get("source_url")
        or f"https://arbeidsplassen.nav.no/stillinger/stilling/{item.get('uuid', '')}",
        raw=item,
    )


def fetch_jobs(
    *,
    queries: tuple[str, ...] = DEFAULT_QUERY_TERMS,
    since_days: int = 30,
    limit_per_query: int = 50,
    counties: tuple[str, ...] | None = None,
    client: httpx.Client | None = None,
) -> list[NavJobAd]:
    """Fetch recent NO job ads matching any of the given terms."""
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=20.0, headers={"accept": "application/json"})

    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()

    try:
        ads: list[NavJobAd] = []
        seen: set[str] = set()
        for q in queries:
            params: dict[str, Any] = {
                "q": q,
                "size": limit_per_query,
                "published": since,
            }
            if counties:
                params["counties"] = list(counties)
            r = client.get(NAV_FEED_URL, params=params)
            r.raise_for_status()
            payload = r.json()
            for item in payload.get("content", payload.get("ads", [])):
                ad_id = str(item.get("uuid", item.get("id", "")))
                if not ad_id or ad_id in seen:
                    continue
                seen.add(ad_id)
                ads.append(_to_navad(item))
        return ads
    finally:
        if owns_client:
            client.close()
