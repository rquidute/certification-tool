"""Thin wrapper around the `th-cli` executable: run a command, read its JSON, query the runner."""
import json
import os
import re
import subprocess
import threading
import time
from typing import Any, List, Optional, Tuple

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip(text: str) -> str:
    """Remove colour escapes and carriage returns from terminal output."""
    return ANSI.sub("", text).replace("\r", "")


def items(obj: Any) -> List[dict]:
    """th-cli --json lists are either a bare list or wrapped ({'items': [...]})."""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for key in ("items", "results", "data"):
            if isinstance(obj.get(key), list):
                return obj[key]
    return []


def enum_name(value: Any) -> Optional[str]:
    """The CLI prints enum reprs ('TestRunnerState.IDLE', 'TestStateEnum.PASSED'); keep the member name."""
    return None if value is None else str(value).split(".")[-1].lower()


class ThCli:
    def __init__(self, binary: str = "th-cli") -> None:
        self.binary = binary

    def run(self, args: List[str], timeout: int = 120, env: Optional[dict] = None) -> Tuple[int, str]:
        try:
            p = subprocess.run([self.binary, *args], capture_output=True, text=True, timeout=timeout,
                               env={**os.environ, **(env or {})})
        except FileNotFoundError:
            return 127, f"th-cli not found: {self.binary}"
        except subprocess.TimeoutExpired:
            return 124, f"timeout after {timeout}s"
        return p.returncode, strip(p.stdout + p.stderr)

    def json(self, args: List[str]) -> Any:
        rc, out = self.run(args)
        if rc != 0:
            return None
        starts = [i for i in (out.find("["), out.find("{")) if i >= 0]
        if not starts:
            return None
        try:
            return json.JSONDecoder().raw_decode(out[min(starts):])[0]
        except json.JSONDecodeError:
            return None

    # ---- test runner / executions / projects
    def runner_state(self) -> Optional[str]:
        """idle | loading | ready | running, or None if unreadable."""
        d = self.json(["test-runner-status", "--json"])
        return enum_name(d.get("state")) if isinstance(d, dict) else None

    def wait_state(self, want: str, timeout: int, stop: Optional[threading.Thread] = None) -> Tuple[bool, Optional[str]]:
        deadline, st = time.time() + timeout, None
        while time.time() < deadline:
            st = self.runner_state()
            if st == want:
                return True, st
            if stop is not None and not stop.is_alive():
                return False, st
            time.sleep(1)
        return False, st

    def exec_state(self, eid: int) -> Optional[str]:
        d = self.json(["test-run-execution", "list", "-i", str(eid), "--json"])
        row = d if isinstance(d, dict) and "state" in d else (items(d)[0] if items(d) else {})
        return enum_name(row.get("state"))

    def exec_set(self) -> set:
        return {e.get("id") for e in items(self.json(["test-run-execution", "list", "--json", "--all"]))}

    def last_execution(self) -> Optional[int]:
        ids = [e.get("id") for e in items(self.json(["test-run-execution", "list", "--json", "--all"]))
               if isinstance(e.get("id"), int)]
        return max(ids) if ids else None

    def projects(self) -> Optional[List[dict]]:
        data = self.json(["project", "list", "--json"])
        return None if data is None else items(data)

    def find_project(self, name: str) -> Optional[int]:
        for p in self.projects() or []:
            if p.get("name") == name:
                return p.get("id")
        return None
