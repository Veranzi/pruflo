"""What a case report says, independent of the file format.

``write`` lays the report out as headings, paragraphs and label/value rows on
a ``Writer``; ``app.reports.pdf`` and ``app.reports.docx`` turn those into a
PDF or a Word document.

Report types:
- executive: one page of counts and the findings that still need a decision.
- summary: the case, evidence list and a line per finding.
- detailed: every chosen section in full, with quotes and review notes.
"""

from collections import Counter
from typing import Any, Protocol

from app.db.models import AuditEvent, Case, EvidenceFile, User

DISCLAIMER = (
    "Findings are produced by deterministic rules from values extracted from the evidence. "
    "They are not conclusions: each one needs human interpretation, and only decisions "
    "recorded by a named reviewer are final."
)


class Writer(Protocol):
    def heading(self, text: str, size: float = 13) -> None: ...
    def para(self, text: str, size: float = 10, bold: bool = False, color: int = 40) -> None: ...
    def row(self, label: str, value: Any) -> None: ...
    def gap(self, mm: float) -> None: ...


def _value(v: dict[str, Any] | None) -> str:
    if not v:
        return "-"
    return f"{v.get('value', '-')} {v.get('unit') or ''}".strip()


def footer_text(case: Case, generated: str) -> str:
    return f"PruFlo · {case.reference} · generated {generated}"


def write(
    w: Writer,
    case: Case,
    options: dict[str, Any],
    evidence: list[EvidenceFile],
    export: dict[str, Any],
    timeline: list[tuple[AuditEvent, User | None]],
    generated: str,
) -> None:
    """The report's content, the same whatever the output format."""
    report_type = options.get("type", "detailed")
    sections = set(options.get("sections") or ["evidence", "timeline", "findings"])

    w.para("PruFlo case report", size=9, color=110)
    w.heading(f"{case.reference} · {case.title}", size=17)
    w.row("Report type", report_type.capitalize())
    w.row("Status", case.status.replace("_", " "))
    w.row("Priority", case.priority)
    if case.category:
        w.row("Category", case.category)
    w.row("Generated", generated)
    if case.description:
        w.gap(2)
        w.para(case.description)

    findings = export["findings"]
    by_status = Counter(f["review"]["status"] for f in findings)
    w.heading("At a glance")
    w.row("Evidence files", len(evidence))
    w.row("Findings", len(findings))
    w.row("Approved", by_status.get("approve", 0))
    w.row("Rejected", by_status.get("reject", 0))
    w.row("Pending review", by_status.get("pending", 0))
    w.gap(2)
    w.para(DISCLAIMER, size=8.5, color=90)

    detailed = report_type == "detailed"
    if report_type != "executive" and "evidence" in sections:
        w.heading("Evidence")
        if not evidence:
            w.para("No evidence has been uploaded.")
        for e in evidence:
            pages = f", {e.page_count} pages" if e.page_count else ""
            w.para(f"{e.original_filename}", bold=True, size=9.5)
            line = f"{e.status.replace('_', ' ')}{pages}"
            if detailed and e.sha256:
                line += f" · SHA-256 {e.sha256[:16]}…"
            w.para(line, size=8.5, color=90)

    if "findings" in sections or report_type == "executive":
        w.heading("Findings")
        shown = (
            [f for f in findings if f["review"]["status"] == "pending"]
            if report_type == "executive"
            else findings
        )
        if not shown:
            w.para("No findings." if not findings else "No findings are waiting for review.")
        for number, f in enumerate(shown, start=1):
            w.para(
                f"{number}. {f['type'].replace('_', ' ').capitalize()} ({f['severity']} severity)",
                bold=True,
                size=10,
            )
            w.row("Expected", _value(f.get("expected")))
            w.row("Observed", _value(f.get("observed")))
            w.row("Difference", _value(f.get("difference")))
            review = f["review"]
            decided = f" by {review['by']}" if review.get("by") else ""
            w.row("Review", f"{review['status']}{decided}")
            if detailed:
                if review.get("note"):
                    w.row("Reviewer note", review["note"])
                if f.get("explanation"):
                    label = (
                        "Explanation (machine-generated)"
                        if f.get("explanation_source") == "machine_generated"
                        else "Explanation"
                    )
                    w.row(label, f["explanation"])
                for e in f["evidence"]:
                    w.row(
                        "Source",
                        f"{e['file_name']} p{e['page']}: “{e['quote']}” ({e['grounding']})",
                    )
            w.gap(1.5)

    if report_type != "executive" and "timeline" in sections:
        w.heading("Timeline")
        if not timeline:
            w.para("Nothing has happened on this case yet.")
        for event, user in timeline:
            who = user.name if user else ("an API key" if event.actor_api_key_id else "system")
            when = event.created_at.strftime("%Y-%m-%d %H:%M")
            title = event.details.get("title") or event.action.replace(".", " ").replace("_", " ")
            w.para(f"{when}  ·  {title}  ·  {who}", size=8.5, color=60)

    if "recommendations" in sections and report_type != "executive":
        w.heading("Next steps")
        pending = [f for f in findings if f["review"]["status"] == "pending"]
        failed = [e for e in evidence if e.status in ("failed_terminal", "needs_review")]
        steps = []
        if pending:
            steps.append(f"Review the {len(pending)} finding(s) still pending.")
        if failed:
            names = ", ".join(e.original_filename for e in failed[:5])
            steps.append(f"Check the evidence that couldn't be fully read: {names}.")
        if not steps:
            steps.append("Nothing outstanding: every finding has a decision.")
        for step in steps:
            w.para(f"• {step}")
