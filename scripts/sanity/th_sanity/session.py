"""Shared state and services for one sanity run. Phases receive a Session and never touch globals."""
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from .dut import DutError, DutManager
from .prompts import PromptDriver
from .results import FAIL, PASS, Results
from .thcli import ThCli, shell_join  # noqa: F401  (shell_join is re-exported for the phases)

# <run dir>/<category>/<step id>/... : every step keeps its own artifacts (transcript, DUT log, th-cli run log, exports)
STEP_DIRS = {"suites": "suites", "custom": "custom", "cli": "cli", "executions": "cli", "coverage": "coverage",
             "cleanup": "cleanup", "harness": "harness"}


CLI_OUTPUT = "cli-output.txt"  # the command, its exit code and its full output, one per CLI step


class Session:
    def __init__(self, manifest: dict, manifest_dir: str, out_dir: str, dry_run: bool = False,
                 th: Optional[ThCli] = None, dut: Optional[DutManager] = None,
                 prompts: Optional[PromptDriver] = None) -> None:
        self.m = manifest
        self.manifest_dir = manifest_dir
        self.out_dir = out_dir
        self.dry_run = dry_run
        self.th_root = os.path.expanduser(manifest.get("th_root", "~/certification-tool"))
        self.results = Results()
        self.th = th or ThCli(manifest.get("th_cli", "th-cli"))
        self.dut = dut or DutManager(manifest.get("dut_profiles", {}), manifest.get("apps_dir", "~/apps"), out_dir)
        self.prompts = prompts or PromptDriver(self.th, self.results, self.dut)
        # state shared between phases
        self.project_id: Optional[int] = None
        self.extra_projects: List[int] = []
        self.exec_ids: Dict[str, int] = {}  # step id -> execution id of a passed run
        self.exec_meta: Dict[str, dict] = {}  # step id -> manifest entry that produced it
        self.live: Dict[str, set] = {}  # suite public_id -> test public_ids currently in the TH
        self.versions: Dict[str, str] = {}  # parsed `th-cli --version`: the TH under test
        self.only: Optional[set] = None  # --only: ids of the suite entries to run (None = all)

    # ---- files
    def step_dir(self, group: str, sid: str) -> str:
        path = os.path.join(self.out_dir, STEP_DIRS.get(group, group), sid)
        os.makedirs(path, exist_ok=True)
        return path

    def abs_path(self, p: Optional[str]) -> Optional[str]:
        return os.path.join(self.manifest_dir, p) if p and not os.path.isabs(p) else p

    # ---- manifest helpers
    @property
    def run_timeout(self) -> int:
        return self.m.get("default_run_timeout_s", 900)

    def prompt_rules(self, entry: dict) -> List[dict]:
        """Step-specific answers first, then the manifest-wide defaults."""
        return entry.get("prompt_answers", []) + self.m.get("default_prompt_answers", [])

    def custom_fixtures(self) -> List[dict]:
        fx = self.m.get("custom", [])
        if isinstance(fx, dict):  # older manifest layout: {"yaml": {...}, "python": {...}}
            fx = [{"id": k, **v} for k, v in fx.items()]
        return fx

    # ---- recording shortcuts
    def record(self, sid: str, group: str, status: str, detail: str = "", dur: float = 0.0, log: str = "",
               command: str = "", dut_command: str = "") -> str:
        return self.results.record(sid, group, status, detail, dur, log, command, dut_command)

    def save_cli_output(self, group: str, sid: str, args: List[str], rc: int, out: str, secs: float) -> str:
        """Keep a CLI command's output with its step: <run>/<category>/<step>/cli-output.txt."""
        path = os.path.join(self.step_dir(group, sid), CLI_OUTPUT)
        with open(path, "w") as f:
            f.write(f"$ {shell_join([self.th.binary, *args])}\nexit code: {rc}\n"
                    f"duration: {secs:.1f}s\n----- output -----\n{out}")
            if not out.endswith("\n"):
                f.write("\n")
        return path

    def cli_step(self, sid: str, group: str, args: List[str], ok_rc: Tuple[int, ...] = (0,), env: Optional[dict] = None,
                 must_contain: str = "", timeout: int = 120) -> Tuple[bool, str]:
        """Run one th-cli command as a recorded step. Passes when the exit code is allowed and the output contains
        `must_contain`. The full output is saved in the step folder."""
        t0 = time.time()
        rc, out = self.th.run(args, timeout=timeout, env=env)
        self.save_cli_output(group, sid, args, rc, out, time.time() - t0)
        good = rc in ok_rc and (must_contain in out)
        why = "" if good else f"rc={rc}" + (f", missing {must_contain!r}" if must_contain not in out else "")
        self.record(sid, group, PASS if good else FAIL, why + "\n" + out[-400:] if not good else "th-cli " + " ".join(args),
                    time.time() - t0, command=shell_join([self.th.binary, *args]))
        return good, out

    # ---- DUT
    def start_dut(self, profile: str, fresh: bool, step_dir: str) -> Optional[str]:
        """Start the DUT with its log in the step folder. Returns None on success, else the failure text."""
        from . import console
        console.note(f"starting DUT '{profile}' ({'fresh' if fresh else 'reuse'})")
        try:
            self.dut.start(profile, fresh=fresh, log_dir=step_dir)
        except DutError as e:
            return str(e)
        console.note(f"DUT ready (log: {self.dut.log_path}; {self.dut.ready_how})")
        return None
