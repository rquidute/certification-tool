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
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dut import DutError, DutManager  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
PASS, FAIL, NOT_RUN = "PASS", "FAIL", "NOT_RUN"
PROMPT_TRIGGER = "Please enter a number for an option above"


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
        self.dut = DutManager(manifest["dut_profiles"], manifest.get("apps_dir", "~/apps"), out_dir)
        self.project_id: Optional[int] = None
        self.extra_projects: List[int] = []
        self.exec_ids: Dict[str, int] = {}  # step id -> execution id
        self.live: Dict[str, set] = {}  # suite id -> test ids
        os.makedirs(out_dir, exist_ok=True)

    # ---------- recording ----------
    def record(self, sid: str, group: str, status: str, detail: str = "", dur: float = 0.0, log: str = "") -> str:
        self.steps.append({"id": sid, "group": group, "status": status, "detail": detail.strip()[-600:],
                           "duration_s": round(dur, 1), "log": log})
        mark = {"PASS": "ok  ", "FAIL": "FAIL", "NOT_RUN": "skip"}[status]
        print(f"[{mark}] {group}/{sid} {detail.strip().splitlines()[0][:110] if detail.strip() else ''}", flush=True)
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
        name = f"sanity-{int(time.time())}"
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
        self.cli_step("project-logs", g, ["project", "logs", "-i", pid, "-o", os.path.join(self.out_dir, "project_logs")])
        exp = os.path.join(self.out_dir, "project_export.json")
        if self.cli_step("project-export", g, ["project", "export", "-i", pid, "-o", exp])[0]:
            before = {p.get("id") for p in items(self.cli_json(["project", "list", "--json"]))}
            if self.cli_step("project-import", g, ["project", "import", "-f", exp])[0]:
                after = {p.get("id") for p in items(self.cli_json(["project", "list", "--json"]))}
                self.extra_projects += sorted(x for x in after - before if x is not None)
        self.cli_step("rescan-tests", g, ["rescan-tests"])
        self.cli_step("abort-testing-idle", g, ["abort-testing"])

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
        for u in uncovered:
            self.record(f"uncovered-{u}", g, FAIL, f"suite {u!r} exists in the TH ({len(self.live[u])} tests) but is neither in the manifest nor excluded")
        for suite, reason in excluded.items():
            self.record(f"excluded-{suite}", g, NOT_RUN, f"explicitly excluded: {reason}")
        self.record("live-suite-summary", g, PASS, f"{len(self.live)} live suites, {len(listed)} covered, {len(excluded)} excluded")

    # ---------- 3. run one test through run-tests (pty, prompt-aware) ----------
    def answer_for(self, block: str, rules: List[dict]) -> Optional[str]:
        opts = re.findall(r"(?m)^\s*(\d+)\s*[:=\-\)]?\s+(.+?)\s*$", block)
        for rule in rules:
            if re.search(rule["match"], block):
                for num, label in opts:
                    if re.search(rule["choose"], label.strip()):
                        return num
        return None

    def run_test(self, sid: str, group: str, test: str, expect: str, answers: List[dict], dut: Optional[str],
                 dut_start: str, config: Optional[str] = None, timeout: Optional[int] = None) -> Optional[int]:
        t0 = time.time()
        timeout = timeout or self.m.get("default_run_timeout_s", 900)
        if dut:
            try:
                self.dut.start(dut, fresh=(dut_start != "reuse"))
            except DutError as e:
                self.record(sid, group, FAIL, f"INFRA: DUT did not start: {e}")
                return None
        args = [self.th_cli, "run-tests", "-t", test, "-n", f"sanity-{sid}", "--no-color", "--no-streaming"]
        if self.project_id:
            args += ["--project-id", str(self.project_id)]
        if config:
            args += ["-c", config]
        out, rc, why = self.pty_run(args, answers + self.m.get("default_prompt_answers", []), timeout)
        log = (re.findall(r"Log output in:?\s*(\S+)", out) or [""])[-1]
        results = (re.findall(r"(?m)^Results:\s*(.+)$", out) or [""])[-1]
        if dut and not self.dut.is_alive():
            why = why or "DUT process died during the run"
        if why:
            self.record(sid, group, FAIL, f"INFRA: {why}\n{out[-400:]}", time.time() - t0, log)
            return self.last_execution()
        if not results and rc != 0:
            self.record(sid, group, FAIL, f"INFRA: rc={rc}, no Results line\n{out[-400:]}", time.time() - t0, log)
            return self.last_execution()
        actual = "pass" if rc == 0 else "fail"
        good = actual == expect
        self.record(sid, group, PASS if good else FAIL,
                    f"{test}: {results or 'no results line'} (expected {expect}, got {actual})", time.time() - t0, log)
        return self.last_execution()

    def pty_run(self, args: List[str], rules: List[dict], timeout: int, idle: int = 180) -> Tuple[str, int, str]:
        master, slave = pty.openpty()
        proc = subprocess.Popen(args, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
        os.close(slave)
        buf, since_answer = "", 0
        start = last = time.time()
        why = ""
        while True:
            r, _, _ = select.select([master], [], [], 1.0)
            if r:
                try:
                    chunk = os.read(master, 65536).decode(errors="replace")
                except OSError:
                    chunk = ""
                if chunk:
                    buf += strip(chunk)
                    last = time.time()
                    if PROMPT_TRIGGER in buf[since_answer:]:
                        block = buf[since_answer:]
                        num = self.answer_for(block, rules)
                        if num is None:
                            why = "UNHANDLED PROMPT (no answer rule matched): " + block[-300:].replace("\n", " | ")
                            break
                        os.write(master, (num + "\n").encode())
                        since_answer = len(buf)
            if proc.poll() is not None and not r:
                break
            if time.time() - start > timeout:
                why = f"timeout after {timeout}s"
                break
            if time.time() - last > idle:
                why = f"no output for {idle}s (waiting on an unknown prompt?)"
                break
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        os.close(master)
        return buf, proc.returncode if proc.returncode is not None else -1, why

    def last_execution(self) -> Optional[int]:
        ids = [e.get("id") for e in items(self.cli_json(["test-run-execution", "list", "--json", "--all"]))
               if isinstance(e.get("id"), int)]
        return max(ids) if ids else None

    # ---------- 4. suites ----------
    def run_suites(self) -> None:
        done: Dict[str, str] = {}
        for s in self.m["suites"]:
            sid = s["id"]
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
                                config=self.abs_path(s.get("config")))
            done[sid] = self.steps[-1]["status"] if len(self.steps) > n else FAIL
            if eid and done[sid] == PASS:
                self.exec_ids[sid] = eid
                if not s.get("dut"):
                    self.exec_ids.setdefault("_nodut", eid)

    def abs_path(self, p: Optional[str]) -> Optional[str]:
        return os.path.join(self.manifest_dir, p) if p and not os.path.isabs(p) else p

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
                self.cli_step(f"custom-{kind}-rescan-add", g, ["rescan-tests"])
                self.load_live()
                cid = self.find_custom_id(c["test"])
                self.record(f"custom-{kind}-visible", g, PASS if cid else FAIL,
                            f"{cid} listed after rescan" if cid else f"no custom variant of {c['test']} listed after rescan")
                if cid:
                    eid = self.run_test(f"custom-{kind}-run", g, cid, "pass", [], c.get("dut"), "fresh")
                    if eid:
                        self.exec_ids[f"custom-{kind}"] = eid
            finally:
                if os.path.exists(dest):
                    os.remove(dest)
                self.cli_step(f"custom-{kind}-rescan-remove", g, ["rescan-tests"])
                self.load_live()
                left = self.find_custom_id(c["test"])
                self.record(f"custom-{kind}-removed", g, PASS if not left else FAIL,
                            "custom variant gone after removal + rescan" if not left else f"{left} STILL listed after removal + rescan")

    # ---------- 6. test-run-execution commands ----------
    def executions(self) -> None:
        g = "executions"
        if self.dry_run:
            self.cli_step("execution-list", g, ["test-run-execution", "list", "--json"])
            return
        eid = self.exec_ids.get("_nodut") or next(iter(self.exec_ids.values()), None)
        if not eid:
            self.record("executions", g, NOT_RUN, "no successful execution to operate on")
            return
        i = str(eid)
        d = self.out_dir
        self.cli_step("execution-list", g, ["test-run-execution", "list", "--json"])
        self.cli_step("execution-show", g, ["test-run-execution", "list", "-i", i, "--json"])
        self.cli_step("execution-log", g, ["test-run-execution", "log", "-i", i, "-o", os.path.join(d, "exec_log.txt")])
        self.cli_step("execution-pics-export", g, ["test-run-execution", "pics-export", "-i", i, "-o", os.path.join(d, "exec_pics")])
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
        # repeat uses a no-DUT execution so no DUT state is needed
        if "_nodut" in self.exec_ids:
            before = self.exec_set()
            rc, out = self.cli(["test-run-execution", "repeat", "-i", str(self.exec_ids["_nodut"]), "--no-color", "--no-streaming"],
                               timeout=self.m.get("default_run_timeout_s", 900))
            self.record("execution-repeat", g, PASS if rc == 0 else FAIL, f"rc={rc}\n{out[-300:]}" if rc else "repeated")
            for x in sorted(self.exec_set() - before):
                self.cli_step(f"execution-delete-repeated-{x}", g, ["test-run-execution", "delete", "-i", str(x), "-y"])
        else:
            self.record("execution-repeat", g, NOT_RUN, "no no-DUT execution available")

    def exec_set(self) -> set:
        return {e.get("id") for e in items(self.cli_json(["test-run-execution", "list", "--json", "--all"]))}

    # ---------- cleanup + report ----------
    def cleanup(self) -> None:
        self.dut.stop()
        for pid in [*self.extra_projects, self.project_id]:
            if pid is not None:
                self.cli_step(f"cleanup-project-{pid}", "cleanup", ["project", "delete", "-i", str(pid), "-y"])

    def report(self, version: str) -> int:
        fails = [s for s in self.steps if s["status"] == FAIL]
        unexpected = [s for s in self.steps if s["status"] == NOT_RUN and not s["id"].startswith("excluded-")]
        go = not fails and not unexpected
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        base = os.path.join(self.out_dir, f"sanity_{stamp}")
        counts = {k: sum(1 for s in self.steps if s["status"] == k) for k in (PASS, FAIL, NOT_RUN)}
        lines = [f"# TH sanity report - {version}", "", f"- Date: {stamp}", f"- Mode: {'DRY RUN' if self.dry_run else 'FULL'}",
                 f"- **Verdict: {'GO' if go else 'NO-GO'}**", f"- PASS {counts[PASS]} / FAIL {counts[FAIL]} / NOT_RUN {counts[NOT_RUN]}",
                 f"- Live suites: {len(self.live)}", "", "| Group | Step | Status | Time s | Detail |", "|---|---|---|---|---|"]
        for s in self.steps:
            det = s["detail"].splitlines()[0][:140] if s["detail"] else ""
            lines.append(f"| {s['group']} | {s['id']} | {s['status']} | {s['duration_s']} | {det.replace('|', '/')} |")
        if fails or unexpected:
            lines += ["", "## Needs attention"]
            for s in fails + unexpected:
                lines += [f"### {s['group']}/{s['id']} ({s['status']})", "```", s["detail"], "```", f"log: {s['log']}" if s["log"] else ""]
        with open(base + ".md", "w") as f:
            f.write("\n".join(lines) + "\n")
        with open(base + ".json", "w") as f:
            json.dump({"version": version, "verdict": "GO" if go else "NO-GO", "counts": counts, "steps": self.steps}, f, indent=2)
        print(f"\nVerdict: {'GO' if go else 'NO-GO'}  (PASS {counts[PASS]}, FAIL {counts[FAIL]}, NOT_RUN {counts[NOT_RUN]})")
        print(f"Report: {base}.md")
        return 0 if go else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    here = os.path.dirname(os.path.abspath(__file__))
    ap.add_argument("--manifest", default=os.path.join(here, "manifest.json"))
    ap.add_argument("--out-dir", default="sanity_reports")
    ap.add_argument("--dry-run", action="store_true", help="coverage gate and read-only CLI commands only")
    ap.add_argument("--version-label", default="", help="TH version string for the report header")
    ap.add_argument("--list-suites", action="store_true", help="print live suites (public_id, size, sample tests) and exit")
    a = ap.parse_args()
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
        h.smoke()
        h.coverage_gate()
        h.run_suites()
        h.custom()
        h.executions()
    except KeyboardInterrupt:
        h.record("interrupted", "harness", FAIL, "interrupted by user")
    finally:
        if not a.dry_run:  # dry run must never kill a DUT app the user is running
            h.cleanup()
    return h.report(version)


if __name__ == "__main__":
    sys.exit(main())
