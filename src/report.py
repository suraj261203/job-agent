"""PDF report generation.

One page-flow document: summary header, then one section per track with a
ranked table. Job titles are live hyperlinks straight to the apply page —
the whole point is that you open the PDF on your phone and tap through.
"""

from __future__ import annotations

import html
from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
)

from sources.base import Job

INK = colors.HexColor("#1a1a1a")
MUTED = colors.HexColor("#6b6b6b")
RULE = colors.HexColor("#d8d8d8")
BAND = colors.HexColor("#f4f4f2")
LINK = colors.HexColor("#1a4f8a")
STRONG = colors.HexColor("#1e6f4a")
MID = colors.HexColor("#9a6b1a")


def _styles() -> dict:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "title", parent=base["Title"], fontName="Helvetica-Bold",
            fontSize=19, leading=23, textColor=INK, alignment=TA_LEFT,
            spaceAfter=2,
        ),
        "subtitle": ParagraphStyle(
            "subtitle", parent=base["Normal"], fontName="Helvetica",
            fontSize=9.5, leading=13, textColor=MUTED, spaceAfter=10,
        ),
        "section": ParagraphStyle(
            "section", parent=base["Heading2"], fontName="Helvetica-Bold",
            fontSize=12.5, leading=15, textColor=INK,
            spaceBefore=12, spaceAfter=6,
        ),
        "role": ParagraphStyle(
            "role", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=9.5, leading=12, textColor=INK,
        ),
        "cell": ParagraphStyle(
            "cell", parent=base["Normal"], fontName="Helvetica",
            fontSize=8.5, leading=11, textColor=INK,
        ),
        "score": ParagraphStyle(
            "score", parent=base["Normal"], fontName="Helvetica-Bold",
            fontSize=11, leading=13, textColor=INK,
        ),
        "meta": ParagraphStyle(
            "meta", parent=base["Normal"], fontName="Helvetica",
            fontSize=7.5, leading=10, textColor=MUTED,
        ),
        "empty": ParagraphStyle(
            "empty", parent=base["Normal"], fontName="Helvetica-Oblique",
            fontSize=9, leading=12, textColor=MUTED, spaceAfter=6,
        ),
    }


def _score_colour(score: float):
    if score >= 60:
        return STRONG
    if score >= 40:
        return MID
    return MUTED


_BADGE = {
    "fresher": ("FRESHER OK", STRONG),
    "fits": ("IN RANGE", LINK),
    "stretch": ("STRETCH", MID),
    "unclear": ("NOT STATED", MUTED),
}


def _badge(job: Job) -> tuple[str, object]:
    label, colour = _BADGE.get(job.eligibility, _BADGE["unclear"])
    if job.eligibility == "stretch" and job.years_required:
        label = f"ASKS {job.years_required}y"
    return label, colour


def _age_label(job: Job) -> str:
    d = job.days_old
    if d is None:
        return "date n/a"
    if d <= 0:
        return "today"
    if d == 1:
        return "1 day"
    return f"{d} days"


def _track_table(jobs: list[Job], st: dict) -> Table:
    rows = [[
        Paragraph("<b>Fit</b>", st["meta"]),
        Paragraph("<b>Role</b>", st["meta"]),
        Paragraph("<b>Company</b>", st["meta"]),
        Paragraph("<b>Location</b>", st["meta"]),
        Paragraph("<b>Posted</b>", st["meta"]),
    ]]
    style = [
        ("GRID", (0, 0), (-1, -1), 0, colors.white),
        ("LINEBELOW", (0, 0), (-1, 0), 0.7, INK),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
    ]

    r = 1
    for job in jobs:
        badge_label, badge_colour = _badge(job)
        score_p = Paragraph(
            f'<font color="{_score_colour(job.score).hexval()}" size="11">'
            f'<b>{job.score:.0f}</b></font><br/>'
            f'<font color="{badge_colour.hexval()}" size="5.5">'
            f'{badge_label}</font>',
            st["score"],
        )
        role_p = Paragraph(
            f'<link href="{html.escape(job.url, quote=True)}">'
            f'<font color="{LINK.hexval()}">{html.escape(job.title)}</font></link>',
            st["role"],
        )
        rows.append([
            score_p,
            role_p,
            Paragraph(html.escape(job.company), st["cell"]),
            Paragraph(html.escape(job.location), st["cell"]),
            Paragraph(_age_label(job), st["cell"]),
        ])
        style += [
            ("LINEBELOW", (0, r), (-1, r), 0.4, RULE),
        ]
        if r % 4 in (1, 2):
            style.append(("BACKGROUND", (0, r), (-1, r), BAND))
        r += 1

        # Detail row: matched skills + salary + source, spanned across.
        skills = ", ".join(job.matched_skills[:10]) or "no direct keyword hits"
        detail = (
            f"<b>Signals:</b> {html.escape(skills)}"
            f" &nbsp;&nbsp;|&nbsp;&nbsp; <b>Pay:</b> {html.escape(job.salary_display)}"
            f" &nbsp;&nbsp;|&nbsp;&nbsp; <b>Via:</b> "
            f"{html.escape(job.publisher or job.source)}"
        )
        rows.append([Paragraph(detail, st["meta"]), "", "", "", ""])
        style += [
            ("SPAN", (0, r), (-1, r)),
            ("LINEBELOW", (0, r), (-1, r), 0.4, RULE),
            ("TOPPADDING", (0, r), (-1, r), 0),
            ("BOTTOMPADDING", (0, r), (-1, r), 6),
            ("LEFTPADDING", (0, r), (-1, r), 8),
        ]
        if (r - 1) % 4 in (1, 2):
            style.append(("BACKGROUND", (0, r), (-1, r), BAND))
        r += 1

    table = Table(
        rows,
        colWidths=[19 * mm, 58 * mm, 39 * mm, 36 * mm, 18 * mm],
        repeatRows=1,
    )
    table.setStyle(TableStyle(style))
    return table


def build_pdf(
    out_path: str | Path,
    by_track: dict[str, list[Job]],
    track_labels: dict[str, str],
    run_meta: dict,
) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    st = _styles()

    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=14 * mm, rightMargin=14 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm,
        title=f"Job Report {date.today().isoformat()}",
        author="Daily Job Agent",
    )

    story = [
        Paragraph("Daily Job Report", st["title"]),
        Paragraph(
            f"{date.today().strftime('%A, %d %B %Y')} &nbsp;·&nbsp; "
            f"{run_meta['new_count']} new postings, "
            f"filtered from {run_meta['fetched']} fetched across "
            f"{run_meta['queries_run']} queries &nbsp;·&nbsp; "
            f"posted within {run_meta['max_days_old']} days",
            st["subtitle"],
        ),
    ]

    for track, jobs in by_track.items():
        label = track_labels.get(track, track)
        story.append(Paragraph(f"{label} &nbsp;<font color='{MUTED.hexval()}'>"
                               f"({len(jobs)})</font>", st["section"]))
        if not jobs:
            story.append(Paragraph(
                "Nothing new cleared the relevance threshold today.", st["empty"]))
            continue
        story.append(_track_table(jobs, st))

    story += [
        Spacer(1, 10 * mm),
        Paragraph(
            "Fit score combines resume skill overlap, title relevance, posting "
            "freshness, sponsorship signals and experience eligibility. "
            f"Your {run_meta['raw_years']:.1f} years of internship experience is "
            f"credited at {run_meta['credited_years']:.1f} years, the "
            "conservative reading an HR screen is likely to take. "
            "<b>FRESHER OK</b> means the posting explicitly welcomes "
            "entry-level candidates. <b>STRETCH</b> means it asks for more "
            "than that credit covers - still worth a shot with a referral. "
            f"Roles asking beyond {run_meta['max_years_required']} years, and "
            "postings already sent, are excluded.",
            st["meta"],
        ),
    ]

    doc.build(story)
    return out_path
