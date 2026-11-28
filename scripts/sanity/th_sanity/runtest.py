"""Run one test case through `th-cli run-tests` and record the outcome as a step."""
import os
import re
import shutil
import time
from typing import List, Optional

from . import console
from .digest import failure_digest
from .results import FAIL, PASS
from .session import Session, shell_join


def merge_repeated_flags(args: List[str]) -> List[str]:
    """['--int-arg','A:1','--int-arg','B:2'] -> ['--int-arg','A:1 B:2'].

    th-cli keeps one value per inline flag, so a repeated `--int-arg` silently drops all but the last one (observed:
    three PIXIT values passed, one reached the test). The TH expects several values as one space-separated argument,
    the same format as the `int-arg` entries of a --tc-params-file."""
    merged: dict = {}
    for i, a in enumerate(args):
        if a.startswith("-"):
            merged.setdefault(a, [])
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                merged[a].append(args[i + 1])
    out: List[str] = []
    for flag, values in merged.items():
        out += [flag] + ([" ".join(values)] if values else [])
    return out


def run_test(s: Session, sid: str, group: str, test: str, expect: str, answers: List[dict], dut: Optional[str],
             dut_start: str, config: Optional[str] = None, timeout: Optional[int] = None,
             text_answers: Optional[List[dict]] = None, test_args: Optional[List[str]] = None) -> Optional[int]:
    """Start the DUT (if the test needs one), run `test`, record PASS/FAIL, return the new execution id.

    A step passes only when th-cli finished by itself and the outcome (pass/fail) equals `expect`.
    Infrastructure problems (DUT did not start, unhandled prompt, timeout, no results line) always fail."""
    t0 = time.time()
    timeout = timeout or s.run_timeout
    sdir = s.step_dir(group, sid)
    if dut:
        err = s.start_dut(dut, fresh=(dut_start != "reuse"), step_dir=sdir)
        if err:
            s.record(sid, group, FAIL, f"INFRA: DUT did not start: {err}")
            return None
    console.note(f"running {test} (timeout {timeout}s, transcript: {sdir}/transcript.out)")
    args = [s.th.binary, "run-tests", "-t", test, "-n", f"sanity-{sid}", "--no-color", "--no-streaming"]
    if s.project_id:
        args += ["--project-id", str(s.project_id)]
    if config:
        args += ["-c", config]
    if test_args:  # inline test parameters, e.g. ["--int-arg", "PIXIT.CGEN.TCRevision:1"]
        args += ["--", *merge_repeated_flags(test_args)]
    cmd, dut_cmd = shell_join(args), (getattr(s.dut, "last_command", "") if dut else "")
    out, rc, why = s.prompts.run(args, answers + s.m.get("default_prompt_answers", []), timeout,
                                 transcript=os.path.join(sdir, "transcript.out"), text_rules=text_answers,
                                 transcript_notes=[f"DUT started with: {dut_cmd}"] if dut_cmd else None)
    log = (re.findall(r"Log output in:?\s*(\S+)", out) or [""])[-1]
    if log and os.path.isfile(log):  # keep th-cli's own run log with the step it belongs to
        try:
            log = shutil.copy(log, os.path.join(sdir, "th-cli-run.log"))
        except OSError:
            pass
    results = (re.findall(r"(?m)^Results:\s*(.+)$", out) or [""])[-1]
    if dut and not s.dut.is_alive():
        why = why or "DUT process died during the run"
    if why:
        extra = "" if why.startswith("UNHANDLED") else "\n" + out[-400:]
        s.record(sid, group, FAIL, f"INFRA: {why}{extra}", time.time() - t0, log, command=cmd, dut_command=dut_cmd)
        return s.th.last_execution()
    if not results and rc != 0:
        s.record(sid, group, FAIL, f"INFRA: rc={rc}, no Results line\n{out[-400:]}", time.time() - t0, log,
                 command=cmd, dut_command=dut_cmd)
        return s.th.last_execution()
    # th-cli exits 0 when nothing failed, which includes a test that was skipped or not applicable.
    # That is not a pass: it needs an actual 'passed' in the summary.
    actual = "fail" if rc != 0 else ("pass" if re.search(r"\bpassed\b", results) else "none")
    detail = f"{test}: {results or 'no results line'} (expected {expect}, got {actual})"
    if actual == "none":
        detail += "\nexit code 0 but no test passed: a skipped / not applicable test is not a pass"
    if actual != expect:  # say WHY on the console and in the report, not only that it failed
        digest = failure_digest(out)
        if digest:
            detail += "\nfailing lines in the th-cli output:\n" + "\n".join(digest)
    s.record(sid, group, PASS if actual == expect else FAIL, detail, time.time() - t0, log,
             command=cmd, dut_command=dut_cmd)
    return s.th.last_execution()
