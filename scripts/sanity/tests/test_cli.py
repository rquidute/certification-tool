import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import fakes  # noqa: F401  (sets sys.path)
from th_sanity import cli
from th_sanity.dut import DutError, DutManager
from th_sanity.thcli import ThCli

NAME = "fake-dut-for-sanity-cli-tests"


class StartupSafety(unittest.TestCase):
    """What happens at startup when a DUT app is already running, and the other start-up gates."""
    RUNNING = [(4242, "/home/u/apps/chip-all-clusters-app --wifi")]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.manifest = os.path.join(self.tmp, "manifest.json")
        with open(self.manifest, "w") as f:
            json.dump({"dut_profiles": {"d": {"binary": NAME, "wipe": ""}}, "apps_dir": self.tmp, "suites": []}, f)
        self.out = os.path.join(self.tmp, "out")

    def main(self, *args, running=None, after=None):
        """Run cli.main with the process list scripted: `running` at startup, `after` once it was asked to stop."""
        calls = {"n": 0}

        def find_running(_self):
            calls["n"] += 1
            return running if calls["n"] == 1 else (after if after is not None else [])
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
                mock.patch.object(cli, "PHASES", []), mock.patch.object(cli.preclean, "preclean"), \
                mock.patch.object(DutManager, "find_running", find_running), \
                mock.patch.object(DutManager, "stop") as stop, \
                mock.patch.object(ThCli, "run", return_value=(0, "")):
            code = cli.main(["--manifest", self.manifest, "--out-dir", self.out, "--version-label", "t", "--color", "never", *args])
        self.stop = stop
        return code, out.getvalue(), err.getvalue()

    def test_refuses_to_start_and_stops_nothing(self):
        code, _, err = self.main("--yes", running=self.RUNNING)
        self.assertEqual(code, 2)
        self.stop.assert_not_called()
        self.assertIn("pid 4242", err)
        self.assertIn("--kill-existing", err)
        self.assertIn("Nothing was changed", err)
        self.assertFalse(os.path.exists(self.out))

    def test_kill_existing_stops_it_and_records_which_process(self):
        code, _, _ = self.main("--yes", "--kill-existing", running=self.RUNNING)
        self.assertEqual(code, 0)
        self.stop.assert_called()
        report = json.load(open(os.path.join(self.out, os.listdir(self.out)[0], "report.json")))
        step = next(s for s in report["steps"] if s["id"] == "stop-existing-dut")
        self.assertEqual(step["status"], "PASS")
        self.assertIn("pid 4242", step["detail"])

    def test_kill_existing_that_does_not_work_is_a_failed_step(self):
        code, _, _ = self.main("--yes", "--kill-existing", running=self.RUNNING, after=self.RUNNING)
        self.assertEqual(code, 1)
        report = json.load(open(os.path.join(self.out, os.listdir(self.out)[0], "report.json")))
        self.assertEqual(next(s for s in report["steps"] if s["id"] == "stop-existing-dut")["status"], "FAIL")

    def test_nothing_running_starts_normally_without_kill_existing(self):
        code, _, _ = self.main("--yes", running=[])
        self.assertEqual(code, 0)

    def test_dry_run_neither_refuses_nor_stops(self):
        code, _, _ = self.main("--dry-run", running=self.RUNNING)
        self.assertEqual(code, 0)
        self.stop.assert_not_called()

    def test_an_unreadable_process_list_refuses_to_start(self):
        def boom(_self):
            raise DutError("cannot list running processes with `ps`: nope")
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()), mock.patch.object(DutManager, "find_running", boom):
            code = cli.main(["--manifest", self.manifest, "--out-dir", self.out, "--yes", "--color", "never"])
        self.assertEqual(code, 2)
        self.assertIn("Cannot tell whether a DUT app is already running", err.getvalue())

    def test_confirmation_screen_names_the_apps_that_will_be_stopped(self):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("builtins.input", return_value=""):
            ok = cli.confirm_start([(4242, "/home/u/apps/chip-all-clusters-app --wifi")])
        text = out.getvalue()
        self.assertTrue(ok)
        self.assertIn("pid 4242: /home/u/apps/chip-all-clusters-app --wifi", text)
        self.assertIn("will be stopped", text)

    def test_confirmation_screen_when_nothing_is_running(self):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("builtins.input", return_value=""):
            cli.confirm_start([])
        self.assertIn("No DUT app is running", out.getvalue())

    def test_confirmation_answers(self):
        for answer, expected in (("", True), ("y", True), ("n", False), ("no", False), ("garbage", False)):
            with redirect_stdout(io.StringIO()), mock.patch("builtins.input", return_value=answer):
                self.assertEqual(cli.confirm_start([]), expected, answer)
        for exc in (EOFError, KeyboardInterrupt):
            with redirect_stdout(io.StringIO()), mock.patch("builtins.input", side_effect=exc):
                self.assertFalse(cli.confirm_start([]))

    def test_without_a_terminal_and_without_yes_it_refuses(self):
        with mock.patch("sys.stdin") as stdin:
            stdin.isatty.return_value = False
            code, _, err = self.main(running=[])
        self.assertEqual(code, 2)
        self.assertIn("--yes", err)


if __name__ == "__main__":
    unittest.main()
