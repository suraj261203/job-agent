"""Deduplication and run state.

Two layers:
1. Within a single run — the same role comes back from several queries and
   several publishers. Collapse by fingerprint, keeping the richest record.
2. Across runs — a posting stays live for weeks. Without persisted state the
   "daily" report is the same list every morning, which is how these things
   get ignored after three days.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

from sources.base import Job

log = logging.getLogger(__name__)

# Prefer sources with full job descriptions when collapsing duplicates.
_SOURCE_RANK = {"jsearch": 2, "adzuna": 1}


def collapse(jobs: list[Job]) -> list[Job]:
    """Within-run dedup. Keeps the record with the best source and longest JD."""
    best: dict[str, Job] = {}
    for job in jobs:
        fp = job.fingerprint
        incumbent = best.get(fp)
        if incumbent is None:
            best[fp] = job
            continue
        challenger_key = (_SOURCE_RANK.get(job.source, 0), len(job.description))
        incumbent_key = (_SOURCE_RANK.get(incumbent.source, 0), len(incumbent.description))
        if challenger_key > incumbent_key:
            best[fp] = job
    dropped = len(jobs) - len(best)
    if dropped:
        log.info("collapsed %d duplicate postings", dropped)
    return list(best.values())


class SeenStore:
    def __init__(self, path: str | Path, prune_after_days: int = 60):
        self.path = Path(path)
        self.prune_after_days = prune_after_days
        self.data: dict = {"jobs": {}, "runs": []}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            log.info("no state file at %s — first run", self.path)
            return
        try:
            self.data = json.loads(self.path.read_text())
            self.data.setdefault("jobs", {})
            self.data.setdefault("runs", [])
        except (json.JSONDecodeError, OSError) as e:
            log.warning("state file unreadable (%s) — starting fresh", e)
            self.data = {"jobs": {}, "runs": []}

    # ---------------------------------------------------------------- queries

    def is_new(self, job: Job) -> bool:
        return job.fingerprint not in self.data["jobs"]

    def filter_new(self, jobs: list[Job]) -> tuple[list[Job], list[Job]]:
        """Returns (new, previously_seen)."""
        new, old = [], []
        for job in jobs:
            (new if self.is_new(job) else old).append(job)
        return new, old

    # ---------------------------------------------------------------- mutation

    def record(self, jobs: list[Job]) -> None:
        today = date.today().isoformat()
        for job in jobs:
            self.data["jobs"].setdefault(job.fingerprint, {
                "first_seen": today,
                "title": job.title,
                "company": job.company,
                "url": job.url,
                "track": job.track,
                "score": job.score,
            })

    def prune(self) -> int:
        cutoff = date.today() - timedelta(days=self.prune_after_days)
        stale = [
            fp for fp, meta in self.data["jobs"].items()
            if _safe_date(meta.get("first_seen")) and _safe_date(meta["first_seen"]) < cutoff
        ]
        for fp in stale:
            del self.data["jobs"][fp]
        if stale:
            log.info("pruned %d entries older than %d days", len(stale),
                     self.prune_after_days)
        return len(stale)

    def log_run(self, new_count: int, total_seen: int, requests_used: int) -> None:
        self.data["runs"].append({
            "date": date.today().isoformat(),
            "new": new_count,
            "candidates_after_dedup": total_seen,
            "api_requests": requests_used,
        })
        self.data["runs"] = self.data["runs"][-90:]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True))
        log.info("state written: %d tracked postings", len(self.data["jobs"]))


def _safe_date(s):
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        return None
