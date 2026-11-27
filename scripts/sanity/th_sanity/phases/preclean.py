"""Pre-flight removal of projects left by earlier sessions, and the final cleanup of this one."""
import re
from typing import List, Optional, Tuple

from .. import console
from ..results import FAIL, PASS
from ..session import Session

# Projects the harness creates: sanity-harness-<epoch>[-renamed], plus the older sanity-<epoch>[-renamed] form and
# whatever suffix `project import` appends to a copy. Anchored, and requires a 9+ digit epoch, so a project that
# merely starts with "sanity" (e.g. "sanity-checks") is never matched.
DEFAULT_PROJECT_REGEX = r"^sanity-(?:harness-)?\d{9,}(?:-renamed)?(?:[ _(\-].*)?$"


def stale_projects(s: Session) -> Optional[List[dict]]:
    rx = re.compile(s.m.get("cleanup", {}).get("project_name_regex", DEFAULT_PROJECT_REGEX))
    projects = s.th.projects()
    if projects is None:
        return None
    return [p for p in projects if isinstance(p.get("name"), str) and p.get("id") is not None and rx.match(p["name"])]


def preclean(s: Session) -> None:
    """Remove projects left behind by earlier sanity sessions (crash, Ctrl-C). Only names matching the
    harness's own pattern are touched; a user's projects are never deleted. Nothing is deleted in a dry run."""
    g = "cleanup"
    print(f"\n{s.results.stamp()} " + console.paint("Pre-flight", "bold", "cyan") + " "
          + console.paint("stale sanity projects", "bold"), flush=True)
    stale = stale_projects(s)
    if stale is None:
        s.record("preclean-list-projects", g, FAIL, "could not read `project list --json`")
        return
    if not stale:
        s.record("preclean-projects", g, PASS, "no stale sanity projects")
        return
    names = ", ".join(f"{p['id']}:{p['name']}" for p in stale)
    limit = s.m.get("cleanup", {}).get("max_delete", 25)
    if len(stale) > limit:
        s.record("preclean-projects", g, FAIL, f"{len(stale)} projects match the cleanup pattern (limit {limit}); "
                 f"refusing to delete them. Check cleanup.project_name_regex. Matches: {names}")
        return
    if s.dry_run:
        s.record("preclean-projects", g, PASS, f"dry run: would delete {len(stale)}: {names}")
        return
    for p in stale:
        s.cli_step(f"preclean-project-{p['id']}", g, ["project", "delete", "-i", str(p["id"]), "-y"])


def final_cleanup(s: Session) -> None:
    """Best effort. A second Ctrl-C must not abandon the remaining cleanup steps."""
    def guarded(label: str, fn) -> None:
        try:
            fn()
        except KeyboardInterrupt:
            print(f"      cleanup interrupted at: {label}. Check leftovers (th-cli project list, ps for the DUT).", flush=True)

    guarded("abort running test", lambda: s.th.run(["abort-testing"], timeout=60))
    guarded("stop DUT", s.dut.stop)
    for pid in [*s.extra_projects, s.project_id]:
        if pid is not None:
            guarded(f"delete project {pid}", lambda pid=pid: s.cli_step(
                f"cleanup-project-{pid}", "cleanup", ["project", "delete", "-i", str(pid), "-y"]))


def stop_existing_dut(s: Session, found: List[Tuple[int, str]]) -> None:
    """--kill-existing: stop DUT apps that were already running, and say exactly which."""
    names = "; ".join(f"pid {pid}: {cmd[:80]}" for pid, cmd in found)
    s.dut.stop()
    left = s.dut.find_running()
    if left:
        s.record("stop-existing-dut", "cleanup", FAIL, f"still running after stop: " + "; ".join(f"pid {p}" for p, _ in left)
                 + " (owned by another user, or started as root?)")
    else:
        s.record("stop-existing-dut", "cleanup", PASS, f"stopped (--kill-existing): {names}")
