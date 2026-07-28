"""Entry point for the daily job agent.

  python src/main.py --config config.yaml
  python src/main.py --dry-run            # fixtures, no API calls, no email
  python src/main.py --no-email           # real fetch, PDF only

Credentials come from the environment, never the config file:
  RAPIDAPI_KEY, ADZUNA_APP_ID, ADZUNA_APP_KEY, SMTP_USERNAME, SMTP_PASSWORD
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from dedup import SeenStore, collapse                      # noqa: E402
from notify import send_email                              # noqa: E402
from report import build_pdf                               # noqa: E402
from scoring import Scorer                                 # noqa: E402
from sources.adzuna import AdzunaSource                    # noqa: E402
from sources.base import Job                               # noqa: E402
from sources.jsearch import JSearchSource                  # noqa: E402

log = logging.getLogger("job-agent")

ROOT = Path(__file__).resolve().parent.parent


# ─────────────────────────────────────────────────────────── query selection

def select_queries(cfg: dict, today: date) -> list[dict]:
    """Daily queries always run; rotating ones run on their slot.

    Rotation keeps the agent inside free-tier API quotas while still covering
    the wider searches (international, sponsorship) every few days.
    """
    budget = cfg["query_budget"]
    mod = budget.get("rotate_mod", 2)
    slot = today.toordinal() % mod

    chosen = []
    for q in cfg["queries"]:
        cadence = q.get("cadence", "daily")
        if cadence == "daily":
            chosen.append(q)
        elif cadence == "rotate" and q.get("rotate_slot", 0) == slot:
            chosen.append(q)

    cap = budget.get("max_requests_per_run", 12)
    if len(chosen) > cap:
        log.warning("trimming %d queries to budget of %d", len(chosen), cap)
        chosen = chosen[:cap]
    return chosen


# ─────────────────────────────────────────────────────────────────── fetching

def build_sources(creds: dict) -> dict:
    sources = {}
    for cls in (JSearchSource, AdzunaSource):
        inst = cls(creds)
        if inst.available():
            sources[inst.name] = inst
        else:
            log.warning("source %r unavailable — credentials missing, skipping",
                        inst.name)
    return sources


def fetch_all(specs: list[dict], sources: dict, max_days_old: int) -> tuple[list[Job], int]:
    jobs: list[Job] = []
    used = 0
    for spec in specs:
        src = sources.get(spec["source"])
        if src is None:
            continue
        log.info("query [%s/%s] %r", spec["source"], spec["track"], spec["query"])
        found = src.search(spec, max_days_old)
        used += 1
        log.info("  -> %d results", len(found))
        jobs.extend(found)
    return jobs, used


def load_fixtures(path: Path) -> list[Job]:
    """Offline mode: replay a saved payload so the pipeline can be tested
    without burning API quota. Invaluable when tuning scoring weights."""
    raw = json.loads(path.read_text())
    jobs = []
    for d in raw:
        posted = d.pop("posted_at", None)
        job = Job(**d)
        if posted:
            job.posted_at = date.fromisoformat(posted)
        jobs.append(job)
    return jobs


# ─────────────────────────────────────────────────────────────────────── main

def main() -> int:
    ap = argparse.ArgumentParser(description="Daily job matching agent")
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--dry-run", action="store_true",
                    help="use fixtures, skip email and state write")
    ap.add_argument("--no-email", action="store_true")
    ap.add_argument("--out-dir", default=str(ROOT / "out"))
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )

    cfg = yaml.safe_load(Path(args.config).read_text())
    max_days_old = cfg["freshness"]["max_days_old"]
    today = date.today()

    creds = {
        "rapidapi_key": os.environ.get("RAPIDAPI_KEY", ""),
        "adzuna_app_id": os.environ.get("ADZUNA_APP_ID", ""),
        "adzuna_app_key": os.environ.get("ADZUNA_APP_KEY", ""),
    }

    # ---- fetch -----------------------------------------------------------
    if args.dry_run:
        fixture = ROOT / "fixtures" / "sample_jobs.json"
        raw_jobs = load_fixtures(fixture)
        requests_used, queries_run = 0, 0
        log.info("DRY RUN — loaded %d fixture postings from %s",
                 len(raw_jobs), fixture.name)
    else:
        sources = build_sources(creds)
        if not sources:
            log.error("no usable sources. Set RAPIDAPI_KEY and/or ADZUNA_APP_ID"
                      " + ADZUNA_APP_KEY.")
            return 2
        specs = select_queries(cfg, today)
        queries_run = len(specs)
        raw_jobs, requests_used = fetch_all(specs, sources, max_days_old)

    log.info("fetched %d raw postings", len(raw_jobs))

    # ---- freshness guard (defence in depth; APIs sometimes ignore filters)
    fresh = [j for j in raw_jobs
             if j.days_old is None or j.days_old <= max_days_old]
    if len(fresh) != len(raw_jobs):
        log.info("dropped %d postings older than %d days",
                 len(raw_jobs) - len(fresh), max_days_old)

    # ---- dedup -----------------------------------------------------------
    unique = collapse(fresh)
    store = SeenStore(ROOT / "state" / "seen.json",
                      cfg["freshness"]["prune_state_after_days"])
    new_jobs, already_seen = store.filter_new(unique)
    log.info("%d unique, %d new, %d previously reported",
             len(unique), len(new_jobs), len(already_seen))

    # ---- score -----------------------------------------------------------
    scorer = Scorer(cfg)
    by_track = scorer.rank(new_jobs)
    reported = [j for jobs in by_track.values() for j in jobs]
    log.info("reporting %d matches: %s", len(reported),
             {k: len(v) for k, v in by_track.items()})
    elig = Counter(j.eligibility for j in reported)
    log.info("experience: %.1fy raw, %.1fy credited | eligibility: %s",
             scorer.raw_years, scorer.years_experience, dict(elig))

    track_labels = {k: v["label"] for k, v in cfg["tracks"].items()}
    run_meta = {
        "new_count": len(reported),
        "fetched": len(raw_jobs),
        "queries_run": queries_run,
        "max_days_old": max_days_old,
        "max_years_required": cfg["candidate"]["max_years_required"],
        "raw_years": scorer.raw_years,
        "credited_years": scorer.years_experience,
    }

    # ---- PDF -------------------------------------------------------------
    pdf_path = None
    if cfg["delivery"]["pdf"]["enabled"]:
        fname = cfg["delivery"]["pdf"]["filename_template"].format(
            date=today.isoformat())
        pdf_path = build_pdf(Path(args.out_dir) / fname, by_track,
                             track_labels, run_meta)
        log.info("PDF written: %s", pdf_path)

    # ---- email -----------------------------------------------------------
    email_cfg = cfg["delivery"]["email"]
    if email_cfg["enabled"] and not args.no_email and not args.dry_run:
        user = os.environ.get("SMTP_USERNAME", "")
        pw = os.environ.get("SMTP_PASSWORD", "")
        if not (user and pw):
            log.error("SMTP_USERNAME / SMTP_PASSWORD not set — skipping email")
        elif not reported:
            log.info("no new matches — skipping email (no empty-inbox noise)")
        else:
            send_email(
                smtp_host=email_cfg["smtp_host"],
                smtp_port=email_cfg["smtp_port"],
                username=user,
                password=pw,
                to_addr=cfg["candidate"]["email_to"],
                subject=email_cfg["subject_template"].format(
                    n=len(reported), date=today.strftime("%d %b")),
                by_track=by_track,
                track_labels=track_labels,
                inline_top_n=email_cfg["inline_top_n"],
                run_meta=run_meta,
                attachment=pdf_path,
            )

    # ---- persist ---------------------------------------------------------
    if not args.dry_run:
        store.record(reported)
        store.prune()
        store.log_run(len(reported), len(unique), requests_used)
        store.save()

    # A machine-readable summary for the GH Actions job summary step.
    print(json.dumps({
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "fetched": len(raw_jobs),
        "unique": len(unique),
        "new": len(new_jobs),
        "reported": len(reported),
        "per_track": {k: len(v) for k, v in by_track.items()},
        "api_requests": requests_used,
        "credited_years": round(scorer.years_experience, 2),
        "eligibility": dict(elig),
        "pdf": str(pdf_path) if pdf_path else None,
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
