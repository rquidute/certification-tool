"""Phase 5b: abort a test run that is actually running, and check the engine recovers."""
import os
import threading
import time
from typing import Any, Dict

from .. import console
from ..results import FAIL, NOT_RUN, PASS
from ..session import Session, shell_join

G = "cli"
STEP = "abort-testing-running"


def run(s: Session) -> None:
    cfg = s.m.get("abort_run")
    if not cfg:
        return
    meta = next((e for e in s.m["suites"] if e["id"] == cfg["from"]), None)
    if s.dry_run:
        s.record(STEP, G, NOT_RUN, "dry-run")
        return
    if not meta or "TODO" in (meta["suite"], meta["test"]):
        s.record(STEP, G, NOT_RUN, f"manifest entry {cfg['from']!r} missing or has TODO")
        return
    s.results.progress = "abort while running"
    sdir = s.step_dir(G, STEP)
    if meta.get("dut"):
        err = s.start_dut(meta["dut"], fresh=True, step_dir=sdir)
        if err:
            s.record(STEP, G, FAIL, f"INFRA: DUT did not start: {err}")
            return
    before = s.th.last_execution() or 0
    args = [s.th.binary, "run-tests", "-t", meta["test"], "-n", "sanity-abort-while-running", "--no-color", "--no-streaming"]
    if s.project_id:
        args += ["--project-id", str(s.project_id)]
    if meta.get("test_args"):
        args += ["--", *meta["test_args"]]
    cmd = shell_join(args) + "   # then aborted with: " + shell_join([s.th.binary, "abort-testing"])
    dcmd = getattr(s.dut, "last_command", "") if meta.get("dut") else ""

    def rec(*a, **k):
        return s.record(*a, command=cmd, dut_command=dcmd, **k)

    transcript = os.path.join(sdir, "transcript.out")
    result: Dict[str, Any] = {}

    def worker() -> None:
        result["out"], result["rc"], result["why"] = s.prompts.run(
            args, s.prompt_rules(meta), s.run_timeout, transcript=transcript,
            text_rules=meta.get("text_answers"), transcript_notes=[f"DUT started with: {dcmd}"] if dcmd else None)

    t0 = time.time()
    th = threading.Thread(target=worker, daemon=True)
    th.start()
    running, st = s.th.wait_state("running", 90, stop=th)
    if not running:
        rec(STEP, G, FAIL,
                 f"runner never reached 'running' (last state {st!r}); the run "
                 f"{'ended first' if not th.is_alive() else 'is stuck'}: {str(result.get('why') or result.get('out', ''))[-300:]}",
                 time.time() - t0)
        th.join(timeout=5)
        return
    time.sleep(cfg.get("after_s", 8))  # let the test get properly under way
    if not th.is_alive():
        rec(STEP, G, FAIL, f"test finished before the abort was sent; pick a longer test in abort_run.from "
                 f"(after_s={cfg.get('after_s', 8)})", time.time() - t0)
        return
    t_abort = time.time()
    rc, out = s.th.run(["abort-testing"], timeout=60)
    s.save_cli_output(G, STEP, ["abort-testing"], rc, out, time.time() - t_abort)
    rec(STEP, G, PASS if rc == 0 else FAIL,
             f"abort accepted: {out.strip()[-100:]}" if rc == 0 else f"rc={rc}\n{out.strip()[-300:]}", time.time() - t0)
    th.join(timeout=120)
    if not th.is_alive():  # the run's writer is done, so appending cannot interleave with its output
        with open(transcript, "a") as f:
            f.write(f"\n# abort request, sent while the run above was in progress:\n"
                    f"$ {shell_join([s.th.binary, 'abort-testing'])}\n{out}" + ("" if out.endswith("\n") else "\n"))
    s.record("run-ends-after-abort", G, PASS if not th.is_alive() else FAIL,
             "th-cli exited after the abort" if not th.is_alive() else "th-cli still running 120s after the abort")
    idle, st = s.th.wait_state("idle", 60)
    s.record("runner-idle-after-abort", G, PASS if idle else FAIL,
             "runner back to idle" if idle else f"runner still {st!r} 60s after the abort")
    eid = s.th.last_execution()
    if eid and eid > before:
        state = s.th.exec_state(eid)
        s.record("execution-cancelled-after-abort", G, PASS if state and "cancel" in state else FAIL,
                 f"execution {eid} state: {state!r} (expected 'cancelled')")
    else:
        s.record("execution-cancelled-after-abort", G, FAIL, "no new execution was recorded for the aborted run")
