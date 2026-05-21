"""JobTech Dev — Swedish public job-ads API.

Endpoint: https://jobsearch.api.jobtechdev.se/search
Docs: https://jobtechdev.se/sv/komponenter/jobsearch

Free, no key. We pull recent ads filtered to CNC/manufacturing-relevant
occupations (occupation-name / employer-name queries) and emit normalised
events. The signals agent later maps employer → Account and classifies
the ad text into HIRE_* / PLANT_EXPANSION / etc.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

JOBTECH_SEARCH_URL = "https://jobsearch.api.jobtechdev.se/search"

# Swedish CNC / manufacturing search terms. Tuned for Ravema's portfolio
# (Mazak full-line, PAMA, automation). We avoid pure "operatör" — too noisy —
# and bias toward roles signalling capacity expansion or process change.
DEFAULT_QUERY_TERMS: tuple[str, ...] = (
    "CNC-operatör",
    "CNC-tekniker",
    "produktionschef",
    "produktionsledare",
    "produktionstekniker",
    "beredare",
    "fleroperationssvarvare",
    "fräsare",
    "automationstekniker",
    "underhållstekniker mekanik",
)


@dataclass
class JobAd:
    """Raw job-ad event from JobTech. No signal classification yet."""

    ad_id: str
    headline: str
    description: str
    employer_name: str | None
    employer_orgnr: str | None
    workplace_city: str | None
    publication_date: datetime | None
    application_deadline: datetime | None
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


def _ad_url(ad_id: str) -> str:
    return f"https://arbetsformedlingen.se/platsbanken/annonser/{ad_id}"


def _to_jobad(item: dict[str, Any]) -> JobAd:
    employer = item.get("employer") or {}
    workplace = item.get("workplace_address") or {}
    occ = item.get("occupation") or {}
    return JobAd(
        ad_id=str(item.get("id", "")),
        headline=item.get("headline", "") or "",
        description=(item.get("description") or {}).get("text", "") or "",
        employer_name=employer.get("name"),
        employer_orgnr=employer.get("organization_number"),
        workplace_city=workplace.get("municipality"),
        publication_date=_parse_dt(item.get("publication_date")),
        application_deadline=_parse_dt(item.get("application_deadline")),
        occupation=occ.get("label"),
        source_url=_ad_url(str(item.get("id", ""))),
        raw=item,
    )


def fetch_jobs(
    *,
    queries: tuple[str, ...] = DEFAULT_QUERY_TERMS,
    since_days: int = 30,
    limit_per_query: int = 50,
    municipalities: tuple[str, ...] | None = None,
    client: httpx.Client | None = None,
) -> list[JobAd]:
    """Fetch recent SE job ads matching any of the given terms.

    Caller owns the httpx client lifecycle in production code so we can reuse
    connection pooling across multiple integrations. In tests, pass a mocked
    transport via httpx.MockTransport.
    """
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=20.0, headers={"accept": "application/json"})

    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).date().isoformat()

    try:
        ads: list[JobAd] = []
        seen: set[str] = set()
        for q in queries:
            params: dict[str, Any] = {
                "q": q,
                "limit": limit_per_query,
                "published-after": since,
                "sort": "pubdate-desc",
            }
            if municipalities:
                params["municipality"] = list(municipalities)
            r = client.get(JOBTECH_SEARCH_URL, params=params)
            r.raise_for_status()
            payload = r.json()
            for item in payload.get("hits", []):
                ad_id = str(item.get("id", ""))
                if not ad_id or ad_id in seen:
                    continue
                seen.add(ad_id)
                ads.append(_to_jobad(item))
        return ads
    finally:
        if owns_client:
            client.close()
