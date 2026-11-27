"""Phase 5a: test-run-execution commands on the executions produced by the suite phase, plus project logs."""
import os
import time

from .. import console
from ..results import FAIL, NOT_RUN, PASS
from ..session import Session, shell_join


def run(s: Session) -> None:
    g = "executions"
    if s.dry_run:
        s.cli_step("execution-list", g, ["test-run-execution", "list", "--json"])
        return
    target = next((k for k in s.exec_ids if k in s.exec_meta), None)
    eid = s.exec_ids[target] if target else next(iter(s.exec_ids.values()), None)
    if not eid:
        s.record("executions", g, NOT_RUN, "no successful execution to operate on")
        return
    i = str(eid)
    sd = lambda name: s.step_dir(g, name)  # noqa: E731
    s.cli_step("execution-list", g, ["test-run-execution", "list", "--json"])
    s.cli_step("execution-show", g, ["test-run-execution", "list", "-i", i, "--json"])
    s.cli_step("execution-log", g, ["test-run-execution", "log", "-i", i, "-o", os.path.join(sd("execution-log"), "exec_log.txt")])
    _pics_export(s, g, i, sd("execution-pics-export"))
    if s.project_id:  # needs executions in the project, so it cannot run in the CLI-commands phase
        s.cli_step("project-logs", g, ["project", "logs", "-i", str(s.project_id), "-o", os.path.join(sd("project-logs"), "project_logs")])
    s.cli_step("execution-rename", g, ["test-run-execution", "rename", "-i", i, "-n", "sanity-renamed"])
    s.cli_step("execution-archive", g, ["test-run-execution", "archive", "-i", i])
    s.cli_step("execution-unarchive", g, ["test-run-execution", "unarchive", "-i", i])
    _export_import(s, g, i, sd("execution-export"))
    _repeat(s, g, target, sd("execution-repeat"))


def _pics_export(s: Session, g: str, i: str, out_dir: str) -> None:
    args = ["test-run-execution", "pics-export", "-i", i, "-o", os.path.join(out_dir, "exec_pics")]
    t0 = time.time()
    rc, out = s.th.run(args)
    s.save_cli_output(g, "execution-pics-export", args, rc, out, time.time() - t0)
    cmd = shell_join([s.th.binary, *args])
    if rc == 0:
        s.record("execution-pics-export", g, PASS, "exported", command=cmd)
    elif "No PICS were used" in out:  # a 404 with that exact reason is the documented answer for PICS-less runs
        s.record("execution-pics-export", g, PASS, "endpoint answered 'No PICS were used' (expected for these runs); the export path itself is NOT exercised", command=cmd)
    else:
        s.record("execution-pics-export", g, FAIL, f"rc={rc}\n{out[-400:]}", command=cmd)


def _export_import(s: Session, g: str, i: str, out_dir: str) -> None:
    exp = os.path.join(out_dir, "exec_export.json")
    if not s.cli_step("execution-export", g, ["test-run-execution", "export", "-i", i, "-o", exp])[0]:
        return
    imp = ["test-run-execution", "import", "-f", exp] + (["-p", str(s.project_id)] if s.project_id else [])
    before = s.th.exec_set()
    if s.cli_step("execution-import", g, imp)[0]:
        for x in sorted(s.th.exec_set() - before):
            s.cli_step(f"execution-delete-imported-{x}", g, ["test-run-execution", "delete", "-i", str(x), "-y"])


def _repeat(s: Session, g: str, target, sdir: str) -> None:
    """Repeat a previous passed run (DUT restarted fresh) and require a NEW execution with the same final state."""
    if not target:
        s.record("execution-repeat", g, NOT_RUN, "no passed suite execution to repeat")
        return
    meta = s.exec_meta[target]
    t0 = time.time()
    before = s.th.exec_set()
    if meta.get("dut"):
        err = s.start_dut(meta["dut"], fresh=True, step_dir=sdir)
        if err:
            s.record("execution-repeat", g, FAIL, f"INFRA: DUT did not start: {err}")
            return
    orig = s.exec_ids[target]
    orig_state = s.th.exec_state(orig)
    console.note(f"repeating execution {orig} ({target}, state {orig_state})")
    args = [s.th.binary, "test-run-execution", "repeat", "-i", str(orig), "--no-color", "--no-streaming"]
    rec = {"command": shell_join(args), "dut_command": getattr(s.dut, "last_command", "") if meta.get("dut") else ""}
    out, rc, why = s.prompts.run(args, s.prompt_rules(meta), s.run_timeout,
                                 transcript=os.path.join(sdir, "transcript.out"), text_rules=meta.get("text_answers"),
                                 transcript_notes=[f"DUT started with: {rec['dut_command']}"] if rec["dut_command"] else None)
    new_ids = sorted(s.th.exec_set() - before)
    new_state = s.th.exec_state(new_ids[-1]) if new_ids else None
    if why:
        s.record("execution-repeat", g, FAIL, f"INFRA: {why}", time.time() - t0, **rec)
    elif not new_ids:
        s.record("execution-repeat", g, FAIL, f"rc={rc}, but no new execution was created\n{out[-300:]}", time.time() - t0, **rec)
    elif new_state != orig_state:
        s.record("execution-repeat", g, FAIL, f"execution {new_ids[-1]} ended {new_state!r}, original {orig} was {orig_state!r}", time.time() - t0, **rec)
    else:
        s.record("execution-repeat", g, PASS, f"execution {orig} repeated as {new_ids[-1]}, both {new_state!r}", time.time() - t0, **rec)
    for x in new_ids:
        s.cli_step(f"execution-delete-repeated-{x}", g, ["test-run-execution", "delete", "-i", str(x), "-y"])
