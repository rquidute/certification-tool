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
        # main() takes a real per-host lock; several calls in one process would collide with each other
        patcher = mock.patch.object(cli, "acquire_lock", return_value=mock.Mock())
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def run_main(self, version_text, *args):
        """cli.main with `th-cli --version` returning version_text and no phases, so only the start-up logic runs."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), \
                mock.patch.object(cli, "PHASES", []), mock.patch.object(cli.preclean, "preclean"), \
                mock.patch.object(DutManager, "find_running", return_value=[]), \
                mock.patch.object(ThCli, "run", return_value=(0, version_text)):
            code = cli.main(["--manifest", self.manifest, "--out-dir", self.out, "--yes", "--color", "never", *args])
        report = json.load(open(os.path.join(self.out, sorted(os.listdir(self.out))[-1], "report.json")))
        return code, out.getvalue(), report

    def test_a_label_that_does_not_match_the_th_under_test_fails_the_run(self):
        from test_core import VERSION_OUTPUT
        code, out, report = self.run_main(VERSION_OUTPUT, "--version-label", "v2.17-beta1")
        self.assertEqual(code, 1)
        step = next(s for s in report["steps"] if s["id"] == "th-version-matches-label")
        self.assertEqual(step["status"], "FAIL")
        self.assertIn("v2.17-beta1", step["detail"])
        self.assertIn("v2.16-beta3+winter2026", step["detail"])
        self.assertEqual(report["th_versions"]["th"], "v2.16-beta3+winter2026")
        self.assertIn("TH under test: v2.16-beta3+winter2026 (sha 4606147-local, SDK 5741ad7)", out)

    def test_a_matching_label_or_no_label_is_fine_and_the_real_version_is_in_the_report(self):
        from test_core import VERSION_OUTPUT
        code, _, report = self.run_main(VERSION_OUTPUT, "--version-label", "v2.16-beta3")
        self.assertEqual(code, 0)
        self.assertFalse(any(s["id"] == "th-version-matches-label" for s in report["steps"]))
        code, _, report = self.run_main(VERSION_OUTPUT)
        self.assertEqual((code, report["version"]), (0, "v2.16-beta3+winter2026"))

    def test_an_unreadable_version_does_not_fail_a_labelled_run(self):
        code, _, report = self.run_main("", "--version-label", "v2.17-beta1")
        self.assertEqual(code, 0)
        self.assertEqual(report["th_versions"], {})

    def test_only_rejects_unknown_ids_and_lists_the_valid_ones(self):
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            code = cli.main(["--manifest", self.manifest, "--out-dir", self.out, "--yes", "--only", "nope"])
        self.assertEqual(code, 2)
        self.assertIn("nope", err.getvalue())

    def test_only_runs_the_focused_phase_creates_a_project_and_is_marked_not_a_release_verdict(self):
        seen = {}
        with open(self.manifest) as f:
            m = json.load(f)
        m["suites"] = [{"id": "a", "suite": "S", "test": "T"}, {"id": "b", "suite": "S", "test": "T", "depends_on": "a"},
                       {"id": "c", "suite": "S", "test": "T"}]
        json.dump(m, open(self.manifest, "w"))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                mock.patch.object(cli.preclean, "preclean"), mock.patch.object(DutManager, "find_running", return_value=[]), \
                mock.patch.object(ThCli, "run", return_value=(0, "")), \
                mock.patch.object(cli.cli_commands, "create_project",
                                  side_effect=lambda s: (seen.setdefault("project", True), setattr(s, "project_id", 1))), \
                mock.patch.object(cli.suites, "run", side_effect=lambda s: seen.setdefault("only", set(s.only))):
            cli.main(["--manifest", self.manifest, "--out-dir", self.out, "--yes", "--only", "b", "--color", "never"])
        self.assertEqual(seen["only"], {"a", "b"})  # b depends on a; c is not run
        self.assertTrue(seen["project"])
        report = json.load(open(os.path.join(self.out, sorted(os.listdir(self.out))[-1], "report.json")))
        self.assertEqual(report["focused"], ["a", "b"])
        self.assertIn("NOT a release verdict", open(os.path.join(self.out, sorted(os.listdir(self.out))[-1], "report.md")).read())

    def test_a_focused_run_does_nothing_when_the_project_cannot_be_created(self):
        ran = []
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                mock.patch.object(cli.preclean, "preclean"), mock.patch.object(DutManager, "find_running", return_value=[]), \
                mock.patch.object(ThCli, "run", return_value=(0, "")), \
                mock.patch.object(cli.cli_commands, "create_project"), \
                mock.patch.object(cli.suites, "run", side_effect=lambda s: ran.append(1)):
            cli.main(["--manifest", self.manifest, "--out-dir", self.out, "--yes", "--only", "a", "--color", "never"])
        self.assertEqual(ran, [])

    def test_a_second_run_is_refused_while_another_holds_the_lock(self):
        from th_sanity.lock import LockHeld
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()), \
                mock.patch.object(cli, "acquire_lock", side_effect=LockHeld("another sanity run is in progress (pid 99)")), \
                mock.patch.object(DutManager, "find_running", return_value=[]):
            code = cli.main(["--manifest", self.manifest, "--out-dir", self.out, "--yes"])
        self.assertEqual(code, 2)
        self.assertIn("pid 99", err.getvalue())
        self.assertIn("Nothing was changed", err.getvalue())
        self.assertFalse(os.path.exists(self.out))

    def test_dry_run_and_list_suites_do_not_need_the_lock(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                mock.patch.object(cli, "acquire_lock", side_effect=AssertionError("lock must not be taken")), \
                mock.patch.object(cli, "PHASES", []), mock.patch.object(cli.preclean, "preclean"), \
                mock.patch.object(ThCli, "run", return_value=(0, "")):
            self.assertEqual(cli.main(["--manifest", self.manifest, "--out-dir", self.out, "--dry-run"]), 0)

    def test_keep_project_flag_reaches_the_cleanup(self):
        seen = {}
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                mock.patch.object(cli, "acquire_lock"), mock.patch.object(cli, "PHASES", []), \
                mock.patch.object(cli.preclean, "preclean"), mock.patch.object(DutManager, "find_running", return_value=[]), \
                mock.patch.object(cli.preclean, "final_cleanup", side_effect=lambda s, keep: seen.setdefault("keep", keep)), \
                mock.patch.object(ThCli, "run", return_value=(0, "")):
            cli.main(["--manifest", self.manifest, "--out-dir", self.out, "--yes"])
            cli.main(["--manifest", self.manifest, "--out-dir", self.out + "2", "--yes", "--keep-project", "always"])
        self.assertEqual(seen["keep"], "auto")  # first call wins in setdefault: the default is auto

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
