"""Phase 1: every th-cli command that does not need a test run, against a throwaway project."""
import os
import time

from ..results import FAIL, PASS
from ..session import Session
from ..thcli import items


def run(s: Session) -> None:
    g = "cli"
    s.cli_step("version", g, ["--version"])
    s.cli_step("test-runner-status", g, ["test-runner-status", "--json"])
    s.cli_step("available-tests-json", g, ["available-tests", "--json"])
    s.cli_step("available-tests-compact", g, ["available-tests", "--compact"])
    s.cli_step("available-tests-group-by-cluster", g, ["available-tests", "--group-by-cluster"])
    s.cli_step("available-tests-cluster", g, ["available-tests", "--cluster", "OnOff"])
    s.cli_step("project-list", g, ["project", "list", "--json"])
    if s.dry_run:
        return
    name = create_project(s)
    if s.project_id is None:
        return
    pid = str(s.project_id)
    s.cli_step("project-update", g, ["project", "update", "-i", pid, "-n", name + "-renamed"])
    s.cli_step("project-edit", g, ["project", "edit", "-i", pid], env={"EDITOR": "true", "VISUAL": "true"})
    s.cli_step("project-applicable-tests", g, ["project", "applicable-tests", "-i", pid, "--json"])
    exp = os.path.join(s.step_dir(g, "project-export"), "project_export.json")
    if s.cli_step("project-export", g, ["project", "export", "-i", pid, "-o", exp])[0]:
        before = {p.get("id") for p in s.th.projects() or []}
        if s.cli_step("project-import", g, ["project", "import", "-f", exp])[0]:
            after = {p.get("id") for p in s.th.projects() or []}
            s.extra_projects += sorted(x for x in after - before if x is not None)
    s.cli_step("rescan-tests", g, ["rescan-tests"])
    _abort_when_idle(s, g)


def create_project(s: Session) -> str:
    """Create the throwaway project every test run is attached to; sets s.project_id (None if it failed)."""
    g = "cli"
    name = f"sanity-harness-{int(time.time())}"
    good, _ = s.cli_step("project-create", g, ["project", "create", "-n", name])
    if good:
        s.project_id = s.th.find_project(name)
    if s.project_id is None:
        s.record("project-id-lookup", g, FAIL, f"could not find project {name!r} in `project list --json`")
    return name


def _abort_when_idle(s: Session, g: str) -> None:
    """Idle engine: abort must be refused (409 "not active") and the engine must stay idle. The state is
    checked first so this step can never abort a run that somebody else started."""
    st = s.th.runner_state()
    if st != "idle":
        s.record("runner-idle-before-abort", g, FAIL, f"runner state is {st!r}, not 'idle': a test run is active, "
                 "finish or abort it before running the sanity harness")
        return
    s.record("runner-idle-before-abort", g, PASS, "test-runner-status: idle")
    s.cli_step("abort-testing-idle-refused", g, ["abort-testing"], ok_rc=(1,), must_contain="not active")
    after = s.th.runner_state()
    s.record("runner-still-idle-after-abort", g, PASS if after == "idle" else FAIL, f"state after refused abort: {after!r}")
