import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from fakes import FakeDut, FakePrompts, FakeTh, ids, js, make_session, status_of
from th_sanity import report
from th_sanity.phases import abort, cli_commands, coverage, custom, executions, preclean, suites
from th_sanity.results import FAIL, NOT_RUN, PASS
from th_sanity.runtest import run_test


def avail(suites_map):
    """available-tests --json in the real shape: collections > test_suites > test_cases keyed by public_id."""
    return js({"test_collections": {"c": {"test_suites": {
        sid: {"metadata": {"public_id": sid}, "test_cases": {t: {"metadata": {"public_id": t}} for t in tests}}
        for sid, tests in suites_map.items()}}}})


class Preclean(unittest.TestCase):
    NAMES = ["sanity-1795802500", "sanity-harness-1795802500", "sanity-harness-1795802500-renamed",
             "sanity-1795802500-renamed (1)", "sanity-checks", "sanity", "my-sanity-1795802500", "sanity-12345",
             "Production", "sanity-harness-1795802500x"]

    def session(self, names, **kw):
        th = FakeTh()
        th.handler = lambda a: (0, js([{"id": i, "name": n} for i, n in enumerate(names, 1)])) if a[:2] == ["project", "list"] else (0, "")
        return make_session(th=th, **kw), th

    def test_only_harness_named_projects_are_deleted(self):
        s, th = self.session(self.NAMES)
        preclean.preclean(s)
        deleted = [self.NAMES[int(c[3]) - 1] for c in th.calls if c[:2] == ["project", "delete"]]
        self.assertEqual(deleted, ["sanity-1795802500", "sanity-harness-1795802500", "sanity-harness-1795802500-renamed",
                                   "sanity-1795802500-renamed (1)"])

    def test_dry_run_deletes_nothing(self):
        s, th = self.session(self.NAMES, dry_run=True)
        preclean.preclean(s)
        self.assertFalse(th.called("project", "delete"))
        self.assertEqual(status_of(s, "preclean-projects"), PASS)

    def test_a_flood_of_matches_is_refused(self):
        s, th = self.session([f"sanity-17958025{i:02d}" for i in range(30)])
        preclean.preclean(s)
        self.assertFalse(th.called("project", "delete"))
        self.assertEqual(status_of(s, "preclean-projects"), FAIL)

    def test_unreadable_project_list_fails_the_step(self):
        th = FakeTh()
        th.handler = lambda a: (1, "boom")
        s = make_session(th=th)
        preclean.preclean(s)
        self.assertEqual(status_of(s, "preclean-list-projects"), FAIL)

    def test_final_cleanup_aborts_stops_the_dut_and_deletes_own_projects(self):
        s = make_session()
        s.project_id, s.extra_projects = 11, [12]
        preclean.final_cleanup(s)
        self.assertTrue(s.th.called("abort-testing"))
        self.assertIn(("stopped", None), s.dut.started)
        self.assertEqual([c[3] for c in s.th.calls if c[:2] == ["project", "delete"]], ["12", "11"])


class CliCommands(unittest.TestCase):
    def runner(self, state):
        th = FakeTh()
        th.handler = lambda a: (0, js({"state": f"TestRunnerState.{state}"})) if a[0] == "test-runner-status" else (
            (1, "Error: Test Engine is not active.") if a[0] == "abort-testing" else (0, ""))
        return th

    def test_idle_engine_must_refuse_abort_and_stay_idle(self):
        s = make_session(th=self.runner("IDLE"))
        cli_commands._abort_when_idle(s, "cli")
        self.assertEqual(ids(s, PASS), ["runner-idle-before-abort", "abort-testing-idle-refused", "runner-still-idle-after-abort"])

    def test_a_busy_engine_is_never_aborted(self):
        s = make_session(th=self.runner("RUNNING"))
        cli_commands._abort_when_idle(s, "cli")
        self.assertFalse(s.th.called("abort-testing"))
        self.assertEqual(status_of(s, "runner-idle-before-abort"), FAIL)

    def test_abort_accepted_while_idle_is_a_failure(self):
        th = self.runner("IDLE")
        base = th.handler
        th.handler = lambda a: (0, "Aborting") if a[0] == "abort-testing" else base(a)
        s = make_session(th=th)
        cli_commands._abort_when_idle(s, "cli")
        self.assertEqual(status_of(s, "abort-testing-idle-refused"), FAIL)

    def test_dry_run_stops_after_the_read_only_commands(self):
        s = make_session(dry_run=True)
        cli_commands.run(s)
        self.assertFalse(s.th.called("project", "create"))
        self.assertFalse(s.th.called("abort-testing"))


class Coverage(unittest.TestCase):
    def session(self, live, **over):
        th = FakeTh()
        th.handler = lambda a: (0, avail(live))
        return make_session(th=th, **over)

    def test_live_suites_are_read_from_the_real_json_shape(self):
        s = self.session({"S1": ["T1", "T2"], "S2": ["T3"]})
        self.assertTrue(coverage.load_live(s))
        self.assertEqual(s.live, {"S1": {"T1", "T2"}, "S2": {"T3"}})

    def test_every_suite_must_be_in_the_manifest_or_excluded(self):
        s = self.session({"S1": ["T1"], "S2": ["T9"], "S3": ["T7"]},
                         excluded=[{"suite": "S3", "reason": "needs a human"}])
        coverage.run(s)
        self.assertEqual(status_of(s, "manifest-a"), PASS)
        self.assertEqual(status_of(s, "uncovered-S2"), FAIL)
        self.assertEqual(status_of(s, "excluded-S3"), NOT_RUN)

    def test_manifest_entry_with_unknown_test_or_todo_fails(self):
        s = self.session({"S1": ["T1"]}, suites=[{"id": "a", "suite": "S1", "test": "NOPE"},
                                                  {"id": "b", "suite": "TODO", "test": "TODO"}])
        coverage.run(s)
        self.assertEqual(ids(s, FAIL), ["manifest-a", "manifest-b"])

    def test_user_custom_suite_is_covered_only_when_a_fixture_targets_it(self):
        s = self.session({"S1": ["T1"], "S1-custom": ["T1-custom"], "S2-custom": ["X-custom"]},
                         custom=[{"id": "f", "expect_suite": "S1-custom"}])
        coverage.run(s)
        self.assertEqual(status_of(s, "custom-suite-S1-custom"), PASS)
        self.assertEqual(status_of(s, "excluded-S2-custom"), NOT_RUN)
        self.assertEqual(ids(s, FAIL), [])

    def test_unreadable_available_tests_fails(self):
        th = FakeTh()
        th.handler = lambda a: (1, "")
        s = make_session(th=th)
        self.assertFalse(coverage.load_live(s))
        self.assertEqual(status_of(s, "load-live-suites"), FAIL)


class Suites(unittest.TestCase):
    ENTRIES = [{"id": "a", "suite": "S", "test": "T", "dut": None}, {"id": "b", "suite": "S", "test": "T", "depends_on": "a"},
               {"id": "c", "suite": "TODO", "test": "TODO"}]

    def test_dependency_and_todo_are_skipped_not_passed(self):
        s = make_session(suites=self.ENTRIES)
        with mock.patch.object(suites, "run_test", return_value=None) as rt:
            rt.side_effect = lambda s_, sid, *a, **k: s_.record(sid, "suites", FAIL, "x")
            suites.run(s)
        self.assertEqual(status_of(s, "run-a"), FAIL)
        self.assertEqual(status_of(s, "run-b"), NOT_RUN)
        self.assertEqual(status_of(s, "run-c"), NOT_RUN)

    def test_passed_run_is_remembered_for_the_execution_phase(self):
        s = make_session(suites=self.ENTRIES[:1])
        with mock.patch.object(suites, "run_test", side_effect=lambda s_, sid, *a, **k: (s_.record(sid, "suites", PASS), 42)[1]):
            suites.run(s)
        self.assertEqual(s.exec_ids, {"a": 42})
        self.assertIn("a", s.exec_meta)

    def test_dry_run_runs_nothing(self):
        s = make_session(suites=self.ENTRIES[:1], dry_run=True)
        with mock.patch.object(suites, "run_test") as rt:
            suites.run(s)
        rt.assert_not_called()
        self.assertEqual(status_of(s, "run-a"), NOT_RUN)


class RunTest(unittest.TestCase):
    def go(self, result, expect="pass", dut=None, **kw):
        th = FakeTh()
        s = make_session(th=th, prompts=FakePrompts(result), dut=kw.pop("fdut", FakeDut()))
        run_test(s, "run-x", "suites", "TC-1", expect, [], dut, "fresh", **kw)
        return s

    def test_pass_when_the_outcome_matches(self):
        s = self.go(("Results: 1 passed\n", 0, ""))
        self.assertEqual(status_of(s, "run-x"), PASS)

    def test_a_failing_test_is_a_failure_unless_failure_was_expected(self):
        self.assertEqual(status_of(self.go(("Results: 1 failed\n", 1, "")), "run-x"), FAIL)
        self.assertEqual(status_of(self.go(("Results: 1 failed\n", 1, ""), expect="fail"), "run-x"), PASS)

    def test_infrastructure_problems_always_fail(self):
        self.assertEqual(status_of(self.go(("oops", 1, "")), "run-x"), FAIL)  # no Results line
        self.assertEqual(status_of(self.go(("", -1, "timeout after 5s")), "run-x"), FAIL)
        s = self.go(("Results: 1 passed\n", 0, ""), dut="d", fdut=FakeDut(error="no binary"))
        self.assertIn("DUT did not start", s.results.steps[-1]["detail"])

    def test_dut_that_died_during_the_run_is_a_failure(self):
        fdut = FakeDut()
        fdut.alive = False
        s = self.go(("Results: 1 passed\n", 0, ""), dut="d", fdut=fdut)
        self.assertEqual(status_of(s, "run-x"), FAIL)
        self.assertIn("DUT process died", s.results.steps[-1]["detail"])

    def test_the_step_folder_gets_the_transcript_path(self):
        th, prompts = FakeTh(), FakePrompts()
        s = make_session(th=th, prompts=prompts)
        run_test(s, "run-x", "suites", "TC-1", "pass", [], None, "fresh")
        self.assertTrue(prompts.calls[0]["transcript"].endswith(os.path.join("suites", "run-x", "transcript.out")))


class Custom(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = os.path.join(self.tmp, "sdk")
        self.dst = os.path.join(self.tmp, "custom")
        os.makedirs(self.src)
        os.makedirs(self.dst)
        for n in ("a.py", "b.py", "c.yaml"):
            open(os.path.join(self.src, n), "w").write("x")

    def fixtures(self):
        d = {"dest_dir": self.dst}
        return [{"id": "fa", "src": self.src + "/a.py", "test": "TC_A", "expect_suite": "S-custom", **d},
                {"id": "fb", "src": self.src + "/b.py", "test": "TC_B", "expect_suite": "S-custom", "run": False, "run_note": "by design", **d},
                {"id": "fc", "kind": "yaml", "src": self.src + "/c.yaml", "test": "TC-C", "expect_suite": "Y-custom", **d}]

    def session(self, loaded):
        th = FakeTh()
        s = make_session(th=th, custom=self.fixtures())
        self.rescans = 0

        def handler(a):
            if a[0] == "rescan-tests":
                self.rescans += 1
                return 0, "rescanned"
            if a[:2] == ["available-tests", "--json"]:
                return 0, avail(loaded() if os.listdir(self.dst) else {"S": ["T"]})
            return 0, ""
        th.handler = handler
        return s

    def test_one_rescan_in_one_out_files_removed_and_yaml_miss_explained(self):
        s = self.session(lambda: {"S-custom": ["TC_A-custom", "TC_B-custom"]})
        with mock.patch.object(custom, "run_test", return_value=7) as rt:
            custom.run(s)
        self.assertEqual(self.rescans, 2)
        self.assertEqual(os.listdir(self.dst), [])
        self.assertEqual([c.args[3] for c in rt.call_args_list], ["TC_A-custom"])  # fb is run:false, fc not visible
        self.assertEqual(status_of(s, "custom-fa-visible"), PASS)
        self.assertEqual(status_of(s, "custom-fb-run"), NOT_RUN)
        self.assertEqual(status_of(s, "custom-fc-visible"), FAIL)
        self.assertIn("YAML collection is built when the backend imports", next(x for x in s.results.steps if x["id"] == "custom-fc-visible")["detail"])
        self.assertEqual({sid for sid in ids(s, PASS) if sid.endswith("-removed")}, {"custom-fa-removed", "custom-fb-removed", "custom-fc-removed"})
        self.assertEqual(s.exec_ids, {"custom-fa": 7})

    def test_id_identical_to_the_sdk_test_is_not_run(self):
        s = self.session(lambda: {"S-custom": ["TC_A", "TC_B-custom"]})  # same id as the SDK test
        with mock.patch.object(custom, "run_test") as rt:
            custom.run(s)
        rt.assert_not_called()
        self.assertEqual(status_of(s, "custom-fa-run"), NOT_RUN)

    def test_a_user_file_is_never_overwritten_or_deleted(self):
        keep = os.path.join(self.dst, "a.py")
        open(keep, "w").write("mine")
        s = self.session(lambda: {})
        with mock.patch.object(custom, "run_test"):
            custom.run(s)
        self.assertEqual(open(keep).read(), "mine")
        self.assertEqual(status_of(s, "custom-fa"), NOT_RUN)

    def test_fixture_removed_even_when_the_run_blows_up(self):
        s = self.session(lambda: {"S-custom": ["TC_A-custom", "TC_B-custom"]})
        with mock.patch.object(custom, "run_test", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                custom.run(s)
        self.assertEqual(os.listdir(self.dst), [os.path.basename(keep) for keep in []])
        self.assertEqual(self.rescans, 2)

    def test_dry_run_copies_nothing(self):
        s = self.session(lambda: {})
        s.dry_run = True
        custom.run(s)
        self.assertEqual(os.listdir(self.dst), [])
        self.assertEqual(self.rescans, 0)


class Executions(unittest.TestCase):
    def session(self, new_state="TestStateEnum.PASSED", create=True, dut_error=None):
        th = FakeTh()
        s = make_session(th=th, dut=FakeDut(error=dut_error), suites=[{"id": "y", "suite": "S", "test": "T", "dut": "d"}])
        s.exec_ids, s.exec_meta = {"y": 36}, {"y": s.m["suites"][0]}
        ran = {"v": False}

        def handler(a):
            if a[:3] == ["test-run-execution", "list", "--json"] or a[:2] == ["test-run-execution", "list"] and "--all" in a:
                rows = [{"id": 36, "state": "TestStateEnum.PASSED"}] + ([{"id": 44, "state": new_state}] if ran["v"] and create else [])
                return 0, js(rows)
            if a[:4] == ["test-run-execution", "list", "-i", "36"]:
                return 0, js({"id": 36, "state": "TestStateEnum.PASSED"})
            if a[:4] == ["test-run-execution", "list", "-i", "44"]:
                return 0, js({"id": 44, "state": new_state})
            if a[:2] == ["test-run-execution", "pics-export"]:
                return 1, "Error: (Status: 404) - No PICS were used by this test run execution"
            return 0, ""
        th.handler = handler

        def prompts_run(*a, **k):
            ran["v"] = True
            return "out", 0, ""
        s.prompts = mock.Mock(run=mock.Mock(side_effect=prompts_run))
        return s

    def repeat(self, **kw):
        s = self.session(**kw)
        executions._repeat(s, "executions", "y", s.step_dir("executions", "execution-repeat"))
        return s

    def test_repeat_passes_when_a_new_execution_ends_like_the_original(self):
        s = self.repeat()
        self.assertEqual(status_of(s, "execution-repeat"), PASS)
        self.assertIn("repeated as 44", s.results.steps[0]["detail"])
        self.assertTrue(s.th.called("test-run-execution", "delete", "-i", "44", "-y"))
        self.assertEqual(s.dut.started[0], ("d", True))  # DUT restarted fresh first

    def test_repeat_fails_on_a_different_outcome(self):
        self.assertEqual(status_of(self.repeat(new_state="TestStateEnum.FAILED"), "execution-repeat"), FAIL)

    def test_repeat_fails_when_nothing_new_was_created(self):
        self.assertEqual(status_of(self.repeat(create=False), "execution-repeat"), FAIL)

    def test_repeat_fails_when_the_dut_does_not_start(self):
        self.assertEqual(status_of(self.repeat(dut_error="no binary"), "execution-repeat"), FAIL)

    def test_pics_export_404_is_accepted_but_flagged_as_partial(self):
        s = self.session()
        executions._pics_export(s, "executions", "36", s.out_dir)
        step = s.results.steps[0]
        self.assertEqual(step["status"], PASS)
        self.assertIn("NOT exercised", step["detail"])

    def test_nothing_to_repeat_is_not_run(self):
        s = self.session()
        executions._repeat(s, "executions", None, s.out_dir)
        self.assertEqual(status_of(s, "execution-repeat"), NOT_RUN)


class Abort(unittest.TestCase):
    """abort-while-running with a scripted engine: state flips to 'running' once the run starts."""

    def session(self, accept=True, final_state="TestStateEnum.CANCELLED", run_secs=None, instant=False):
        th = FakeTh()
        s = make_session(th=th, suites=[{"id": "p", "suite": "S", "test": "T", "dut": None}],
                         abort_run={"from": "p", "after_s": 0})
        engine = {"state": "IDLE", "aborted": threading.Event(), "execs": [5]}

        def handler(a):
            if a[0] == "test-runner-status":
                return 0, js({"state": f"TestRunnerState.{engine['state']}"})
            if a[0] == "abort-testing":
                if accept:
                    engine["aborted"].set()
                    return 0, "Aborting testing."
                return 1, "Error 409"
            if a[:3] == ["test-run-execution", "list", "--json"]:
                return 0, js([{"id": i} for i in engine["execs"]])
            if a[:3] == ["test-run-execution", "list", "-i"]:
                return 0, js({"id": a[3], "state": final_state})
            return 0, ""
        th.handler = handler

        def prompts_run(*a, **k):
            engine["state"] = "RUNNING"
            if instant:  # th-cli already gone while the engine still reports 'running': the test was too short
                return "out", 0, ""
            if run_secs is not None:
                time.sleep(run_secs)
            else:
                engine["aborted"].wait(timeout=10)
            engine["execs"].append(6)
            engine["state"] = "IDLE"
            return "out", 1, ""
        s.prompts = mock.Mock(run=mock.Mock(side_effect=prompts_run))
        return s

    def test_abort_accepted_run_ends_engine_idle_execution_cancelled(self):
        s = self.session()
        abort.run(s)
        self.assertEqual(ids(s, FAIL), [])
        self.assertEqual(ids(s, PASS), ["abort-testing-running", "run-ends-after-abort", "runner-idle-after-abort",
                                        "execution-cancelled-after-abort"])

    def test_the_abort_request_is_appended_to_the_transcript_after_the_run(self):
        s = self.session()
        abort.run(s)
        text = open(os.path.join(s.out_dir, "cli", "abort-testing-running", "transcript.out")).read()
        self.assertIn("# abort request, sent while the run above was in progress:\n$ th-cli abort-testing\nAborting testing.", text)

    def test_a_test_that_finishes_before_the_abort_is_reported_not_passed(self):
        s = self.session(instant=True)
        abort.run(s)
        self.assertEqual(status_of(s, "abort-testing-running"), FAIL)
        self.assertIn("finished before the abort", s.results.steps[0]["detail"])

    def test_abort_refused_while_running_fails(self):
        s = self.session(accept=False, run_secs=2)
        abort.run(s)
        self.assertEqual(status_of(s, "abort-testing-running"), FAIL)

    def test_execution_that_is_not_cancelled_fails(self):
        s = self.session(final_state="TestStateEnum.PASSED")
        abort.run(s)
        self.assertEqual(status_of(s, "execution-cancelled-after-abort"), FAIL)

    def test_dry_run_and_missing_config(self):
        s = self.session()
        s.dry_run = True
        abort.run(s)
        self.assertEqual(status_of(s, "abort-testing-running"), NOT_RUN)
        s2 = make_session()
        abort.run(s2)
        self.assertEqual(s2.results.steps, [])


class CliOutputIsKept(unittest.TestCase):
    """Every th-cli command that is a step keeps its command, exit code and full output in the step folder."""

    def read(self, s, group_dir, sid):
        with open(os.path.join(s.out_dir, group_dir, sid, "cli-output.txt")) as f:
            return f.read()

    def test_passing_and_failing_steps_both_keep_the_full_output(self):
        th = FakeTh()
        th.handler = lambda a: (0, "line one\nline two\n") if a[0] == "project" else (1, "Error: boom\nmore detail")
        s = make_session(th=th)
        s.cli_step("project-list", "cli", ["project", "list", "--json"])
        s.cli_step("rescan-tests", "cli", ["rescan-tests"])
        ok = self.read(s, "cli", "project-list")
        self.assertIn("$ th-cli project list --json", ok)
        self.assertIn("exit code: 0", ok)
        self.assertIn("line one\nline two", ok)
        bad = self.read(s, "cli", "rescan-tests")
        self.assertIn("exit code: 1", bad)
        self.assertIn("Error: boom\nmore detail", bad)  # whole output, not the 400 characters the console shows

    def test_arguments_are_shell_quoted_so_the_command_can_be_pasted_back(self):
        s = make_session()
        s.cli_step("rename", "cli", ["test-run-execution", "rename", "-i", "5", "-n", "my name; rm"])
        self.assertIn("-n 'my name; rm'", self.read(s, "cli", "rename"))

    def test_execution_and_custom_steps_land_in_their_category_folder(self):
        s = make_session()
        s.cli_step("execution-list", "executions", ["test-run-execution", "list"])
        s.cli_step("custom-rescan-add", "custom", ["rescan-tests"])
        s.cli_step("cleanup-project-3", "cleanup", ["project", "delete", "-i", "3", "-y"])
        self.assertTrue(os.path.isfile(os.path.join(s.out_dir, "cli", "execution-list", "cli-output.txt")))
        self.assertTrue(os.path.isfile(os.path.join(s.out_dir, "custom", "custom-rescan-add", "cli-output.txt")))
        self.assertTrue(os.path.isfile(os.path.join(s.out_dir, "cleanup", "cleanup-project-3", "cli-output.txt")))

    def test_pics_export_keeps_its_output_even_though_it_is_not_a_cli_step(self):
        th = FakeTh()
        th.handler = lambda a: (1, "Error: (Status: 404) - No PICS were used by this test run execution")
        s = make_session(th=th)
        executions._pics_export(s, "executions", "36", s.out_dir)
        self.assertIn("No PICS were used", self.read(s, "cli", "execution-pics-export"))

    def test_the_abort_response_is_kept_next_to_the_abort_transcript(self):
        s = Abort().session()
        abort.run(s)
        saved = self.read(s, "cli", "abort-testing-running")
        self.assertIn("$ th-cli abort-testing", saved)
        self.assertIn("Aborting testing.", saved)

    def test_a_custom_failure_points_at_a_rescan_output_file_that_exists(self):
        tmp = tempfile.mkdtemp()
        src, dst = os.path.join(tmp, "sdk"), os.path.join(tmp, "custom")
        os.makedirs(src)
        os.makedirs(dst)
        open(os.path.join(src, "c.yaml"), "w").write("x")
        th = FakeTh()
        th.handler = lambda a: (0, "rescan said: nothing new") if a[0] == "rescan-tests" else (0, avail({"S": ["T"]}))
        s = make_session(th=th, custom=[{"id": "fc", "kind": "yaml", "src": src + "/c.yaml", "dest_dir": dst,
                                         "test": "TC-C", "expect_suite": "Y-custom"}])
        custom.run(s)
        detail = next(x["detail"] for x in s.results.steps if x["id"] == "custom-fc-visible")
        path = detail.split("full rescan output: ")[1].strip()
        self.assertTrue(os.path.isfile(path), path)
        self.assertIn("rescan said: nothing new", open(path).read())

    def test_the_whole_cli_phase_leaves_one_output_file_per_command_step(self):
        th = FakeTh()
        created = []

        def handler(a):
            if a[:2] == ["project", "create"]:
                created.append(a[3])  # the harness names its project from the clock; echo it back
            if a[0] == "test-runner-status":
                return 0, js({"state": "TestRunnerState.IDLE"})
            if a[:2] == ["project", "list"]:
                return 0, js([{"id": 1, "name": created[0]}] if created else [])
            if a[0] == "abort-testing":
                return 1, "Error: Failed to abort testing (Status: 409) - Test Engine is not active."
            return 0, "ok"
        th.handler = handler
        s = make_session(th=th)
        cli_commands.run(s)
        steps = [x["id"] for x in s.results.steps if x["group"] == "cli"]
        recorded_only = {"runner-idle-before-abort", "runner-still-idle-after-abort"}  # state checks, no command output
        for sid in steps:
            if sid not in recorded_only:
                self.assertTrue(os.path.isfile(os.path.join(s.out_dir, "cli", sid, "cli-output.txt")), sid)
        self.assertIn("abort-testing-idle-refused", steps)


class CommandLines(unittest.TestCase):
    """The th-cli command line (and how the DUT was started) is recorded per step and shown in the report."""

    def step(self, s, sid):
        return next(x for x in s.results.steps if x["id"] == sid)

    def test_cli_step_records_a_pasteable_command(self):
        s = make_session()
        s.cli_step("rename", "executions", ["test-run-execution", "rename", "-i", "5", "-n", "my name"])
        self.assertEqual(self.step(s, "rename")["command"], "th-cli test-run-execution rename -i 5 -n 'my name'")

    def test_test_run_records_the_run_tests_command_and_the_dut_start(self):
        s = make_session(prompts=FakePrompts(("Results: 1 passed\n", 0, "")))
        s.project_id = 15
        run_test(s, "run-x", "suites", "TC-ACE-1.1", "pass", [], "d", "fresh", config="/m/cfg.json")
        step = self.step(s, "run-x")
        self.assertEqual(step["command"], "th-cli run-tests -t TC-ACE-1.1 -n sanity-run-x --no-color --no-streaming "
                                          "--project-id 15 -c /m/cfg.json")
        self.assertEqual(step["dut_command"], "/home/u/apps/d-app --wifi")

    def test_transcript_notes_carry_the_dut_command_only_when_there_is_a_dut(self):
        prompts = FakePrompts(("Results: 1 passed\n", 0, ""))
        s = make_session(prompts=prompts)
        run_test(s, "a", "suites", "TC-1", "pass", [], "d", "fresh")
        run_test(s, "b", "suites", "TC-1", "pass", [], None, "fresh")
        self.assertEqual(prompts.calls[0]["notes"], ["DUT started with: /home/u/apps/d-app --wifi"])
        self.assertIsNone(prompts.calls[1]["notes"])

    def test_a_test_without_a_dut_has_no_dut_command(self):
        s = make_session(prompts=FakePrompts(("Results: 1 passed\n", 0, "")))
        run_test(s, "run-x", "suites", "TC-1", "pass", [], None, "fresh")
        self.assertEqual(self.step(s, "run-x")["dut_command"], "")

    def test_infrastructure_failures_keep_the_command_too(self):
        s = make_session(prompts=FakePrompts(("", -1, "timeout after 5s")))
        run_test(s, "run-x", "suites", "TC-1", "pass", [], None, "fresh")
        self.assertIn("run-tests -t TC-1", self.step(s, "run-x")["command"])

    def test_failure_on_the_console_shows_the_command_first(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            s = make_session(prompts=FakePrompts(("Results: 1 failed\n", 1, "")))
            run_test(s, "run-x", "suites", "TC-1", "pass", [], None, "fresh")
        lines = buf.getvalue().splitlines()
        i = next(n for n, ln in enumerate(lines) if "FAIL" in ln)
        self.assertIn("command: th-cli run-tests -t TC-1", lines[i + 1])

    def test_repeat_and_abort_record_their_commands(self):
        s = Executions().session()
        executions._repeat(s, "executions", "y", s.step_dir("executions", "execution-repeat"))
        rep = self.step(s, "execution-repeat")
        self.assertIn("test-run-execution repeat -i 36", rep["command"])
        self.assertEqual(rep["dut_command"], "/home/u/apps/d-app --wifi")
        a = Abort().session()
        abort.run(a)
        ab = self.step(a, "abort-testing-running")
        self.assertIn("run-tests -t T", ab["command"])
        self.assertIn("aborted with: th-cli abort-testing", ab["command"])

    def test_report_has_a_command_column_and_the_json_has_the_fields(self):
        s = make_session(prompts=FakePrompts(("Results: 1 failed\n", 1, "")))
        run_test(s, "run-x", "suites", "TC-1", "pass", [], "d", "fresh")
        s.cli_step("piped", "cli", ["x", "a|b"])
        report.write_report(s, "v1")
        md = open(os.path.join(s.out_dir, "report.md")).read()
        self.assertIn("| Step | Status | Time s | Command | Detail |", md)
        self.assertIn("`th-cli run-tests -t TC-1 -n sanity-run-x --no-color --no-streaming`<br>DUT: `/home/u/apps/d-app --wifi`", md)
        self.assertIn("a\\|b", md)  # a pipe inside a command must not break the table
        needs = md.split("## Needs attention")[1]
        self.assertIn("Command:\n```\nth-cli run-tests -t TC-1", needs)
        self.assertIn("DUT started with:\n```\n/home/u/apps/d-app --wifi", needs)
        data = json.load(open(os.path.join(s.out_dir, "report.json")))
        row = next(x for x in data["steps"] if x["id"] == "run-x")
        self.assertTrue(row["command"].startswith("th-cli run-tests"))
        self.assertEqual(row["dut_command"], "/home/u/apps/d-app --wifi")


class FailureDigest(unittest.TestCase):
    OUT = ("Test Run [EXECUTING]\n  - Python Testing Suite [EXECUTING]\n"
           "    - Step 1 [PASSED]\n    - Step 2: read attribute [FAILED]\n"
           "2026-11-27 ERROR | Attribute 0x1234 expected 3 got 5\n2026-11-27 ERROR | Attribute 0x1234 expected 3 got 5\n"
           "Traceback (most recent call last):\nAssertionError: boom\nResults: 1 failed\n")

    def test_digest_keeps_failing_lines_once_and_drops_the_summary_line(self):
        from th_sanity.digest import failure_digest
        d = failure_digest(self.OUT)
        self.assertEqual(d, ["    - Step 2: read attribute [FAILED]", "2026-11-27 ERROR | Attribute 0x1234 expected 3 got 5",
                             "Traceback (most recent call last):", "AssertionError: boom"])

    def test_digest_is_limited_and_empty_when_nothing_failed(self):
        from th_sanity.digest import failure_digest
        self.assertEqual(len(failure_digest("\n".join(f"line {i} ERROR" for i in range(50)), limit=5)), 5)
        self.assertEqual(failure_digest("all fine\nResults: 1 passed"), [])

    def test_a_failed_test_explains_itself_in_the_step_detail(self):
        s = make_session(prompts=FakePrompts((self.OUT, 1, "")))
        run_test(s, "run-x", "suites", "TC-1", "pass", [], None, "fresh")
        detail = next(x["detail"] for x in s.results.steps if x["id"] == "run-x")
        self.assertIn("failing lines in the th-cli output:", detail)
        self.assertIn("Step 2: read attribute [FAILED]", detail)

    def test_a_passing_or_expected_failure_gets_no_digest(self):
        for rc, expect in ((0, "pass"), (1, "fail")):
            s = make_session(prompts=FakePrompts((self.OUT, rc, "")))
            run_test(s, "run-x", "suites", "TC-1", expect, [], None, "fresh")
            self.assertNotIn("failing lines", s.results.steps[0]["detail"])


class Report(unittest.TestCase):
    def test_go_report_files_latest_pointer_and_exit_code(self):
        root = tempfile.mkdtemp()
        s = make_session()
        s.out_dir = os.path.join(root, "sanity_20260101_000000_v1")
        s.record("a", "suites", PASS)
        self.assertEqual(report.write_report(s, "v1"), 0)
        for name in ("report.md", "report.json"):
            self.assertTrue(os.path.isfile(os.path.join(s.out_dir, name)))
        self.assertEqual(os.readlink(os.path.join(root, "latest")), "sanity_20260101_000000_v1")
        self.assertIn("**Verdict: GO**", open(os.path.join(s.out_dir, "report.md")).read())

    def test_no_go_lists_what_needs_attention_and_returns_1(self):
        s = make_session()
        s.record("a", "suites", FAIL, "INFRA: boom\nline two")
        self.assertEqual(report.write_report(s, "v1"), 1)
        text = open(os.path.join(s.out_dir, "report.md")).read()
        self.assertIn("## Needs attention", text)
        self.assertIn("line two", text)

    def test_report_files_contain_no_escape_codes_even_with_colour_on(self):
        from th_sanity import console
        console.USE_COLOR = True
        try:
            s = make_session()
            s.record("a", "suites", FAIL, "x")
            report.write_report(s, "v1")
        finally:
            console.USE_COLOR = False
        self.assertNotIn("\x1b", open(os.path.join(s.out_dir, "report.md")).read())
        self.assertNotIn("\x1b", open(os.path.join(s.out_dir, "report.json")).read())


if __name__ == "__main__":
    unittest.main()
