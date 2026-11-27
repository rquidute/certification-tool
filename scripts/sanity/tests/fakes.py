"""Shared test doubles. Tests never touch a real th-cli, DUT app or backend."""
import json
import os
import sys
import tempfile
from typing import Callable, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from th_sanity import console  # noqa: E402
from th_sanity.session import Session  # noqa: E402
from th_sanity.thcli import ThCli  # noqa: E402

console.USE_COLOR = False


class FakeTh(ThCli):
    """ThCli whose `run` is scripted. The real json()/runner_state()/... logic still runs on top of it."""

    def __init__(self) -> None:
        super().__init__("th-cli")
        self.calls: List[List[str]] = []
        self.handler: Callable[[List[str]], Tuple[int, str]] = lambda args: (0, "")

    def run(self, args, timeout=120, env=None):
        self.calls.append(list(args))
        return self.handler(list(args))

    def called(self, *prefix: str) -> bool:
        return any(c[:len(prefix)] == list(prefix) for c in self.calls)


class FakePrompts:
    """Stands in for PromptDriver: returns canned (output, rc, reason) and records the arguments."""

    def __init__(self, result: Tuple[str, int, str] = ("Results: 1 passed\n", 0, "")) -> None:
        self.result = result
        self.calls: List[dict] = []

    def run(self, args, rules, timeout, idle=180, transcript=None, text_rules=None):
        self.calls.append({"args": args, "rules": rules, "timeout": timeout, "transcript": transcript})
        return self.result


class FakeDut:
    def __init__(self, error: Optional[str] = None) -> None:
        self.error, self.started, self.log_path, self.ready_how, self.alive = error, [], None, "fake", True
        self.last_command = ""

    def start(self, profile, fresh=True, log_dir=None):
        from th_sanity.dut import DutError
        if self.error:
            raise DutError(self.error)
        self.started.append((profile, fresh))
        self.last_command = f"/home/u/apps/{profile}-app --wifi"
        self.log_path = os.path.join(log_dir or "/tmp", "dut.log")

    def is_alive(self):
        return self.alive

    def stop(self):
        self.started.append(("stopped", None))


def manifest(**over) -> dict:
    m = {"th_root": "/nonexistent", "default_run_timeout_s": 30,
         "dut_profiles": {"d": {"binary": "fake-dut"}},
         "suites": [{"id": "a", "suite": "S1", "test": "T1", "dut": None, "expect": "pass"}],
         "excluded": [], "default_prompt_answers": [], "custom": []}
    m.update(over)
    return m


def make_session(th: Optional[FakeTh] = None, dut=None, prompts=None, dry_run=False, **over) -> Session:
    out = tempfile.mkdtemp(prefix="sanity-test-")
    return Session(manifest(**over), out, out, dry_run=dry_run, th=th or FakeTh(),
                   dut=dut or FakeDut(), prompts=prompts or FakePrompts())


def status_of(s: Session, sid: str) -> Optional[str]:
    return next((x["status"] for x in s.results.steps if x["id"] == sid), None)


def ids(s: Session, status: str) -> List[str]:
    return [x["id"] for x in s.results.steps if x["status"] == status]


def js(obj) -> str:
    return json.dumps(obj)
