"""Step ledger: records every step, prints it, and answers "what is the verdict?"."""
import time
from typing import Dict, List, Tuple

from . import console
from .console import SYMBOL, LABEL, STATUS_COLOR, bar, paint

PASS, FAIL, NOT_RUN = "PASS", "FAIL", "NOT_RUN"
STATUSES = (PASS, FAIL, NOT_RUN)

# Report categories. Coverage is a pre-flight gate (is every suite accounted for?), not a test result, so it
# is counted on its own and never mixed into the test PASS numbers.
CATEGORY = {"cli": "CLI commands", "executions": "CLI commands", "suites": "Test suites",
            "custom": "Custom tests", "coverage": "Coverage gate", "cleanup": "Harness", "harness": "Harness"}
CATEGORY_ORDER = ["Test suites", "Custom tests", "CLI commands", "Coverage gate", "Harness"]


def counts(steps: List[dict]) -> Dict[str, int]:
    return {k: sum(1 for s in steps if s["status"] == k) for k in STATUSES}


class Results:
    def __init__(self) -> None:
        self.steps: List[dict] = []
        self.t0 = time.time()
        self.progress = ""  # shown in the periodic "still running" line, set by the phases

    # ---- time and headers
    def elapsed(self) -> str:
        s = int(time.time() - self.t0)
        return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"

    def stamp(self) -> str:
        return paint(f"[{self.elapsed()}]", "gray")

    def tally(self) -> str:
        c = counts(self.steps)
        return "  ".join(paint(f"{SYMBOL[k]} {c[k]}", STATUS_COLOR[k] if c[k] else "gray") for k in STATUSES)

    def phase(self, n: int, name: str, total: int = 5) -> None:
        print(f"\n{self.stamp()} " + paint(f"Phase {n}/{total}", "bold", "cyan") + f" {bar(n - 1, total, 10)} "
              + paint(name, "bold") + "   " + self.tally(), flush=True)

    # ---- recording
    def record(self, sid: str, group: str, status: str, detail: str = "", dur: float = 0.0, log: str = "",
               command: str = "", dut_command: str = "") -> str:
        """`command` is the th-cli command line the step ran; `dut_command` how the DUT app was started."""
        self.steps.append({"id": sid, "group": group, "status": status, "detail": detail.strip()[-1500:],
                           "duration_s": round(dur, 1), "log": log, "command": command, "dut_command": dut_command})
        badge = paint(f"{SYMBOL[status]} {LABEL[status]}", "bold", STATUS_COLOR[status])
        took = paint(f" ({dur:.0f}s)", "gray") if dur >= 1 else ""
        lines = detail.strip().splitlines()
        first = lines[0][:100] if lines else ""
        print(f"{self.stamp()} {badge} {paint(group + '/', 'gray')}{paint(sid, 'bold')}{took} "
              f"{paint(first, 'dim') if status != FAIL else first}", flush=True)
        if status == FAIL:  # show why right away instead of only in the report
            bar_ = paint("           │ " if console.UNICODE else "           | ", "red")
            if command:
                print(bar_ + paint(f"command: {command}", "dim"), flush=True)
            for ln in lines[1:15]:
                print(bar_ + paint(ln[:140], "dim"), flush=True)
        return status

    # ---- verdict
    def attention(self) -> Tuple[List[dict], List[dict]]:
        """(failed steps, steps that were not run without an explicit exclusion)."""
        fails = [s for s in self.steps if s["status"] == FAIL]
        unexpected = [s for s in self.steps if s["status"] == NOT_RUN and not s["id"].startswith("excluded-")]
        return fails, unexpected

    def is_go(self) -> bool:
        fails, unexpected = self.attention()
        return not fails and not unexpected

    def by_category(self) -> Dict[str, List[dict]]:
        cats: Dict[str, List[dict]] = {c: [] for c in CATEGORY_ORDER}
        for s in self.steps:
            cats.setdefault(CATEGORY.get(s["group"], "Harness"), []).append(s)
        return {c: v for c, v in cats.items() if v}
