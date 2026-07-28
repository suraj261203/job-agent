# Daily Job Agent

Fetches fresh job postings from aggregator APIs, scores them against your
resume, drops everything you've already been shown, and emails you a ranked
PDF every morning at 09:00 IST via GitHub Actions.

Two tracks are scored and ranked **independently**, so QA/SDET roles never
crowd out SDE roles or vice versa:

- **Track A** — QA / SDET / Test Automation
- **Track B** — SDE / Backend

Calibrated for **internship-only experience**: 18 months of internship is
credited at a discount, roles that explicitly welcome freshers are boosted,
and postings demanding full-time years you can't document are ranked down.
See [Experience calibration](#experience-calibration-read-this-one).

---

## Setup (about 20 minutes, most of it waiting for API signups)

### 1. Create the repo

```bash
git init job-agent && cd job-agent
# copy these files in
git add . && git commit -m "feat: daily job agent"
git remote add origin git@github.com:suraj261203/job-agent.git
git push -u origin main
```

A **private** repo is recommended — `state/seen.json` accumulates a record of
every role you've been shown.

### 2. Get API credentials

| Service | Where | Free tier | Required? |
|---|---|---|---|
| **JSearch** | [RapidAPI](https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch) → Subscribe → Basic | ~200 req/month | Yes — primary source |
| **Adzuna** | [developer.adzuna.com](https://developer.adzuna.com/) → register → create app | generous, daily-capped | Optional — secondary |

JSearch pulls from Google for Jobs, which indexes LinkedIn, Indeed,
Glassdoor, ZipRecruiter and company career pages. That's why there's no
LinkedIn or Naukri adapter here: LinkedIn's official API doesn't expose job
search, Naukri has no public API, and scraping either violates their terms
and breaks constantly. Going through the aggregate is the durable path.

### 3. Gmail app password

Google blocks plain SMTP logins. Enable 2FA, then create an app password at
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
and use that 16-character string as `SMTP_PASSWORD`.

### 4. Add repo secrets

`Settings → Secrets and variables → Actions → New repository secret`:

```
RAPIDAPI_KEY        your RapidAPI key
ADZUNA_APP_ID       (optional)
ADZUNA_APP_KEY      (optional)
SMTP_USERNAME       sripadisuraj26@gmail.com
SMTP_PASSWORD       the 16-char app password
```

### 5. Test before you burn quota

```bash
pip install -r requirements.txt
python src/main.py --dry-run          # fixtures, no API calls, no email
open out/job-report-*.pdf
```

Then a real run with no email:

```bash
export RAPIDAPI_KEY=...
python src/main.py --no-email -v
```

Then trigger the workflow manually from the Actions tab. Once you've seen one
good email, the cron takes over.

---

## API quota maths

The free JSearch tier is roughly 200 requests/month. The config runs **6
queries/day** (`cadence: daily`) plus **2-3 rotating** across a 3-day cycle,
so 8-9 requests/day.

- Daily cron: ~250/month — **overshoots**.
- Weekdays only (`cron: "30 3 * * 1-5"`): **~183/month — fits**.

The workflow ships with the daily cron, so change it to `1-5` unless you're
paying for a higher tier. Weekday-only also matches reality: very little gets
posted on Indian weekends.

`query_budget.max_requests_per_run` is a hard stop that trims the list if it
ever grows past the cap, and `rotate_mod: 3` is what keeps the wider searches
(new-grad programmes, sponsorship, remote) in rotation without daily cost.

---

## Tuning

Everything worth changing is in `config.yaml`; you shouldn't need to touch the
Python.

- **`skills`** — weights lifted from your resume. Raise the ones you want to
  be hired for, not just the ones you know. Skills you'd rather move away from
  should be lowered, not deleted, so they still count as partial signal.
- **`scoring.penalties`** — the highest-leverage knob. Every time you get a
  bad match, add its distinguishing phrase here. This is what stops the
  report degrading into noise over a few weeks.
- **`scoring.min_score_to_report`** — start at 25. If the report is too long,
  raise it; if you get empty mornings, lower it.
- **`candidate.max_years_required`** — a hard gate, not a penalty. Roles
  asking beyond this are dropped entirely. Currently 3.

After any change, re-run `--dry-run` and check the ordering still makes sense.

---

## Experience calibration (read this one)

Your 18 months at Carousell are internship, not full-time, and employers treat
that inconsistently — some count it fully, some discount it, some ignore it.
The agent splits the difference rather than guessing:

```yaml
candidate:
  experience_since: "2025-01-01"
  experience_is_internship: true
  internship_discount: 0.5      # <- the dial
  max_years_required: 3
```

At `0.5`, your 1.6 raw years are credited as **0.8 years** — the conservative
read an HR screen is likely to take. Every posting then gets one of four
badges:

| Badge | Meaning | Score effect |
|---|---|---|
| **FRESHER OK** | JD explicitly says fresher / entry-level / new grad / trainee / 0-2 years | **+16** |
| **IN RANGE** | Stated requirement is within credited years + 1.5 | +6 |
| **NOT STATED** | No year figure in the JD — very common, often fine | 0 |
| **ASKS Ny** | Wants more than your credit covers | −12 |
| *(dropped)* | Wants more than `max_years_required` | excluded |

**Tune the dial from real feedback, not from feelings.** If you're clearing
screens for 2-year roles, your internship is being counted — raise toward
`0.75`. If you're getting auto-rejected on experience, drop to `0.35` and the
report will lean harder on FRESHER OK postings.

Separately, `scoring.penalties` now includes internship-hostile language —
`relieving letter`, `full-time experience`, `post qualification experience`,
`internship experience will not`. A req demanding a relieving letter you
can't produce is a wasted application, and those get ranked to the floor.

**Why STRETCH roles aren't dropped.** Year requirements are the softest thing
in any JD. A 3-year ask where you hit nine of ten listed skills is worth a
referral attempt; dropping it silently would hide your best long shots. They
just don't get to outrank roles you're cleanly eligible for.

### Auditing why something scored what it did

```bash
python -c "
import sys, yaml; sys.path.insert(0, 'src')
from scoring import Scorer
from main import load_fixtures
from pathlib import Path
cfg = yaml.safe_load(open('config.yaml'))
for j in load_fixtures(Path('fixtures/sample_jobs.json')):
    r = Scorer(cfg).score(j)
    print(f'{r.score if r else \"DROP\":>8}  {j.title}')
    if r: print('         ', '; '.join(r.score_reasons))
"
```

Every score carries its own reason list — there's no black box to argue with.

---

## How it works

```
config.yaml ──> select_queries()      cadence + rotation, budget-capped
                     │
                     v
              sources/{jsearch,adzuna}   date_posted = week (your 7-day rule)
                     │
                     v
              freshness guard           belt-and-braces; APIs do ignore filters
                     │
                     v
              dedup.collapse()          same role via 3 publishers -> 1 entry
                     │
                     v
              SeenStore.filter_new()    suppress anything reported before
                     │
                     v
              Scorer.rank()             classify track, gate, score, sort
                     │
                     ├──> report.build_pdf()    tappable apply links
                     └──> notify.send_email()   HTML digest + PDF attached
                     │
                     v
              state/seen.json           committed back by the workflow
```

### Design decisions worth knowing about

**Track is decided by job title, not by the query that found it.** A query for
"software engineer" reliably returns QA roles and vice versa. Trusting the
query would file things under the wrong heading.

**Deduplication ignores IDs and URLs.** The fingerprint is a normalised
`company|title` hash with noise words stripped, because the same role appears
on LinkedIn, Indeed and the company's careers page with three different IDs.
Matching on ID would show you every role three times, which is the main
failure mode of naive aggregators.

**Experience is a hard gate.** A role wanting 8 years isn't a weak match, it's
a wrong match. Scoring it low and letting it through just means it crowds out
real matches on a quiet day.

**Empty runs send no email.** A daily notification that's often empty gets
filtered to a folder and then ignored. Silence means nothing new cleared the
bar; the Actions run summary is still there if you want to confirm it ran.

---

## Known limitations

- **Aggregator lag.** Google for Jobs indexes most boards within hours, but
  some postings surface a day late. A role posted Monday might reach you
  Tuesday.
- **No Naukri.** Significant gap for the Indian market specifically. No API,
  and scraping is against their terms. Worth checking manually.
- **`min_years_required` is regex on prose.** It reads the lowest year figure
  in the JD, which misfires on text like "founded 10 years ago". Rare, and it
  errs toward dropping rather than including.
- **Salary is usually null.** Most postings simply don't publish it.
- **Fingerprint collisions.** Two genuinely different roles with the same
  title at the same company collapse into one. Correct behaviour more often
  than not, but it does mean "SDE-1, Payments" and "SDE-1, Search" at one
  company may show as a single row.
