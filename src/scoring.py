"""Relevance scoring.

Design notes
------------
* Track is decided by the *job title*, not by which query found the job.
  A query for "software engineer" routinely returns QA roles and vice versa;
  trusting the query would put things in the wrong section of the report.
* Anything matching neither track's title patterns is dropped. This is the
  single most effective quality filter — aggregators return a lot of
  "Business Analyst" noise for technical queries.
* Experience is a hard gate, not a penalty. A role wanting 8 years is not a
  weak match, it's a wrong match, and letting it through with a low score
  just means it crowds out real matches on a slow day.
* Internship time is credited at a discount, not ignored. Employers vary from
  "counts fully" to "doesn't count at all"; the discount factor in config is
  the dial between those readings. Roles that *explicitly* welcome freshers
  get a large bonus, because those are the ones where the question of whether
  an internship counts never gets asked.
"""

from __future__ import annotations

import math
import re
from datetime import date
from typing import Optional

from sources.base import Job

# "3+ years", "3-5 years", "4 to 6 yrs", "minimum 2 years"
_YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:\+|-|–|to)?\s*(?:\d{1,2})?\s*\+?\s*(?:years?|yrs?)\b",
    re.IGNORECASE,
)

SKILL_CEILING = 70.0    # asymptotic max contribution from skill overlap
SKILL_HALFLIFE = 45.0   # raw weight at which ~63% of the ceiling is reached

# How far past your effective experience a stated requirement can sit before
# it counts as a stretch rather than a fit.
STRETCH_TOLERANCE = 1.5

# Reqs that explicitly welcome people without full-time history. These are the
# highest-conversion postings for someone with internship-only experience.
_FRESHER_SIGNALS = (
    "fresher", "freshers", "entry level", "entry-level", "junior",
    "new grad", "new graduate", "recent graduate", "recent graduates",
    "graduate engineer trainee", "graduate trainee", "graduate programme",
    "graduate program", "campus hire", "campus recruitment", "trainee",
    "0-1 year", "0-2 year", "0 to 2 year",
    "no prior experience", "no experience required",
    "2025 batch", "2026 batch", "final year", "final-year",
    "early career", "early-career", "internship or equivalent",
    "internships count", "including internships",
)

_VISA_KEYWORDS = (
    "visa sponsorship", "sponsorship available", "will sponsor",
    "relocation assistance", "relocation support", "work permit",
    "h-1b", "h1b", "skilled worker visa", "tier 2 sponsorship",
    "blue card", "eu blue card", "sponsor a visa",
)


def _word_present(needle: str, haystack: str) -> bool:
    """Whole-token match so 'go' doesn't fire inside 'going' or 'Django'."""
    if not needle:
        return False
    if " " in needle or "/" in needle or "." in needle or "-" in needle:
        return needle in haystack
    return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])",
                     haystack) is not None


def min_years_required(text: str) -> Optional[int]:
    """Lowest year-count mentioned — approximates the stated minimum."""
    found = [int(m.group(1)) for m in _YEARS_RE.finditer(text)]
    found = [y for y in found if 0 <= y <= 30]
    return min(found) if found else None


def classify_track(title: str, tracks: dict) -> Optional[str]:
    """Return the track whose title patterns match best, or None."""
    t = title.lower()
    best, best_hits = None, 0
    for key, cfg in tracks.items():
        hits = sum(1 for pat in cfg.get("title_must_match_any", [])
                   if _word_present(pat, t))
        if hits > best_hits:
            best, best_hits = key, hits
    # QA wins ties: a "Software Test Engineer" belongs in Track A.
    if best_hits == 0:
        return None
    if "qa" in tracks:
        qa_hits = sum(1 for pat in tracks["qa"].get("title_must_match_any", [])
                      if _word_present(pat, t))
        if qa_hits == best_hits and qa_hits > 0:
            return "qa"
    return best


class Scorer:
    def __init__(self, config: dict):
        self.cfg = config
        self.skills: dict[str, float] = config["skills"]
        self.tracks: dict = config["tracks"]
        self.bonuses: dict = config["scoring"]["bonuses"]
        self.penalties: dict = config["scoring"]["penalties"]
        self.max_years = config["candidate"]["max_years_required"]

        exp_since = date.fromisoformat(config["candidate"]["experience_since"])
        self.raw_years = (date.today() - exp_since).days / 365.25

        # Internship time gets credited at a discount, because that is how it
        # is actually read by an HR screen. `raw_years` is kept around so the
        # report can say "you have 1.6 years, they'll credit 0.8".
        cand = config["candidate"]
        self.discount = (
            float(cand.get("internship_discount", 1.0))
            if cand.get("experience_is_internship") else 1.0
        )
        self.years_experience = self.raw_years * self.discount

    # ------------------------------------------------------------ eligibility

    def classify_eligibility(self, hay: str, req: Optional[int]) -> str:
        """How likely you are to clear the experience screen on paper."""
        if any(sig in hay for sig in _FRESHER_SIGNALS):
            return "fresher"
        if req is None:
            return "unclear"
        if req <= self.years_experience + STRETCH_TOLERANCE:
            return "fits"
        return "stretch"

    # ------------------------------------------------------------------ score

    def score(self, job: Job) -> Optional[Job]:
        """Score in place. Returns None if the job should be dropped."""
        track = classify_track(job.title, self.tracks)
        if track is None:
            return None
        job.track = track

        hay = job.haystack
        title_l = job.title.lower()
        reasons: list[str] = []

        # ---- hard gate: experience -------------------------------------
        req = min_years_required(hay)
        if req is not None and req > self.max_years:
            return None

        # ---- skill overlap ---------------------------------------------
        matched, skill_points = [], 0.0
        for skill, weight in self.skills.items():
            if _word_present(skill, hay):
                matched.append(skill)
                skill_points += float(weight)
        # Adzuna gives snippets, not full JDs — don't punish it for thin text.
        if job.source == "adzuna":
            skill_points *= 1.35
        # Smooth saturation rather than a hard clamp. A hard min(x, 60) made a
        # 16-skill match and a 10-skill match score identically, destroying the
        # ordering exactly where it matters most — at the top of the list.
        skill_points = SKILL_CEILING * (1.0 - math.exp(-skill_points / SKILL_HALFLIFE))
        job.matched_skills = matched
        if matched:
            reasons.append(f"{len(matched)} skill matches (+{skill_points:.0f})")

        total = skill_points

        # ---- title relevance -------------------------------------------
        title_bonus = 0.0
        for pat, pts in self.tracks[track].get("title_bonus", {}).items():
            if _word_present(pat, title_l):
                title_bonus = max(title_bonus, float(pts))
        if title_bonus:
            total += title_bonus
            reasons.append(f"title match (+{title_bonus:.0f})")

        # ---- freshness --------------------------------------------------
        d = job.days_old
        if d is not None:
            if d <= 1:
                total += self.bonuses["fresh_today"]
                reasons.append(f"posted today (+{self.bonuses['fresh_today']})")
            elif d <= 3:
                total += self.bonuses["fresh_3_days"]
                reasons.append(f"posted {d}d ago (+{self.bonuses['fresh_3_days']})")

        # ---- visa / remote / salary -------------------------------------
        if any(k in hay for k in _VISA_KEYWORDS):
            total += self.bonuses["visa_sponsorship"]
            reasons.append(f"sponsorship mentioned (+{self.bonuses['visa_sponsorship']})")
        if job.is_remote:
            total += self.bonuses["remote"]
            reasons.append(f"remote (+{self.bonuses['remote']})")
        if job.salary_min or job.salary_max:
            total += self.bonuses["salary_disclosed"]

        # ---- experience fit ---------------------------------------------
        job.years_required = req
        job.eligibility = self.classify_eligibility(hay, req)

        if job.eligibility == "fresher":
            bonus = self.bonuses["fresher_friendly"]
            total += bonus
            reasons.append(f"open to freshers/entry-level (+{bonus})")
        elif job.eligibility == "fits":
            bonus = self.bonuses["experience_fits"]
            total += bonus
            reasons.append(f"asks {req}y, within reach (+{bonus})")
        elif job.eligibility == "stretch":
            pen = self.cfg["scoring"].get("stretch_penalty", 12)
            total -= pen
            reasons.append(
                f"asks {req}y vs {self.years_experience:.1f}y credited (-{pen})"
            )
        else:
            reasons.append("no experience stated")

        # ---- penalties ---------------------------------------------------
        for pat, pts in self.penalties.items():
            if _word_present(pat.strip(), hay):
                total -= float(pts)
                reasons.append(f"'{pat.strip()}' (-{pts})")

        job.score = round(max(total, 0.0), 1)
        job.score_reasons = reasons
        return job

    # ------------------------------------------------------------------ batch

    def rank(self, jobs: list[Job]) -> dict[str, list[Job]]:
        """Score, filter, and split into per-track ranked lists."""
        floor = self.cfg["scoring"]["min_score_to_report"]
        cap = self.cfg["scoring"]["max_per_track"]

        by_track: dict[str, list[Job]] = {k: [] for k in self.tracks}
        for job in jobs:
            scored = self.score(job)
            if scored is None or scored.score < floor:
                continue
            by_track[scored.track].append(scored)

        for track in by_track:
            by_track[track].sort(
                key=lambda j: (-j.score, j.days_old if j.days_old is not None else 99)
            )
            by_track[track] = by_track[track][:cap]
        return by_track
