"""Normalized job record + source interface.

Every source adapter converts its provider's payload into a `Job`, so
scoring, dedup and reporting never need to know where a posting came from.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, date
from typing import Optional


_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]")

# Noise words stripped before building the dedup key, so that
# "Senior SDET (Remote) - Bangalore" and "SDET, Bangalore" collapse.
_TITLE_NOISE = {
    "remote", "hybrid", "onsite", "on", "site", "full", "time", "fulltime",
    "contract", "permanent", "urgent", "hiring", "immediate", "joiner",
    "opening", "openings", "job", "jobs", "role", "position", "w", "f",
    "years", "year", "yrs", "exp", "experience", "india", "pvt", "ltd",
    "limited", "inc", "llc", "technologies", "technology", "solutions",
    "services", "labs", "systems", "software", "the", "a", "an", "and",
}


def _norm_tokens(text: str) -> list[str]:
    text = _NON_ALNUM.sub(" ", (text or "").lower())
    return [t for t in _WS.split(text) if t and t not in _TITLE_NOISE]


@dataclass
class Job:
    source: str
    external_id: str
    title: str
    company: str
    location: str
    url: str
    description: str = ""
    posted_at: Optional[date] = None
    is_remote: bool = False
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: Optional[str] = None
    publisher: Optional[str] = None

    # Populated downstream
    track: str = ""
    score: float = 0.0
    eligibility: str = "unclear"   # fresher | fits | unclear | stretch
    years_required: Optional[int] = None
    score_reasons: list[str] = field(default_factory=list)
    matched_skills: list[str] = field(default_factory=list)

    # ---------------------------------------------------------------- helpers

    @property
    def fingerprint(self) -> str:
        """Stable identity across sources.

        Deliberately excludes the URL and external_id: the same role surfaces
        on LinkedIn, Indeed and the company's own careers page with three
        different IDs, and seeing it three times is the main failure mode of
        a naive aggregator.
        """
        title = " ".join(sorted(set(_norm_tokens(self.title))))
        company = " ".join(sorted(set(_norm_tokens(self.company))))
        raw = f"{company}|{title}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @property
    def days_old(self) -> Optional[int]:
        if self.posted_at is None:
            return None
        return (datetime.now(timezone.utc).date() - self.posted_at).days

    @property
    def haystack(self) -> str:
        """Lowercased title + description, for keyword matching."""
        return f"{self.title}\n{self.description}".lower()

    @property
    def salary_display(self) -> str:
        if self.salary_min is None and self.salary_max is None:
            return "—"
        cur = self.salary_currency or ""

        def fmt(v):
            if v is None:
                return "?"
            if v >= 100_000:
                return f"{v / 100_000:.1f}L" if cur == "INR" else f"{v / 1000:.0f}k"
            if v >= 1000:
                return f"{v / 1000:.0f}k"
            return f"{v:.0f}"

        if self.salary_min and self.salary_max:
            return f"{cur} {fmt(self.salary_min)}–{fmt(self.salary_max)}".strip()
        return f"{cur} {fmt(self.salary_min or self.salary_max)}+".strip()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["posted_at"] = self.posted_at.isoformat() if self.posted_at else None
        d["fingerprint"] = self.fingerprint
        d["days_old"] = self.days_old
        return d


class Source:
    """Base class for a job source adapter."""

    name = "base"

    def __init__(self, creds: dict, timeout: int = 30):
        self.creds = creds
        self.timeout = timeout

    def available(self) -> bool:
        raise NotImplementedError

    def search(self, spec: dict, max_days_old: int) -> list[Job]:
        """`spec` is one entry from config.yaml's `queries` list."""
        raise NotImplementedError
