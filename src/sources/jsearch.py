"""JSearch adapter — Google for Jobs aggregate (LinkedIn, Indeed, Glassdoor,
ZipRecruiter, company career pages).

Docs: https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch
Auth: RapidAPI key in X-RapidAPI-Key.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from .base import Job, Source

log = logging.getLogger(__name__)

ENDPOINT = "https://jsearch.p.rapidapi.com/search"
HOST = "jsearch.p.rapidapi.com"


def _date_posted_param(max_days_old: int) -> str:
    """JSearch only accepts these buckets, not an arbitrary N."""
    if max_days_old <= 1:
        return "today"
    if max_days_old <= 3:
        return "3days"
    if max_days_old <= 7:
        return "week"
    return "month"


class JSearchSource(Source):
    name = "jsearch"

    def available(self) -> bool:
        return bool(self.creds.get("rapidapi_key"))

    def search(self, spec: dict, max_days_old: int) -> list[Job]:
        params = {
            "query": spec["query"],
            "page": "1",
            "num_pages": str(spec.get("num_pages", 1)),
            "date_posted": _date_posted_param(max_days_old),
        }
        if spec.get("country"):
            params["country"] = spec["country"]
        if spec.get("remote_only"):
            params["work_from_home"] = "true"
        if spec.get("employment_types"):
            params["employment_types"] = spec["employment_types"]

        headers = {
            "X-RapidAPI-Key": self.creds["rapidapi_key"],
            "X-RapidAPI-Host": HOST,
        }

        try:
            r = requests.get(ENDPOINT, params=params, headers=headers,
                             timeout=self.timeout)
        except requests.RequestException as e:
            log.warning("jsearch request failed for %r: %s", spec["query"], e)
            return []

        if r.status_code == 429:
            log.warning("jsearch rate limited — free tier quota likely exhausted")
            return []
        if r.status_code != 200:
            log.warning("jsearch HTTP %s for %r: %s", r.status_code,
                        spec["query"], r.text[:200])
            return []

        payload = r.json()
        return [j for j in (self._parse(d) for d in payload.get("data") or []) if j]

    # ------------------------------------------------------------------ parse

    @staticmethod
    def _parse(d: dict) -> Job | None:
        title = (d.get("job_title") or "").strip()
        url = d.get("job_apply_link") or d.get("job_google_link") or ""
        if not title or not url:
            return None

        posted = None
        ts = d.get("job_posted_at_timestamp")
        if ts:
            try:
                posted = datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
            except (ValueError, TypeError, OSError):
                posted = None
        if posted is None and d.get("job_posted_at_datetime_utc"):
            try:
                posted = datetime.fromisoformat(
                    d["job_posted_at_datetime_utc"].replace("Z", "+00:00")
                ).date()
            except ValueError:
                posted = None

        loc = ", ".join(
            p for p in (d.get("job_city"), d.get("job_state"), d.get("job_country"))
            if p
        ) or ("Remote" if d.get("job_is_remote") else "—")

        return Job(
            source="jsearch",
            external_id=str(d.get("job_id") or url),
            title=title,
            company=(d.get("employer_name") or "Unknown").strip(),
            location=loc,
            url=url,
            description=d.get("job_description") or "",
            posted_at=posted,
            is_remote=bool(d.get("job_is_remote")),
            salary_min=d.get("job_min_salary"),
            salary_max=d.get("job_max_salary"),
            salary_currency=d.get("job_salary_currency"),
            publisher=d.get("job_publisher"),
        )
