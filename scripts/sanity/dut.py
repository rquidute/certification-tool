"""DUT lifecycle manager for the sanity harness.

The TH backend never starts the DUT app, so the harness does it. Every start is
explicit about state: "fresh" wipes /tmp/chip_* (factory reset), "reuse" keeps
the KVS so credentials from a previous commissioning stay valid.
"""
import os
import re
import signal
import subprocess
import time
from typing import Dict, Optional


class DutError(RuntimeError):
    pass


class DutManager:
    def __init__(self, profiles: Dict[str, dict], apps_dir: str, log_dir: str):
        self.profiles = profiles
        self.apps_dir = os.path.expanduser(apps_dir)
        self.log_dir = log_dir
        self.proc: Optional[subprocess.Popen] = None
        self.profile_name: Optional[str] = None
        self.log_path: Optional[str] = None

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
        # Match the command line instead, anchored to the executable path so unrelated processes
        # that merely mention the name (editors, tail, grep) are not hit.
        for prof in self.profiles.values():
            subprocess.run(["pkill", "-9", "-f", rf"(^|/){re.escape(prof['binary'])}( |$)"], check=False)
        time.sleep(1)

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, profile: str, fresh: bool = True, log_dir: Optional[str] = None) -> None:
        """Start the DUT. Raises DutError if it is not ready (mDNS published) in time."""
        prof = self.profiles.get(profile)
        if prof is None:
            raise DutError(f"unknown DUT profile {profile!r}")
        self.stop()
        if fresh:
            subprocess.run("rm -rf /tmp/chip_* /tmp/repl-storage.json", shell=True, check=False)
        binary = os.path.join(self.apps_dir, prof["binary"])
        if not os.access(binary, os.X_OK):
            raise DutError(f"DUT binary not found or not executable: {binary}")
        target = log_dir or self.log_dir  # the step folder, so the DUT log sits next to the step's transcript
        os.makedirs(target, exist_ok=True)
        self.log_path = os.path.join(target, "dut.log")
        log = open(self.log_path, "w")
        self.proc = subprocess.Popen(
            [binary, *prof.get("args", [])], cwd=self.apps_dir, stdout=log, stderr=subprocess.STDOUT
        )
        self.profile_name = profile
        marker = prof.get("ready_marker", "mDNS service published: _matterc._udp")
        deadline = time.time() + prof.get("ready_timeout_s", 60)
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise DutError(f"{prof['binary']} exited early (rc={self.proc.returncode}), see {self.log_path}")
            with open(self.log_path, errors="replace") as f:
                if marker in f.read():
                    return
            time.sleep(1)
        self.stop()
        raise DutError(f"{prof['binary']} not ready within {prof.get('ready_timeout_s', 60)}s, see {self.log_path}")
