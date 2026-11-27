#!/usr/bin/env python3
"""Release sanity harness for the Matter Test Harness (TH).

Runs on the TH host (needs th-cli, a running backend and the DUT apps in ~/apps).
Standard library only.

    python3 scripts/sanity/run_sanity.py --manifest scripts/sanity/manifest.json
    python3 scripts/sanity/run_sanity.py --dry-run     # coverage gate + read-only commands only

What it does, in order:
  1. CLI smoke: every th-cli command, against a throwaway project.
  2. Coverage gate: every live suite must be in the manifest (or explicitly excluded).
  3. One test per suite, DUT started in a known state (fresh or reuse credentials).
  4. Custom YAML + Python test: add, rescan, run, remove, rescan.
  5. test-run-execution commands on the executions produced above.
  6. Report (Markdown + JSON) and exit code: 0 = GO, 1 = NO-GO.

Design rules: a step is PASS only when it ran and matched its expectation. Anything not run
is NOT_RUN and makes the verdict NO-GO unless it is an explicit, reasoned exclusion.
An unknown interactive prompt fails the step; the harness never guesses an answer.
"""
import argparse
import datetime
import json
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dut import DutError, DutManager  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PASS, FAIL, NOT_RUN = "PASS", "FAIL", "NOT_RUN"
PROMPT_TRIGGER = "Please enter a number for an option above"
# Projects the harness creates: sanity-harness-<epoch>[-renamed], plus the older sanity-<epoch>[-renamed] form and
# whatever suffix `project import` appends to a copy. Anchored, and requires a 9+ digit epoch, so a project that
# merely starts with "sanity" (e.g. "sanity-checks") is never matched.
DEFAULT_PROJECT_REGEX = r"^sanity-(?:harness-)?\d{9,}(?:-renamed)?(?:[ _(\-].*)?$"

# ---- console styling (standard library only). Auto-off when not a terminal or when NO_COLOR is set.
USE_COLOR = False
UNICODE = (getattr(sys.stdout, "encoding", "") or "").lower().startswith("utf")
_CODES = {"bold": "1", "dim": "2", "inv": "7", "red": "31", "green": "32", "yellow": "33", "cyan": "36", "gray": "90"}
SYMBOL = {"PASS": "✔" if UNICODE else "+", "FAIL": "✘" if UNICODE else "x", "NOT_RUN": "○" if UNICODE else "-"}
LABEL = {"PASS": "PASS", "FAIL": "FAIL", "NOT_RUN": "SKIP"}
STATUS_COLOR = {"PASS": "green", "FAIL": "red", "NOT_RUN": "yellow"}


def paint(text: str, *styles: str) -> str:
    if not USE_COLOR or not styles:
        return text
    return "\033[" + ";".join(_CODES[s] for s in styles) + "m" + text + "\033[0m"


def bar(done: int, total: int, width: int = 12) -> str:
    filled = int(width * done / total) if total else 0
    full, empty = ("█", "░") if UNICODE else ("#", "-")
    return paint(full * filled, "green") + paint(empty * (width - filled), "gray")
# th-cli text prompts end with 'Enter value: ' or 'Enter value [default: x]: ' and no newline
TEXT_PROMPT = re.compile(r"Enter value(?: \[default: [^\]]*\])?: $")
BASE38 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-."


def qr_discriminator(qr: str) -> int:
    """12-bit discriminator inside a Matter QR payload (MT:...). Bit layout per the Matter spec:
    version 3, vendor 16, product 16, flow 2, rendezvous 8, discriminator 12, passcode 27 (LSB first)."""
    body, raw, i = qr[3:], bytearray(), 0
    while i < len(body):
        chunk = body[i:i + 5]
        i += len(chunk)
        value = sum(BASE38.index(c) * 38 ** k for k, c in enumerate(chunk))
        raw += value.to_bytes({5: 3, 4: 2, 2: 1}[len(chunk)], "little")
    return (int.from_bytes(raw, "little") >> (3 + 16 + 16 + 2 + 8)) & 0xFFF
# Report categories. Coverage is a pre-flight gate (is every suite accounted for?), not a test result, so it
# is counted on its own and never mixed into the test PASS numbers.
CATEGORY = {"cli": "CLI commands", "executions": "CLI commands", "suites": "Test suites",
            "custom": "Custom tests", "coverage": "Coverage gate", "cleanup": "Harness", "harness": "Harness"}
CATEGORY_ORDER = ["Test suites", "Custom tests", "CLI commands", "Coverage gate", "Harness"]


def strip(text: str) -> str:
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


class Harness:
    def __init__(self, manifest: dict, manifest_dir: str, out_dir: str, dry_run: bool):
        self.m = manifest
        self.th_cli = manifest.get("th_cli", "th-cli")
        self.th_root = os.path.expanduser(manifest.get("th_root", "~/certification-tool"))
        self.out_dir = out_dir
        self.dry_run = dry_run
        self.manifest_dir = manifest_dir
        self.steps: List[dict] = []
        self.t0 = time.time()
        self.phase_name = ""
        self.progress = ""
        self.dut = DutManager(manifest["dut_profiles"], manifest.get("apps_dir", "~/apps"), out_dir)
        self.project_id: Optional[int] = None
        self.extra_projects: List[int] = []
        self.exec_ids: Dict[str, int] = {}  # step id -> execution id
        self.exec_meta: Dict[str, dict] = {}  # step id -> manifest entry that produced it
        self.live: Dict[str, set] = {}  # suite id -> test ids
        os.makedirs(out_dir, exist_ok=True)

    # ---------- recording ----------
    def elapsed(self) -> str:
        s = int(time.time() - self.t0)
        return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"

    def tally(self) -> str:
        c = {k: sum(1 for s in self.steps if s["status"] == k) for k in (PASS, FAIL, NOT_RUN)}
        return "  ".join(paint(f"{SYMBOL[k]} {c[k]}", STATUS_COLOR[k] if c[k] else "gray") for k in (PASS, FAIL, NOT_RUN))

    def stamp(self) -> str:
        return paint(f"[{self.elapsed()}]", "gray")

    def phase(self, n: int, name: str) -> None:
        self.phase_name = f"phase {n}/5 {name}"
        print(f"\n{self.stamp()} " + paint(f"Phase {n}/5", "bold", "cyan") + f" {bar(n - 1, 5, 10)} "
              + paint(name, "bold") + "   " + self.tally(), flush=True)

    def record(self, sid: str, group: str, status: str, detail: str = "", dur: float = 0.0, log: str = "") -> str:
        self.steps.append({"id": sid, "group": group, "status": status, "detail": detail.strip()[-1500:],
                           "duration_s": round(dur, 1), "log": log})
        badge = paint(f"{SYMBOL[status]} {LABEL[status]}", "bold", STATUS_COLOR[status])
        took = paint(f" ({dur:.0f}s)", "gray") if dur >= 1 else ""
        lines = detail.strip().splitlines()
        first = lines[0][:100] if lines else ""
        print(f"{self.stamp()} {badge} {paint(group + '/', 'gray')}{paint(sid, 'bold')}{took} "
              f"{paint(first, 'dim') if status != FAIL else first}", flush=True)
        if status == FAIL:  # show why right away instead of only in the report
            for ln in lines[1:15]:
                print(paint("           │ " if UNICODE else "           | ", "red") + paint(ln[:140], "dim"), flush=True)
        return status

    # ---------- th-cli plumbing ----------
    def cli(self, args: List[str], timeout: int = 120, env: Optional[dict] = None) -> Tuple[int, str]:
        e = {**os.environ, **(env or {})}
        try:
            p = subprocess.run([self.th_cli, *args], capture_output=True, text=True, timeout=timeout, env=e)
        except FileNotFoundError:
            return 127, f"th-cli not found: {self.th_cli}"
        except subprocess.TimeoutExpired:
            return 124, f"timeout after {timeout}s"
        return p.returncode, strip(p.stdout + p.stderr)

    def cli_step(self, sid: str, group: str, args: List[str], ok_rc=(0,), env=None, must_contain: str = "",
                 timeout: int = 120) -> Tuple[bool, str]:
        t0 = time.time()
        rc, out = self.cli(args, timeout=timeout, env=env)
        good = rc in ok_rc and (must_contain in out)
        why = "" if good else f"rc={rc}" + (f", missing {must_contain!r}" if must_contain not in out else "")
        self.record(sid, group, PASS if good else FAIL, why + "\n" + out[-400:] if not good else "th-cli " + " ".join(args),
                    time.time() - t0)
        return good, out

    def cli_json(self, args: List[str]) -> Any:
        rc, out = self.cli(args)
        if rc != 0:
            return None
        starts = [i for i in (out.find("["), out.find("{")) if i >= 0]
        if not starts:
            return None
        try:
            return json.JSONDecoder().raw_decode(out[min(starts):])[0]
        except json.JSONDecodeError:
            return None

    def runner_state(self) -> Optional[str]:
        """idle | loading | ready | running (test-runner-status --json), None if unreadable."""
        d = self.cli_json(["test-runner-status", "--json"])
        return str(d.get("state")).lower() if isinstance(d, dict) and d.get("state") is not None else None

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
        d = self.cli_json(["test-run-execution", "list", "-i", str(eid), "--json"])
        row = d if isinstance(d, dict) and "state" in d else (items(d)[0] if items(d) else {})
        return str(row.get("state")).lower() if row.get("state") is not None else None

    # ---------- 0. pre-flight cleanup ----------
    def stale_projects(self) -> Optional[List[dict]]:
        cfg = self.m.get("cleanup", {})
        rx = re.compile(cfg.get("project_name_regex", DEFAULT_PROJECT_REGEX))
        data = self.cli_json(["project", "list", "--json"])
        if data is None:
            return None
        return [p for p in items(data) if isinstance(p.get("name"), str) and p.get("id") is not None and rx.match(p["name"])]

    def preclean(self) -> None:
        """Remove projects left behind by earlier sanity sessions (crash, Ctrl-C). Only names matching the
        harness's own pattern are touched; a user's projects are never deleted. Nothing is deleted in a dry run."""
        g = "cleanup"
        print(f"\n{self.stamp()} " + paint("Pre-flight", "bold", "cyan") + " " + paint("stale sanity projects", "bold"), flush=True)
        stale = self.stale_projects()
        if stale is None:
            self.record("preclean-list-projects", g, FAIL, "could not read `project list --json`")
            return
        if not stale:
            self.record("preclean-projects", g, PASS, "no stale sanity projects")
            return
        names = ", ".join(f"{p['id']}:{p['name']}" for p in stale)
        limit = self.m.get("cleanup", {}).get("max_delete", 25)
        if len(stale) > limit:
            self.record("preclean-projects", g, FAIL, f"{len(stale)} projects match the cleanup pattern (limit {limit}); "
                        f"refusing to delete them. Check cleanup.project_name_regex. Matches: {names}")
            return
        if self.dry_run:
            self.record("preclean-projects", g, PASS, f"dry run: would delete {len(stale)}: {names}")
            return
        for p in stale:
            self.cli_step(f"preclean-project-{p['id']}", g, ["project", "delete", "-i", str(p["id"]), "-y"])

    # ---------- 1. CLI smoke ----------
    def smoke(self) -> None:
        g = "cli"
        self.cli_step("version", g, ["--version"])
        self.cli_step("test-runner-status", g, ["test-runner-status", "--json"])
        ok, _ = self.cli_step("available-tests-json", g, ["available-tests", "--json"])
        self.cli_step("available-tests-compact", g, ["available-tests", "--compact"])
        self.cli_step("available-tests-group-by-cluster", g, ["available-tests", "--group-by-cluster"])
        self.cli_step("available-tests-cluster", g, ["available-tests", "--cluster", "OnOff"])
        self.cli_step("project-list", g, ["project", "list", "--json"])
        if self.dry_run:
            return
        name = f"sanity-harness-{int(time.time())}"
        good, _ = self.cli_step("project-create", g, ["project", "create", "-n", name])
        if good:
            self.project_id = self.find_project(name)
        if self.project_id is None:
            self.record("project-id-lookup", g, FAIL, f"could not find project {name!r} in `project list --json`")
            return
        pid = str(self.project_id)
        self.cli_step("project-update", g, ["project", "update", "-i", pid, "-n", name + "-renamed"])
        self.cli_step("project-edit", g, ["project", "edit", "-i", pid], env={"EDITOR": "true", "VISUAL": "true"})
        self.cli_step("project-applicable-tests", g, ["project", "applicable-tests", "-i", pid, "--json"])
        exp = os.path.join(self.out_dir, "project_export.json")
        if self.cli_step("project-export", g, ["project", "export", "-i", pid, "-o", exp])[0]:
            before = {p.get("id") for p in items(self.cli_json(["project", "list", "--json"]))}
            if self.cli_step("project-import", g, ["project", "import", "-f", exp])[0]:
                after = {p.get("id") for p in items(self.cli_json(["project", "list", "--json"]))}
                self.extra_projects += sorted(x for x in after - before if x is not None)
        self.cli_step("rescan-tests", g, ["rescan-tests"])
        # Idle engine: abort must be refused (409 "not active") and the engine must stay idle. The state is
        # checked first so this step can never abort a run that somebody else started.
        st = self.runner_state()
        if st != "idle":
            self.record("runner-idle-before-abort", g, FAIL, f"runner state is {st!r}, not 'idle': a test run is active, "
                        "finish or abort it before running the sanity harness")
        else:
            self.record("runner-idle-before-abort", g, PASS, "test-runner-status: idle")
            self.cli_step("abort-testing-idle-refused", g, ["abort-testing"], ok_rc=(1,), must_contain="not active")
            after = self.runner_state()
            self.record("runner-still-idle-after-abort", g, PASS if after == "idle" else FAIL, f"state after refused abort: {after!r}")

    def find_project(self, name: str) -> Optional[int]:
        for p in items(self.cli_json(["project", "list", "--json"])):
            if p.get("name") == name:
                return p.get("id")
        return None

    # ---------- 2. coverage gate ----------
    def load_live(self) -> bool:
        data = self.cli_json(["available-tests", "--json"])
        if data is None:
            self.record("load-live-suites", "coverage", FAIL, "could not read `available-tests --json`")
            return False
        self.live = {}

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                tcs = node.get("test_cases")
                if isinstance(tcs, (dict, list)):
                    # Real shape: {"metadata": {"public_id": ...}, "test_cases": {<public_id>: {"metadata": ...}}}
                    sid = str((node.get("metadata") or {}).get("public_id") or node.get("public_id") or node.get("name"))
                    tests = set()
                    for key, tc in (tcs.items() if isinstance(tcs, dict) else enumerate(tcs)):
                        pid = (tc.get("metadata") or {}).get("public_id") or tc.get("public_id")
                        tests.add(str(pid or (key if isinstance(key, str) else "")))
                    tests.discard("")
                    self.live.setdefault(sid, set()).update(tests)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(data)
        if not self.live:
            self.record("load-live-suites", "coverage", FAIL, "no suites found in available-tests output; JSON shape changed?")
            return False
        return True

    def coverage_gate(self) -> None:
        g = "coverage"
        if not self.load_live():
            return
        listed = {s["suite"] for s in self.m["suites"]}
        excluded = {e["suite"]: e["reason"] for e in self.m.get("excluded", [])}
        for s in self.m["suites"]:
            if "TODO" in (s["suite"], s["test"]):
                self.record(f"manifest-{s['id']}", g, FAIL, "manifest entry still has TODO; fill suite/test from available-tests --json")
            elif s["suite"] not in self.live:
                self.record(f"manifest-{s['id']}", g, FAIL, f"suite {s['suite']!r} not found in TH")
            elif s["test"] not in self.live[s["suite"]]:
                self.record(f"manifest-{s['id']}", g, FAIL, f"test {s['test']!r} not in suite {s['suite']!r}")
            else:
                self.record(f"manifest-{s['id']}", g, PASS, f"{s['suite']} / {s['test']}")
        uncovered = sorted(set(self.live) - listed - set(excluded))
        # '<suite>-custom' suites hold the user's side-loaded files and come and go with them. The custom
        # step below tests its own fixtures; user files are reported, never silently counted as covered.
        for u in [x for x in uncovered if x.endswith("-custom")]:
            uncovered.remove(u)
            self.record(f"excluded-{u}", g, NOT_RUN, f"user custom suite ({len(self.live[u])} tests); only the harness's own fixtures are tested")
        for u in uncovered:
            self.record(f"uncovered-{u}", g, FAIL, f"suite {u!r} exists in the TH ({len(self.live[u])} tests) but is neither in the manifest nor excluded")
        for suite, reason in excluded.items():
            self.record(f"excluded-{suite}", g, NOT_RUN, f"explicitly excluded: {reason}")
        self.record("live-suite-summary", g, PASS, f"{len(self.live)} live suites, {len(listed)} covered, {len(excluded)} excluded")

    # ---------- 3. run one test through run-tests (pty, prompt-aware) ----------
    def resolve_text(self, value: str) -> Optional[str]:
        """Text answers may be literals or tokens: @dut_qr = the QR code the DUT printed at startup,
        @qr_disc = the discriminator (hex) inside that QR code."""
        if value == "@dut_qr":
            try:
                with open(self.dut.log_path or "", errors="replace") as f:
                    m = re.search(r"SetupQRCode: \[(MT:[^\]]+)\]", f.read())
            except OSError:
                m = None
            self.last_qr = m.group(1) if m else None
            return self.last_qr
        if value == "@qr_disc":
            return hex(qr_discriminator(self.last_qr)) if getattr(self, "last_qr", None) else None
        return value

    def answer_for(self, block: str, rules: List[dict]) -> Optional[str]:
        opts = re.findall(r"(?m)^\s*(\d+)\s*[:=\-\)]?\s+(.+?)\s*$", block)
        for rule in rules:
            if re.search(rule["match"], block):
                for num, label in opts:
                    if re.search(rule["choose"], label.strip()):
                        return num
        return None

    def run_test(self, sid: str, group: str, test: str, expect: str, answers: List[dict], dut: Optional[str],
                 dut_start: str, config: Optional[str] = None, timeout: Optional[int] = None,
                 text_answers: Optional[List[dict]] = None) -> Optional[int]:
        t0 = time.time()
        timeout = timeout or self.m.get("default_run_timeout_s", 900)
        if dut:
            print(paint(f"      … starting DUT '{dut}' ({dut_start})" if UNICODE else f"      ... starting DUT '{dut}' ({dut_start})", "gray"), flush=True)
            try:
                self.dut.start(dut, fresh=(dut_start != "reuse"))
            except DutError as e:
                self.record(sid, group, FAIL, f"INFRA: DUT did not start: {e}")
                return None
            print(paint(f"      … DUT ready (log: {self.dut.log_path})" if UNICODE else f"      ... DUT ready (log: {self.dut.log_path})", "gray"), flush=True)
        print(paint(f"      … running {test} (timeout {timeout}s, transcript: {self.out_dir}/{sid}.out)" if UNICODE
                    else f"      ... running {test} (timeout {timeout}s, transcript: {self.out_dir}/{sid}.out)", "gray"), flush=True)
        args = [self.th_cli, "run-tests", "-t", test, "-n", f"sanity-{sid}", "--no-color", "--no-streaming"]
        if self.project_id:
            args += ["--project-id", str(self.project_id)]
        if config:
            args += ["-c", config]
        out, rc, why = self.pty_run(args, answers + self.m.get("default_prompt_answers", []), timeout,
                                    transcript=os.path.join(self.out_dir, f"{sid}.out"), text_rules=text_answers)
        log = (re.findall(r"Log output in:?\s*(\S+)", out) or [""])[-1]
        results = (re.findall(r"(?m)^Results:\s*(.+)$", out) or [""])[-1]
        if dut and not self.dut.is_alive():
            why = why or "DUT process died during the run"
        if why:
            extra = "" if why.startswith("UNHANDLED") else "\n" + out[-400:]
            self.record(sid, group, FAIL, f"INFRA: {why}{extra}", time.time() - t0, log)
            return self.last_execution()
        if not results and rc != 0:
            self.record(sid, group, FAIL, f"INFRA: rc={rc}, no Results line\n{out[-400:]}", time.time() - t0, log)
            return self.last_execution()
        actual = "pass" if rc == 0 else "fail"
        good = actual == expect
        self.record(sid, group, PASS if good else FAIL,
                    f"{test}: {results or 'no results line'} (expected {expect}, got {actual})", time.time() - t0, log)
        return self.last_execution()

    @staticmethod
    def file_tail(path: str) -> str:
        try:
            with open(path, "rb") as f:
                f.seek(max(0, os.path.getsize(path) - 4096))
                lines = [ln.strip() for ln in f.read().decode(errors="replace").splitlines() if ln.strip()]
            return lines[-1] if lines else ""
        except OSError:
            return ""

    def pty_run(self, args: List[str], rules: List[dict], timeout: int, idle: int = 180,
                transcript: Optional[str] = None, text_rules: Optional[List[dict]] = None) -> Tuple[str, int, str]:
        """Run th-cli in a pty, answering known prompts. th-cli prints little while a test runs (progress goes
        to its own log file), so growth of that log file counts as activity for the idle check."""
        master, slave = pty.openpty()
        proc = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        tfile = open(transcript, "w") if transcript else None
        buf, since_answer = "", 0
        start = last = beat = time.time()
        why, clean = "", False
        cli_log, cli_log_size = None, -1
        try:
            while True:
                r, _, _ = select.select([master], [], [], 1.0)
                eof = False
                if r:
                    try:
                        raw = os.read(master, 65536)
                    except OSError:  # Linux: EIO once th-cli has exited and closed its end of the pty
                        raw = b""
                    # select() keeps reporting an exited pty as readable, so EOF must end the loop explicitly
                    eof = raw == b""
                    chunk = strip(raw.decode(errors="replace"))
                    if chunk:
                        buf += chunk
                        last = time.time()
                        if tfile:
                            tfile.write(chunk)
                            tfile.flush()
                        if cli_log is None:
                            m = re.search(r"Log output in:?\s*(\S+)", buf)
                            cli_log = m.group(1) if m else None
                        if TEXT_PROMPT.search(buf[since_answer:]):
                            block = buf[since_answer:]
                            ans = next((self.resolve_text(r_["value"]) for r_ in (text_rules or [])
                                        if re.search(r_["match"], block)), None)
                            if ans is None:
                                why = "UNHANDLED TEXT PROMPT (no text_answers rule matched, or its value could " \
                                      "not be resolved). The prompt was:\n" + "\n".join(block.splitlines()[-8:])
                                break
                            os.write(master, (ans + "\n").encode())
                            since_answer = len(buf)
                        elif PROMPT_TRIGGER in buf[since_answer:]:
                            block = buf[since_answer:]
                            num = self.answer_for(block, rules)
                            if num is None:
                                upto = block[:block.index(PROMPT_TRIGGER) + len(PROMPT_TRIGGER)]
                                why = "UNHANDLED PROMPT (no answer rule matched). The prompt was:\n" + \
                                      "\n".join(upto.splitlines()[-12:])
                                break
                            os.write(master, (num + "\n").encode())
                            since_answer = len(buf)
                if cli_log and os.path.exists(cli_log):
                    size = os.path.getsize(cli_log)
                    if size != cli_log_size:
                        cli_log_size, last = size, time.time()
                if eof or (proc.poll() is not None and not r):
                    break
                if time.time() - start > timeout:
                    why = f"timeout after {timeout}s"
                    break
                if time.time() - last > idle:
                    why = f"no th-cli output and no log growth for {idle}s (waiting on an unknown prompt?)"
                    break
                if time.time() - beat > 30:
                    beat = time.time()
                    tail = (cli_log and self.file_tail(cli_log)) or next(
                        (ln.strip() for ln in reversed(buf.splitlines()) if ln.strip()), "(no output yet)")
                    print(paint(f"      [{self.elapsed()}] {self.progress} | step {int(beat - start)}s/{timeout}s | "
                                f"{tail[:100]}", "gray"), flush=True)
            clean = not why
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            os.close(master)
            if tfile:
                tfile.close()
            if not clean:
                # th-cli is gone but the backend keeps executing the test. Abort it so the next step does
                # not fail with "Test Engine is busy".
                self.cli(["abort-testing"], timeout=60)
        return buf, proc.returncode if proc.returncode is not None else -1, why

    def last_execution(self) -> Optional[int]:
        ids = [e.get("id") for e in items(self.cli_json(["test-run-execution", "list", "--json", "--all"]))
               if isinstance(e.get("id"), int)]
        return max(ids) if ids else None

    # ---------- 4. suites ----------
    def run_suites(self) -> None:
        done: Dict[str, str] = {}
        total = len(self.m["suites"])
        durations: List[float] = []
        t_prev = time.time()
        for i, s in enumerate(self.m["suites"], 1):
            sid = s["id"]
            self.progress = f"suite {i}/{total}"
            if i > 1 and time.time() - t_prev > 5:  # skipped steps are instant and would skew the average
                durations.append(time.time() - t_prev)
            t_prev = time.time()
            if durations:
                eta = sum(durations) / len(durations) * (total - i + 1)
                print(f"{self.stamp()} {paint(f'suite {i}/{total}', 'bold', 'cyan')} {bar(i - 1, total)} {paint(sid, 'bold')} "
                      + paint(f"(avg {sum(durations) / len(durations):.0f}s/suite, ~{int(eta // 60)}m left, estimate only)", "gray"), flush=True)
            else:
                print(f"{self.stamp()} {paint(f'suite {i}/{total}', 'bold', 'cyan')} {bar(i - 1, total)} {paint(sid, 'bold')}", flush=True)
            dep = s.get("depends_on")
            if dep and done.get(dep) != PASS:
                self.record(f"run-{sid}", "suites", NOT_RUN, f"depends on {dep}, which did not pass")
                continue
            if "TODO" in (s["suite"], s["test"]):
                self.record(f"run-{sid}", "suites", NOT_RUN, "manifest entry has TODO")
                continue
            if self.dry_run:
                self.record(f"run-{sid}", "suites", NOT_RUN, "dry-run")
                continue
            n = len(self.steps)
            eid = self.run_test(f"run-{sid}", "suites", s["test"], s.get("expect", "pass"),
                                s.get("prompt_answers", []), s.get("dut"), s.get("dut_start", "fresh"),
                                config=self.abs_path(s.get("config")), text_answers=s.get("text_answers"))
            done[sid] = self.steps[-1]["status"] if len(self.steps) > n else FAIL
            if eid and done[sid] == PASS:
                self.exec_ids[sid] = eid
                self.exec_meta[sid] = s

    def abs_path(self, p: Optional[str]) -> Optional[str]:
        return os.path.join(self.manifest_dir, p) if p and not os.path.isabs(p) else p

    def find_custom(self, base: str) -> Optional[Tuple[str, str]]:
        """(suite, test id) of a side-loaded variant of `base`. Python overrides get a '-custom' id suffix;
        custom YAML tests land in a '<suite>-custom' suite, possibly with the same id as the SDK test."""
        norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())  # noqa: E731
        for suite, tests in self.live.items():
            for t in tests:
                if norm(base) in norm(t) and ("custom" in t.lower() or suite.lower().endswith("-custom")):
                    return suite, t
        return None

    def find_custom_id(self, base: str) -> Optional[str]:
        """Side-loaded tests that override an SDK test get a '-custom' marker in their public_id.
        The exact spelling is not guaranteed, so match on normalised text instead of an exact id."""
        norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())  # noqa: E731
        for tests in self.live.values():
            for t in tests:
                if "custom" in t.lower() and norm(base) in norm(t):
                    return t
        return None

    # ---------- 5. custom tests ----------
    def custom(self) -> None:
        for kind, c in self.m.get("custom", {}).items():
            self.progress = f"custom {kind}"
            g = "custom"
            src = c["src"].format(th_root=self.th_root)
            dest_dir = c["dest_dir"].format(th_root=self.th_root)
            dest = os.path.join(dest_dir, os.path.basename(src))
            if self.dry_run:
                self.record(f"custom-{kind}", g, NOT_RUN, "dry-run")
                continue
            if os.path.exists(dest):
                self.record(f"custom-{kind}", g, NOT_RUN, f"{dest} already exists; refusing to overwrite or delete a user file")
                continue
            if not os.path.isfile(src):
                self.record(f"custom-{kind}", g, FAIL, f"fixture source missing: {src}")
                continue
            try:
                shutil.copyfile(src, dest)
                _, rescan_out = self.cli_step(f"custom-{kind}-rescan-add", g, ["rescan-tests"])
                self.load_live()
                hit = self.find_custom(c["test"])
                cid = hit[1] if hit else None
                if hit:
                    self.record(f"custom-{kind}-visible", g, PASS, f"{hit[1]} in suite {hit[0]!r}")
                else:
                    cust = {s: sorted(t)[:5] for s, t in self.live.items() if s.lower().endswith("-custom")}
                    self.record(f"custom-{kind}-visible", g, FAIL,
                                f"no custom variant of {c['test']} after rescan\ncustom suites now: {cust}\n"
                                f"rescan said: {rescan_out.strip()[-500:]}")
                if hit and "custom" not in hit[1].lower():
                    # Same id as the SDK test: `th-cli run-tests -t <id>` cannot say which one to run, so a
                    # PASS here could silently be the SDK test. Refuse rather than report a false pass.
                    self.record(f"custom-{kind}-run", g, NOT_RUN,
                                f"custom id {hit[1]!r} is identical to the SDK test id; th-cli cannot select the "
                                f"custom variant unambiguously")
                elif cid:
                    eid = self.run_test(f"custom-{kind}-run", g, cid, "pass", [], c.get("dut"), "fresh")
                    if eid:
                        self.exec_ids[f"custom-{kind}"] = eid
            finally:
                if os.path.exists(dest):
                    os.remove(dest)
                self.cli_step(f"custom-{kind}-rescan-remove", g, ["rescan-tests"])
                self.load_live()
                left = self.find_custom(c["test"])
                left = left[1] if left else None
                self.record(f"custom-{kind}-removed", g, PASS if not left else FAIL,
                            "custom variant gone after removal + rescan" if not left else f"{left} STILL listed after removal + rescan")

    # ---------- 6. test-run-execution commands ----------
    def executions(self) -> None:
        g = "executions"
        if self.dry_run:
            self.cli_step("execution-list", g, ["test-run-execution", "list", "--json"])
            return
        target = next((k for k in self.exec_ids if k in self.exec_meta), None)
        eid = self.exec_ids[target] if target else next(iter(self.exec_ids.values()), None)
        if not eid:
            self.record("executions", g, NOT_RUN, "no successful execution to operate on")
            return
        i = str(eid)
        d = self.out_dir
        self.cli_step("execution-list", g, ["test-run-execution", "list", "--json"])
        self.cli_step("execution-show", g, ["test-run-execution", "list", "-i", i, "--json"])
        self.cli_step("execution-log", g, ["test-run-execution", "log", "-i", i, "-o", os.path.join(d, "exec_log.txt")])
        rc, out = self.cli(["test-run-execution", "pics-export", "-i", i, "-o", os.path.join(d, "exec_pics")])
        if rc == 0:
            self.record("execution-pics-export", g, PASS, "exported")
        elif "No PICS were used" in out:  # a 404 with that exact reason is the documented answer for PICS-less runs
            self.record("execution-pics-export", g, PASS, "endpoint answered 'No PICS were used' (expected for these runs); the export path itself is NOT exercised")
        else:
            self.record("execution-pics-export", g, FAIL, f"rc={rc}\n{out[-400:]}")
        if self.project_id:  # needs executions in the project, so it cannot run in the smoke phase
            self.cli_step("project-logs", g, ["project", "logs", "-i", str(self.project_id), "-o", os.path.join(d, "project_logs")])
        self.cli_step("execution-rename", g, ["test-run-execution", "rename", "-i", i, "-n", "sanity-renamed"])
        self.cli_step("execution-archive", g, ["test-run-execution", "archive", "-i", i])
        self.cli_step("execution-unarchive", g, ["test-run-execution", "unarchive", "-i", i])
        exp = os.path.join(d, "exec_export.json")
        if self.cli_step("execution-export", g, ["test-run-execution", "export", "-i", i, "-o", exp])[0]:
            imp = ["test-run-execution", "import", "-f", exp] + (["-p", str(self.project_id)] if self.project_id else [])
            before = self.exec_set()
            if self.cli_step("execution-import", g, imp)[0]:
                for x in sorted(self.exec_set() - before):
                    self.cli_step(f"execution-delete-imported-{x}", g, ["test-run-execution", "delete", "-i", str(x), "-y"])
        # repeat re-runs the execution, so the DUT must be in the state the original run had
        if target:
            meta = self.exec_meta[target]
            t0 = time.time()
            before = self.exec_set()
            ready = True
            if meta.get("dut"):
                try:
                    self.dut.start(meta["dut"], fresh=True)
                except DutError as e:
                    ready = False
                    self.record("execution-repeat", g, FAIL, f"INFRA: DUT did not start: {e}")
            if ready:
                orig = self.exec_ids[target]
                orig_state = self.exec_state(orig)
                print(paint(f"      … repeating execution {orig} ({target}, state {orig_state})", "gray"), flush=True)
                args = [self.th_cli, "test-run-execution", "repeat", "-i", str(orig), "--no-color", "--no-streaming"]
                out, rc, why = self.pty_run(args, meta.get("prompt_answers", []) + self.m.get("default_prompt_answers", []),
                                            self.m.get("default_run_timeout_s", 900),
                                            transcript=os.path.join(d, "execution-repeat.out"), text_rules=meta.get("text_answers"))
                new_ids = sorted(self.exec_set() - before)
                new_state = self.exec_state(new_ids[-1]) if new_ids else None
                if why:
                    self.record("execution-repeat", g, FAIL, f"INFRA: {why}", time.time() - t0)
                elif not new_ids:
                    self.record("execution-repeat", g, FAIL, f"rc={rc}, but no new execution was created\n{out[-300:]}", time.time() - t0)
                elif new_state != orig_state:
                    self.record("execution-repeat", g, FAIL,
                                f"execution {new_ids[-1]} ended {new_state!r}, original {orig} was {orig_state!r}", time.time() - t0)
                else:
                    self.record("execution-repeat", g, PASS,
                                f"execution {orig} repeated as {new_ids[-1]}, both {new_state!r}", time.time() - t0)
            for x in sorted(self.exec_set() - before):
                self.cli_step(f"execution-delete-repeated-{x}", g, ["test-run-execution", "delete", "-i", str(x), "-y"])
        else:
            self.record("execution-repeat", g, NOT_RUN, "no passed suite execution to repeat")

    def abort_running(self) -> None:
        g = "cli"
        cfg = self.m.get("abort_run")
        if not cfg:
            return
        meta = next((s for s in self.m["suites"] if s["id"] == cfg["from"]), None)
        if self.dry_run:
            self.record("abort-testing-running", g, NOT_RUN, "dry-run")
            return
        if not meta or "TODO" in (meta["suite"], meta["test"]):
            self.record("abort-testing-running", g, NOT_RUN, f"manifest entry {cfg['from']!r} missing or has TODO")
            return
        self.progress = "abort while running"
        if meta.get("dut"):
            try:
                print(paint(f"      … starting DUT '{meta['dut']}' (fresh)", "gray"), flush=True)
                self.dut.start(meta["dut"], fresh=True)
            except DutError as e:
                self.record("abort-testing-running", g, FAIL, f"INFRA: DUT did not start: {e}")
                return
        before = self.last_execution() or 0
        args = [self.th_cli, "run-tests", "-t", meta["test"], "-n", "sanity-abort-while-running", "--no-color", "--no-streaming"]
        if self.project_id:
            args += ["--project-id", str(self.project_id)]
        result: Dict[str, Any] = {}

        def worker() -> None:
            result["out"], result["rc"], result["why"] = self.pty_run(
                args, meta.get("prompt_answers", []) + self.m.get("default_prompt_answers", []),
                self.m.get("default_run_timeout_s", 900),
                transcript=os.path.join(self.out_dir, "abort-while-running.out"), text_rules=meta.get("text_answers"))

        t0 = time.time()
        th = threading.Thread(target=worker, daemon=True)
        th.start()
        running, st = self.wait_state("running", 90, stop=th)
        if not running:
            self.record("abort-testing-running", g, FAIL,
                        f"runner never reached 'running' (last state {st!r}); the run "
                        f"{'ended first' if not th.is_alive() else 'is stuck'}: {str(result.get('why') or result.get('out', ''))[-300:]}",
                        time.time() - t0)
            th.join(timeout=5)
            return
        time.sleep(cfg.get("after_s", 8))  # let the test get properly under way
        if not th.is_alive():
            self.record("abort-testing-running", g, FAIL,
                        f"test finished before the abort was sent; pick a longer test in abort_run.from "
                        f"(after_s={cfg.get('after_s', 8)})", time.time() - t0)
            return
        rc, out = self.cli(["abort-testing"], timeout=60)
        ok = rc == 0
        self.record("abort-testing-running", g, PASS if ok else FAIL,
                    f"abort accepted: {out.strip()[-100:]}" if ok else f"rc={rc}\n{out.strip()[-300:]}", time.time() - t0)
        th.join(timeout=120)
        self.record("run-ends-after-abort", g, PASS if not th.is_alive() else FAIL,
                    "th-cli exited after the abort" if not th.is_alive() else "th-cli still running 120s after the abort")
        idle, st = self.wait_state("idle", 60)
        self.record("runner-idle-after-abort", g, PASS if idle else FAIL,
                    "runner back to idle" if idle else f"runner still {st!r} 60s after the abort")
        eid = self.last_execution()
        if eid and eid > before:
            state = self.exec_state(eid)
            self.record("execution-cancelled-after-abort", g, PASS if state and "cancel" in state else FAIL,
                        f"execution {eid} state: {state!r} (expected 'cancelled')")
        else:
            self.record("execution-cancelled-after-abort", g, FAIL, "no new execution was recorded for the aborted run")

    def exec_set(self) -> set:
        return {e.get("id") for e in items(self.cli_json(["test-run-execution", "list", "--json", "--all"]))}

    # ---------- cleanup + report ----------
    def cleanup(self) -> None:
        """Best effort. A second Ctrl-C must not abandon the remaining cleanup steps."""
        def guarded(label: str, fn) -> None:
            try:
                fn()
            except KeyboardInterrupt:
                print(f"      cleanup interrupted at: {label}. Check leftovers (th-cli project list, ps for the DUT).", flush=True)
        guarded("abort running test", lambda: self.cli(["abort-testing"], timeout=60))
        guarded("stop DUT", self.dut.stop)
        for pid in [*self.extra_projects, self.project_id]:
            if pid is not None:
                guarded(f"delete project {pid}", lambda pid=pid: self.cli_step(
                    f"cleanup-project-{pid}", "cleanup", ["project", "delete", "-i", str(pid), "-y"]))

    def by_category(self) -> Dict[str, List[dict]]:
        cats: Dict[str, List[dict]] = {c: [] for c in CATEGORY_ORDER}
        for s in self.steps:
            cats.setdefault(CATEGORY.get(s["group"], "Harness"), []).append(s)
        return {c: v for c, v in cats.items() if v}

    @staticmethod
    def cat_counts(steps: List[dict]) -> str:
        n = {k: sum(1 for s in steps if s["status"] == k) for k in (PASS, FAIL, NOT_RUN)}
        return f"PASS {n[PASS]} / FAIL {n[FAIL]} / NOT_RUN {n[NOT_RUN]}"

    def report(self, version: str) -> int:
        fails = [s for s in self.steps if s["status"] == FAIL]
        unexpected = [s for s in self.steps if s["status"] == NOT_RUN and not s["id"].startswith("excluded-")]
        go = not fails and not unexpected
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        base = os.path.join(self.out_dir, f"sanity_{stamp}")
        counts = {k: sum(1 for s in self.steps if s["status"] == k) for k in (PASS, FAIL, NOT_RUN)}
        cats = self.by_category()
        lines = [f"# TH sanity report - {version}", "", f"- Date: {stamp}", f"- Total time: {self.elapsed()}",
                 f"- Mode: {'DRY RUN' if self.dry_run else 'FULL'}", f"- **Verdict: {'GO' if go else 'NO-GO'}**",
                 f"- Live suites: {len(self.live)}", "", "## Summary by category", "",
                 "| Category | PASS | FAIL | NOT_RUN |", "|---|---|---|---|"]
        for c, st in cats.items():
            n = {k: sum(1 for s in st if s["status"] == k) for k in (PASS, FAIL, NOT_RUN)}
            lines.append(f"| {c} | {n[PASS]} | {n[FAIL]} | {n[NOT_RUN]} |")
        for c, st in cats.items():
            lines += ["", f"## {c}", "", "| Step | Status | Time s | Detail |", "|---|---|---|---|"]
            for s in st:
                det = s["detail"].splitlines()[0][:140] if s["detail"] else ""
                lines.append(f"| {s['id']} | {s['status']} | {s['duration_s']} | {det.replace('|', '/')} |")
        if fails or unexpected:
            lines += ["", "## Needs attention"]
            for s in fails + unexpected:
                lines += [f"### {s['group']}/{s['id']} ({s['status']})", "```", s["detail"], "```", f"log: {s['log']}" if s["log"] else ""]
        with open(base + ".md", "w") as f:
            f.write("\n".join(lines) + "\n")
        with open(base + ".json", "w") as f:
            json.dump({"version": version, "verdict": "GO" if go else "NO-GO", "counts": counts, "steps": self.steps}, f, indent=2)
        rule = paint(("─" if UNICODE else "-") * 60, "gray")
        print(f"\n{rule}\n" + paint(f" Summary  ·  {self.elapsed()}", "bold"))
        print(paint(f" {'':<15}{'PASS':>7}{'FAIL':>7}{'SKIP':>7}", "gray"))
        for c, st in cats.items():
            n = {k: sum(1 for s in st if s["status"] == k) for k in (PASS, FAIL, NOT_RUN)}
            cells = "".join(paint(f"{n[k]:>7}", STATUS_COLOR[k] if n[k] else "gray") for k in (PASS, FAIL, NOT_RUN))
            print(f" {c:<15}" + cells)
        print(rule)
        if fails or unexpected:
            print(paint(" Needs attention:", "bold"))
            for s in fails + unexpected:
                why = (s["detail"].splitlines() or [""])[0][:90]
                print(f"  {paint(SYMBOL[s['status']], STATUS_COLOR[s['status']])} {s['group']}/{s['id']}  {paint(why, 'dim')}")
            print(rule)
        print(" " + (paint("  GO  ", "bold", "inv", "green") if go else paint("  NO-GO  ", "bold", "inv", "red")))
        print(f"Report: {base}.md")
        return 0 if go else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--manifest", default=os.path.join(here, "manifest.json"))
    ap.add_argument("--out-dir", default="sanity_reports")
    ap.add_argument("--dry-run", action="store_true", help="coverage gate and read-only CLI commands only")
    ap.add_argument("--version-label", default="", help="TH version string for the report header")
    ap.add_argument("--no-preclean", action="store_true", help="do not delete leftover sanity projects from earlier sessions")
    ap.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                    help="colored output (auto = only on a terminal and when NO_COLOR is not set)")
    ap.add_argument("--list-suites", action="store_true", help="print live suites (public_id, size, sample tests) and exit")
    a = ap.parse_args()
    global USE_COLOR
    USE_COLOR = a.color == "always" or (a.color == "auto" and sys.stdout.isatty() and "NO_COLOR" not in os.environ)
    with open(a.manifest) as f:
        manifest = json.load(f)
    h = Harness(manifest, os.path.dirname(os.path.abspath(a.manifest)), a.out_dir, a.dry_run)
    if a.list_suites:
        if not h.load_live():
            print(h.steps[-1]["detail"])
            return 1
        for sid, tests in sorted(h.live.items()):
            print(f"{sid!r}: {len(tests)} tests, e.g. {sorted(tests)[:3]}")
        return 0
    version = a.version_label or (h.cli(["--version"])[1].strip().replace("\n", " ")[:200])
    try:
        if not a.no_preclean:
            h.preclean()
        h.phase(1, "CLI commands")
        h.smoke()
        h.phase(2, "Coverage gate")
        h.coverage_gate()
        h.phase(3, "Test suites (one test per suite)")
        h.run_suites()
        h.phase(4, "Custom tests (YAML + Python)")
        h.custom()
        h.phase(5, "CLI commands on executions and runs")
        h.executions()
        h.abort_running()
    except KeyboardInterrupt:
        h.record("interrupted", "harness", FAIL, "interrupted by user")
    finally:
        if not a.dry_run:  # dry run must never kill a DUT app the user is running
            try:
                h.cleanup()
            except KeyboardInterrupt:
                print("cleanup interrupted; writing report anyway", flush=True)
    return h.report(version)


if __name__ == "__main__":
    sys.exit(main())
