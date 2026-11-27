"""DUT lifecycle manager for the sanity harness.

The TH backend never starts the DUT app, so the harness does it. Every start is
explicit about state: "fresh" wipes /tmp/chip_* (factory reset), "reuse" keeps
the KVS so credentials from a previous commissioning stay valid.

Readiness differs between the two:
  * fresh  - the device is uncommissioned and announces itself as commissionable: `_matterc._udp`.
  * reuse  - the device kept its fabric, so it is already commissioned and does NOT announce
             `_matterc._udp`; it publishes operational records (`_matter._tcp`) instead. If that marker
             is not seen either, a quiet log (no new output for `quiet_s`) is taken as "booted".
Profiles may override the patterns with `ready_marker` / `ready_marker_reuse` (regular expressions), the factory-reset
files with `wipe`, and the quiet period with `quiet_s`.
"""
import os
import re
import shlex
import signal
import subprocess
import time
from typing import Callable, Dict, List, Optional, Tuple

READY_FRESH = r"mDNS service published: _matterc\._udp"
READY_REUSE = r"mDNS service published: _matter\._tcp"
DEFAULT_WIPE = "/tmp/chip_* /tmp/repl-storage.json"


class DutError(RuntimeError):
    pass


def process_pattern(binary: str) -> str:
    """Regex (valid as Python and POSIX ERE) matching a process whose command line STARTS with the binary,
    e.g. './chip-all-clusters-app --wifi' or '/home/u/apps/chip-all-clusters-app'. Anchoring at the start keeps
    `tail -f .../chip-all-clusters-app` or an editor that merely mentions the path from being matched or killed."""
    escaped = re.sub(r"([.+*?^${}()|\[\]\\])", r"\\\1", binary)
    return rf"^([^ ]*/)?{escaped}( |$)"


def parse_ps(text: str) -> List[Tuple[int, str]]:
    """Parse `ps -eo pid=,args=` output into (pid, command line)."""
    rows = []
    for line in text.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit():
            rows.append((int(pid), args.strip()))
    return rows


def list_processes() -> List[Tuple[int, str]]:
    try:
        done = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=10, check=True)
    except (OSError, subprocess.SubprocessError) as e:
        raise DutError(f"cannot list running processes with `ps`: {e}") from e
    return parse_ps(done.stdout)


def log_tail(path: Optional[str], lines: int = 15) -> str:
    try:
        with open(path or "", errors="replace") as f:
            return "\n".join(f.read().splitlines()[-lines:])
    except OSError:
        return "(no log)"


class DutManager:
    def __init__(self, profiles: Dict[str, dict], apps_dir: str, log_dir: str,
                 lister: Callable[[], List[Tuple[int, str]]] = list_processes):
        self._lister = lister
        self.profiles = profiles
        self.apps_dir = os.path.expanduser(apps_dir)
        self.log_dir = log_dir
        self.proc: Optional[subprocess.Popen] = None
        self.profile_name: Optional[str] = None
        self.log_path: Optional[str] = None
        self.ready_how = ""  # how the last start was judged ready (for the step note)
        self.last_command = ""  # command line of the last start, for the report

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.proc = None
        # Also catch apps left over from a previous (crashed) harness run. `pkill -x` cannot be used:
        # it refuses process names over 15 characters (chip-all-clusters-app is 21) and matches nothing.
        for prof in self.profiles.values():
            subprocess.run(["pkill", "-9", "-f", process_pattern(prof["binary"])], check=False)
        time.sleep(1)

    def find_running(self) -> List[Tuple[int, str]]:
        """(pid, command line) of every running DUT app of any profile, other than this harness's own child."""
        patterns = [re.compile(process_pattern(p["binary"])) for p in self.profiles.values()]
        own = {os.getpid(), self.proc.pid if self.proc else -1}
        return [(pid, args) for pid, args in self._lister()
                if pid not in own and any(pat.search(args) for pat in patterns)]

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, profile: str, fresh: bool = True, log_dir: Optional[str] = None) -> None:
        """Start the DUT. Raises DutError if it is not ready in time."""
        prof = self.profiles.get(profile)
        if prof is None:
            raise DutError(f"unknown DUT profile {profile!r}")
        self.stop()
        wipe = prof.get("wipe", DEFAULT_WIPE)  # factory reset for a fresh start; "" disables it (used by the tests)
        if fresh and wipe:
            subprocess.run(f"rm -rf {wipe}", shell=True, check=False)
        binary = os.path.join(self.apps_dir, prof["binary"])
        if not os.access(binary, os.X_OK):
            raise DutError(f"DUT binary not found or not executable: {binary}")
        target = log_dir or self.log_dir  # the step folder, so the DUT log sits next to the step's transcript
        os.makedirs(target, exist_ok=True)
        self.log_path = os.path.join(target, "dut.log")
        self.last_command = " ".join(shlex.quote(p) for p in [binary, *prof.get("args", [])])
        with open(self.log_path, "w") as log:  # the child keeps its own copy of the descriptor
            self.proc = subprocess.Popen(
                [binary, *prof.get("args", [])], cwd=self.apps_dir, stdout=log, stderr=subprocess.STDOUT
            )
        self.profile_name = profile
        marker = re.compile(prof.get("ready_marker" if fresh else "ready_marker_reuse", READY_FRESH if fresh else READY_REUSE))
        timeout = prof.get("ready_timeout_s", 60)
        quiet_s = prof.get("quiet_s", 5)
        start = last_growth = time.time()
        size = -1
        while time.time() - start < timeout:
            if self.proc.poll() is not None:
                raise DutError(f"{prof['binary']} exited early (rc={self.proc.returncode}), see {self.log_path}\n"
                               f"{log_tail(self.log_path)}")
            with open(self.log_path, errors="replace") as f:
                text = f.read()
            if marker.search(text):
                self.ready_how = f"marker '{marker.pattern}'"
                return
            if len(text) != size:
                size, last_growth = len(text), time.time()
            elif not fresh and size > 0 and time.time() - last_growth >= quiet_s:
                self.ready_how = f"log quiet for {quiet_s}s (no '{marker.pattern}' line; device already commissioned?)"
                return
            time.sleep(1)
        self.stop()
        raise DutError(f"{prof['binary']} not ready within {timeout}s (waiting for '{marker.pattern}'), see {self.log_path}\n"
                       f"last log lines:\n{log_tail(self.log_path)}")
