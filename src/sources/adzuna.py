"""Adzuna adapter — secondary source, free developer tier, covers India.

Docs: https://developer.adzuna.com/
Auth: app_id + app_key as query params (not a bearer token).
Note: descriptions come back as truncated snippets, so skill matching is
weaker here than on JSearch. Weighted accordingly in scoring.
"""

from __future__ import annotations

import logging
from datetime import datetime

import requests

from .base import Job, Source

log = logging.getLogger(__name__)

ENDPOINT = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"


class AdzunaSource(Source):
    name = "adzuna"

    def available(self) -> bool:
        return bool(self.creds.get("adzuna_app_id") and self.creds.get("adzuna_app_key"))

    def search(self, spec: dict, max_days_old: int) -> list[Job]:
        country = spec.get("country", "in")
        params = {
            "app_id": self.creds["adzuna_app_id"],
            "app_key": self.creds["adzuna_app_key"],
            "what": spec["query"],
            "max_days_old": max_days_old,
            "results_per_page": spec.get("results_per_page", 50),
            "sort_by": "date",
            "content-type": "application/json",
        }
        if spec.get("where"):
            params["where"] = spec["where"]

        try:
            r = requests.get(ENDPOINT.format(country=country), params=params,
                             timeout=self.timeout)
        except requests.RequestException as e:
            log.warning("adzuna request failed for %r: %s", spec["query"], e)
            return []

        if r.status_code != 200:
            log.warning("adzuna HTTP %s for %r: %s", r.status_code,
                        spec["query"], r.text[:200])
            return []

        payload = r.json()
        return [j for j in (self._parse(d) for d in payload.get("results") or []) if j]

    # ------------------------------------------------------------------ parse

    @staticmethod
    def _parse(d: dict) -> Job | None:
        title = (d.get("title") or "").strip()
        url = d.get("redirect_url") or ""
        if not title or not url:
            return None

        posted = None
        if d.get("created"):
            try:
                posted = datetime.fromisoformat(
                    d["created"].replace("Z", "+00:00")
                ).date()
            except ValueError:
                posted = None

        loc = ((d.get("location") or {}).get("display_name")) or "—"
        desc = d.get("description") or ""

        return Job(
            source="adzuna",
            external_id=str(d.get("id") or url),
            title=title,
            company=((d.get("company") or {}).get("display_name") or "Unknown").strip(),
            location=loc,
            url=url,
            description=desc,
            posted_at=posted,
            is_remote="remote" in f"{title} {loc} {desc}".lower(),
            salary_min=d.get("salary_min"),
            salary_max=d.get("salary_max"),
            salary_currency="INR" if d.get("salary_min") else None,
            publisher="Adzuna",
        )
