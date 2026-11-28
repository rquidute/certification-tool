"""Release sanity harness for the Matter Test Harness (TH).

Runs on the TH host (needs th-cli, a running backend and the DUT apps in ~/apps). Standard library only.

    python3 scripts/sanity/run_sanity.py --version-label v2.17-beta1
    python3 scripts/sanity/run_sanity.py --dry-run     # coverage gate + read-only commands only
    python3 scripts/sanity/run_sanity.py --list-suites # suite/test ids to put in manifest.json

What it does, in order:
  0. Pre-flight: delete sanity projects left behind by earlier sessions.
  1. CLI commands that need no test run, against a throwaway project.
  2. Coverage gate: every live suite must be in the manifest (or explicitly excluded).
  3. One test per suite, DUT started in a known state (fresh or reuse credentials).
  4. Custom YAML + Python tests: add, rescan, check, run, remove, rescan.
  5. test-run-execution commands, repeat of a previous run, abort of a running test.
  6. Report (Markdown + JSON) in sanity_reports/<run>/ and exit code: 0 = GO, 1 = NO-GO.

Design rules: a step is PASS only when it ran and matched its expectation. Anything not run is NOT_RUN
and makes the verdict NO-GO unless it is an explicit, reasoned exclusion. An unknown interactive prompt
fails the step; the harness never guesses an answer.
"""
import argparse
import datetime
import json
import os
import re
import sys
from typing import List, Optional, Tuple

from . import console
from .console import paint
from .dut import DutError
from .lock import LockHeld, acquire_lock
from .versions import describe, label_matches, parse_versions
from .phases import abort, cli_commands, coverage, custom, executions, preclean, suites
from .report import write_report
from .results import FAIL
from .session import Session

# (title, function) in execution order; the pre-flight cleanup runs before phase 1.
PHASES = [
    ("CLI commands", cli_commands.run),
    ("Coverage gate", coverage.run),
    ("Test suites (one test per suite)", suites.run),
    ("Custom tests (YAML + Python)", custom.run),
    ("CLI commands on executions and runs", lambda s: (executions.run(s), abort.run(s))),
]


def _focused(s: Session) -> None:
    """--only: create the throwaway project, then run just the chosen suite entries."""
    if not s.dry_run:
        cli_commands.create_project(s)
        if s.project_id is None:
            return
    suites.run(s)


def confirm_start(existing: Optional[List[Tuple[int, str]]] = None) -> bool:
    """Ask the operator to close the TH UI and wait for confirmation. The TH sends every test prompt to all
    connected web clients, so an open UI can answer or disturb the prompts the harness answers over the CLI."""
    print(f"\n{console.rule(72)}")
    print(paint(" Before starting", "bold", "yellow"))
    print(f"  {paint('1.', 'bold')} Close the TH web UI (every browser tab). The TH sends test prompts to all")
    print("     connected clients, so a click in the UI can answer or disturb a prompt.")
    print(f"  {paint('2.', 'bold')} Do not start other test runs. The harness uses the test engine exclusively.")
    if existing:
        print(f"  {paint('3.', 'bold')} {paint('These running DUT apps will be stopped (--kill-existing) and not restored:', 'yellow')}")
        for pid, cmd in existing:
            print(f"       pid {pid}: {cmd[:90]}")
    else:
        print(f"  {paint('3.', 'bold')} No DUT app is running. The harness starts its own and stops it at the end.")
    print(console.rule(72))
    try:
        answer = input(paint(" Press Enter once the UI is closed to continue, or type n to cancel: ", "bold")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ("", "y", "yes", "c", "continue")


def build_parser(default_manifest: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", default=default_manifest)
    ap.add_argument("--out-dir", default="sanity_reports")
    ap.add_argument("--dry-run", action="store_true", help="coverage gate and read-only CLI commands only")
    ap.add_argument("--version-label", default="", help="TH version string for the report header and the run folder name")
    ap.add_argument("--yes", action="store_true", help="skip the 'close the TH UI' confirmation (unattended runs)")
    ap.add_argument("--kill-existing", action="store_true",
                    help="stop a DUT app (e.g. chip-all-clusters-app) that is already running instead of refusing to start")
    ap.add_argument("--only", default="", metavar="ID[,ID...]",
                    help="focused run: only these suites[] entries (plus the entries they depend on); skips the CLI, custom and "
                         "execution phases, so it is NOT a release verdict. Use it to iterate on a failing suite")
    ap.add_argument("--keep-project", choices=["auto", "always", "never"], default="auto",
                    help="keep the throwaway project after the run so failing commands can be re-run against it "
                         "(auto = only when the run did not end GO; the next run's pre-flight deletes it)")
    ap.add_argument("--no-preclean", action="store_true", help="do not delete leftover sanity projects from earlier sessions")
    ap.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                    help="colored output (auto = only on a terminal and when NO_COLOR is not set)")
    ap.add_argument("--list-suites", action="store_true", help="print live suites (public_id, size, sample tests) and exit")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    default_manifest = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "manifest.json")
    a = build_parser(default_manifest).parse_args(argv)
    console.set_color(a.color)
    with open(a.manifest) as f:
        manifest = json.load(f)
    label = re.sub(r"[^A-Za-z0-9._+-]+", "-", a.version_label).strip("-")
    run_dir = os.path.join(a.out_dir, "sanity_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + (f"_{label}" if label else ""))
    s = Session(manifest, os.path.dirname(os.path.abspath(a.manifest)), run_dir, a.dry_run)

    if a.only:
        wanted = [x.strip() for x in a.only.split(",") if x.strip()]
        known = {e["id"]: e for e in manifest["suites"]}
        bad = [x for x in wanted if x not in known]
        if bad:
            print(f" Unknown suite entry id(s) for --only: {', '.join(bad)}\n Valid ids: {', '.join(known)}", file=sys.stderr)
            return 2
        chosen, queue = set(), list(wanted)
        while queue:  # an entry that depends on another needs it to run first
            sid = queue.pop()
            if sid not in chosen:
                chosen.add(sid)
                if known[sid].get("depends_on"):
                    queue.append(known[sid]["depends_on"])
        s.only = chosen

    if a.list_suites:
        if not coverage.load_live(s):
            print(s.results.steps[-1]["detail"])
            return 1
        for sid, tests in sorted(s.live.items()):
            print(f"{sid!r}: {len(tests)} tests, e.g. {sorted(tests)[:3]}")
        return 0

    # Two runs at once would share the engine and the DUT and delete each other's projects.
    if not a.dry_run:
        try:
            s.lock = acquire_lock()
        except LockHeld as e:
            print(f" {e}. Nothing was changed.", file=sys.stderr)
            return 2

    # A DUT app that is already running would be killed on the first test. Never do that silently.
    try:
        existing = [] if a.dry_run else s.dut.find_running()
    except DutError as e:
        print(f" {e}\n Cannot tell whether a DUT app is already running, so nothing was started.", file=sys.stderr)
        return 2
    if existing and not a.kill_existing:
        print(paint(" A DUT app is already running; the harness would have to kill it:", "bold", "yellow"), file=sys.stderr)
        for pid, cmd in existing:
            print(f"   pid {pid}: {cmd[:100]}", file=sys.stderr)
        print(" Stop it yourself, or rerun with --kill-existing to let the harness stop it (it is not restored).\n"
              " Nothing was changed.", file=sys.stderr)
        return 2

    # Dry run only reads, so an open UI cannot interfere and no confirmation is needed.
    if not a.dry_run and not a.yes:
        if not sys.stdin.isatty():
            print("Not running in a terminal, so the 'close the TH UI' confirmation cannot be asked. "
                  "Close the UI yourself and pass --yes.", file=sys.stderr)
            return 2
        if not confirm_start(existing):
            print(paint("Cancelled. Nothing was changed.", "yellow"))
            return 2

    # The TH under test, as the TH itself reports it. A label is only a name; it must not claim another version.
    s.versions = parse_versions(s.th.run(["--version"])[1])
    version = a.version_label or s.versions.get("th", "unknown")
    print(f"\n TH under test: {paint(describe(s.versions), 'bold')}" + (f"   (label: {a.version_label})" if a.version_label else ""))
    try:
        if existing:
            preclean.stop_existing_dut(s, existing)
        if a.version_label and "th" in s.versions and not label_matches(a.version_label, s.versions["th"]):
            s.record("th-version-matches-label", "harness", FAIL,
                     f"the label says {a.version_label!r} but the TH under test reports {s.versions['th']!r}. "
                     "Either the label is wrong or the wrong TH build is deployed; this report would be mislabelled")
        if not a.no_preclean:
            preclean.preclean(s)
        phases = PHASES if s.only is None else [("Focused: " + ", ".join(sorted(s.only)), _focused)]
        for n, (title, fn) in enumerate(phases, 1):
            s.results.phase(n, title, len(phases))
            fn(s)
    except KeyboardInterrupt:
        s.record("interrupted", "harness", FAIL, "interrupted by user")
    finally:
        if not a.dry_run:  # dry run must never kill a DUT app the user is running
            try:
                preclean.final_cleanup(s, keep=a.keep_project)
            except KeyboardInterrupt:
                print("cleanup interrupted; writing report anyway", flush=True)
    return write_report(s, version)
