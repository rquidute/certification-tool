"""Report writer: Markdown + JSON in the run folder, the console summary, and the `latest` pointer."""
import json
import os

from . import console
from .console import SYMBOL, STATUS_COLOR, paint
from .results import FAIL, NOT_RUN, PASS, STATUSES, counts
from .session import Session


def _markdown(s: Session, version: str, go: bool, fails, unexpected) -> str:
    cats = s.results.by_category()
    lines = [f"# TH sanity report - {version}", "", f"- Date: {os.path.basename(s.out_dir)}",
             f"- Total time: {s.results.elapsed()}", f"- Mode: {'DRY RUN' if s.dry_run else 'FULL'}",
             f"- **Verdict: {'GO' if go else 'NO-GO'}**", f"- Live suites: {len(s.live)}", "",
             "## Summary by category", "", "| Category | PASS | FAIL | NOT_RUN |", "|---|---|---|---|"]
    for c, st in cats.items():
        n = counts(st)
        lines.append(f"| {c} | {n[PASS]} | {n[FAIL]} | {n[NOT_RUN]} |")
    for c, st in cats.items():
        lines += ["", f"## {c}", "", "| Step | Status | Time s | Detail |", "|---|---|---|---|"]
        for step in st:
            det = step["detail"].splitlines()[0][:140] if step["detail"] else ""
            lines.append(f"| {step['id']} | {step['status']} | {step['duration_s']} | {det.replace('|', '/')} |")
    if fails or unexpected:
        lines += ["", "## Needs attention"]
        for step in fails + unexpected:
            lines += [f"### {step['group']}/{step['id']} ({step['status']})", "```", step["detail"], "```",
                      f"log: {step['log']}" if step["log"] else ""]
    return "\n".join(lines) + "\n"


def _print_summary(s: Session, go: bool, fails, unexpected) -> None:
    rule = console.rule(60)
    print(f"\n{rule}\n" + paint(f" Summary  ·  {s.results.elapsed()}", "bold"))
    print(paint(f" {'':<15}{'PASS':>7}{'FAIL':>7}{'SKIP':>7}", "gray"))
    for c, st in s.results.by_category().items():
        n = counts(st)
        print(f" {c:<15}" + "".join(paint(f"{n[k]:>7}", STATUS_COLOR[k] if n[k] else "gray") for k in STATUSES))
    print(rule)
    if fails or unexpected:
        print(paint(" Needs attention:", "bold"))
        for step in fails + unexpected:
            why = (step["detail"].splitlines() or [""])[0][:90]
            print(f"  {paint(SYMBOL[step['status']], STATUS_COLOR[step['status']])} {step['group']}/{step['id']}  {paint(why, 'dim')}")
        print(rule)
    print(" " + (paint("  GO  ", "bold", "inv", "green") if go else paint("  NO-GO  ", "bold", "inv", "red")))


def _point_latest_at(out_dir: str) -> None:
    latest = os.path.join(os.path.dirname(os.path.abspath(out_dir)), "latest")
    try:  # convenience pointer to the newest run folder
        if os.path.islink(latest):
            os.remove(latest)
        if not os.path.exists(latest):
            os.symlink(os.path.basename(os.path.abspath(out_dir)), latest)
    except OSError:
        pass


def write_report(s: Session, version: str) -> int:
    """Write report.md / report.json, print the summary, return the exit code (0 = GO, 1 = NO-GO)."""
    fails, unexpected = s.results.attention()
    go = not fails and not unexpected
    os.makedirs(s.out_dir, exist_ok=True)
    base = os.path.join(s.out_dir, "report")
    with open(base + ".md", "w") as f:
        f.write(_markdown(s, version, go, fails, unexpected))
    with open(base + ".json", "w") as f:
        json.dump({"version": version, "verdict": "GO" if go else "NO-GO", "counts": counts(s.results.steps),
                   "steps": s.results.steps}, f, indent=2)
    _print_summary(s, go, fails, unexpected)
    _point_latest_at(s.out_dir)
    print(f"Report: {base}.md")
    print(f"Logs:   {s.out_dir}/{{suites,custom,cli}}/<step>/")
    return 0 if go else 1
