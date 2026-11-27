"""Phase 3: one representative test per suite, DUT started in the state each manifest entry asks for."""
import time
from typing import Dict, List

from .. import console
from ..results import FAIL, NOT_RUN, PASS
from ..runtest import run_test
from ..session import Session


def run(s: Session) -> None:
    done: Dict[str, str] = {}
    entries = s.m["suites"]
    total = len(entries)
    durations: List[float] = []
    t_prev = time.time()
    for i, e in enumerate(entries, 1):
        sid = e["id"]
        s.results.progress = f"suite {i}/{total}"
        if i > 1 and time.time() - t_prev > 5:  # skipped steps are instant and would skew the average
            durations.append(time.time() - t_prev)
        t_prev = time.time()
        head = f"{s.results.stamp()} {console.paint(f'suite {i}/{total}', 'bold', 'cyan')} {console.bar(i - 1, total)} {console.paint(sid, 'bold')}"
        if durations:
            avg = sum(durations) / len(durations)
            head += " " + console.paint(f"(avg {avg:.0f}s/suite, ~{int(avg * (total - i + 1) // 60)}m left, estimate only)", "gray")
        print(head, flush=True)
        dep = e.get("depends_on")
        if dep and done.get(dep) != PASS:
            s.record(f"run-{sid}", "suites", NOT_RUN, f"depends on {dep}, which did not pass")
            continue
        if "TODO" in (e["suite"], e["test"]):
            s.record(f"run-{sid}", "suites", NOT_RUN, "manifest entry has TODO")
            continue
        if s.dry_run:
            s.record(f"run-{sid}", "suites", NOT_RUN, "dry-run")
            continue
        n = len(s.results.steps)
        eid = run_test(s, f"run-{sid}", "suites", e["test"], e.get("expect", "pass"), e.get("prompt_answers", []),
                       e.get("dut"), e.get("dut_start", "fresh"), config=s.abs_path(e.get("config")),
                       text_answers=e.get("text_answers"))
        done[sid] = s.results.steps[-1]["status"] if len(s.results.steps) > n else FAIL
        if eid and done[sid] == PASS:
            s.exec_ids[sid] = eid
            s.exec_meta[sid] = e
