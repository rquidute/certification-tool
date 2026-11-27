import os
import stat
import tempfile
import unittest

from fakes import FakeTh, make_session  # noqa: F401  (also sets sys.path)
from th_sanity import prompts
from th_sanity.dut import DutError, DutManager
from th_sanity.prompts import PromptDriver, answer_for, qr_discriminator
from th_sanity.results import FAIL, NOT_RUN, PASS, Results

DEFAULTS = [
    {"match": "Should the DUT be commissioned", "choose": "(?i)^yes$"},
    {"match": "reuse the previous commissioning", "choose": "(?i)^no$"},
    {"match": "(?i)commissioning mode|ready|continue|press", "choose": "(?i)^(yes|ok|done|continue|pass|ready|y)$"},
]
COMMISSION = "Should the DUT be commissioned to run this test case?\n  1: YES\n  2: NO\nPlease enter a number for an option above"
REUSE = ("Do you want to reuse the previous commissioning information?\nIf you select NO, a new commissioning "
         "will be performed\n  1: YES\n  2: NO\nPlease enter a number for an option above")


class AnswerFor(unittest.TestCase):
    def test_commissioning_prompt_is_yes(self):
        self.assertEqual(answer_for(COMMISSION, DEFAULTS), "1")

    def test_reuse_defaults_to_no_but_a_step_rule_overrides_it(self):
        self.assertEqual(answer_for(REUSE, DEFAULTS), "2")
        step = [{"match": "reuse the previous commissioning", "choose": "(?i)^yes$"}]
        self.assertEqual(answer_for(REUSE, step + DEFAULTS), "1")

    def test_manual_catch_all_picks_pass_and_never_fail(self):
        catch_all = [{"match": ".", "choose": "(?i)^(pass|yes|ok|done|continue|y)$"}]
        self.assertEqual(answer_for("Do it\n  1: PASS\n  2: FAIL\nPlease enter", catch_all), "1")
        self.assertIsNone(answer_for("Do it\n  2: FAIL\nPlease enter", catch_all))

    def test_no_matching_rule_means_no_answer(self):
        self.assertIsNone(answer_for("Something new\n  1: ALPHA\nPlease enter", DEFAULTS))


class QrDiscriminator(unittest.TestCase):
    def test_known_payloads_decode_to_0xf00(self):
        for qr in ("MT:-24J0AFN00KA0648G00", "MT:Y.K9042C00KA0648G00", "MT:YNJV75HZ00KA0648G00"):
            self.assertEqual(qr_discriminator(qr), 0xF00, qr)


class Driver(unittest.TestCase):
    """PromptDriver against small bash scripts that behave like th-cli."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.th = FakeTh()
        self.results = Results()
        self.dut = type("D", (), {"log_path": None})()
        self.driver = PromptDriver(self.th, self.results, self.dut)

    def script(self, body: str) -> list:
        path = os.path.join(self.tmp, "fake-th-cli.sh")
        with open(path, "w") as f:
            f.write(body)
        return ["bash", path]

    def test_finished_run_returns_without_waiting_for_idle_timeout(self):
        out, rc, why = self.driver.run(self.script('echo "Results: 1 passed"; exit 0'), [], 20, idle=2)
        self.assertEqual((rc, why), (0, ""))
        self.assertIn("Results: 1 passed", out)
        self.assertFalse(self.th.called("abort-testing"))

    def test_nonzero_exit_is_reported_not_hidden(self):
        _, rc, why = self.driver.run(self.script("echo hi; exit 3"), [], 20, idle=2)
        self.assertEqual((rc, why), (3, ""))

    def test_quiet_but_logging_is_not_idle(self):
        log = os.path.join(self.tmp, "run.log")
        body = f'echo "Log output in: {log}"; for i in 1 2 3 4; do echo x >> {log}; sleep 1; done; echo "Results: ok"'
        _, rc, why = self.driver.run(self.script(body), [], 30, idle=2)
        self.assertEqual((rc, why), (0, ""))

    def test_truly_silent_run_is_stopped_and_the_backend_run_aborted(self):
        _, _, why = self.driver.run(self.script("echo waiting; sleep 30"), [], 30, idle=2)
        self.assertIn("no th-cli output", why)
        self.assertIn("waiting", why)  # the last output is shown
        self.assertTrue(self.th.called("abort-testing"))

    def test_option_prompt_is_answered_from_rules(self):
        body = 'printf "Pick\\n  1: PASS\\n  2: FAIL\\nPlease enter a number for an option above: \\n"; read n; echo "choice=$n"'
        out, _, why = self.driver.run(self.script(body), [{"match": "Pick", "choose": "(?i)^pass$"}], 20, idle=5)
        self.assertEqual(why, "")
        self.assertIn("choice=1", out)

    def test_unknown_option_prompt_stops_the_run_and_shows_the_prompt(self):
        body = 'printf "Starting\\nOdd question?\\n  1: ALPHA\\nPlease enter a number for an option above: \\n"; sleep 30'
        _, _, why = self.driver.run(self.script(body), [], 20, idle=5)
        self.assertTrue(why.startswith("UNHANDLED PROMPT"))
        self.assertIn("Odd question?", why)
        self.assertTrue(self.th.called("abort-testing"))

    def test_file_upload_prompt_is_skipped_with_enter(self):
        body = 'echo "Enter the path to the file to upload (or press Enter to skip): "; read f; echo "file=[$f]"'
        out, _, why = self.driver.run(self.script(body), [], 20, idle=5)
        self.assertEqual(why, "")
        self.assertIn("file=[]", out)

    def test_text_prompts_use_the_dut_qr_code_and_its_discriminator(self):
        dut_log = os.path.join(self.tmp, "dut.log")
        with open(dut_log, "w") as f:
            f.write("boot\nSetupQRCode: [MT:-24J0AFN00KA0648G00]\n")
        self.dut.log_path = dut_log
        body = ('printf "Please enter the QR code payload\\nEnter value: "; read a; '
                'printf "Please enter 12-bit discriminator from the device advertisement\\nEnter value: "; read b; echo "QR=$a DISC=$b"')
        rules = [{"match": "QR code payload", "value": "@dut_qr"}, {"match": "12-bit discriminator", "value": "@qr_disc"}]
        out, _, why = self.driver.run(self.script(body), [], 20, idle=5, text_rules=rules)
        self.assertEqual(why, "")
        self.assertIn("QR=MT:-24J0AFN00KA0648G00 DISC=0xf00", out)

    def test_text_prompt_with_unresolvable_token_stops_the_run(self):
        self.dut.log_path = os.path.join(self.tmp, "missing.log")
        body = 'printf "Please enter the QR code payload\\nEnter value: "; sleep 30'
        _, _, why = self.driver.run(self.script(body), [], 20, idle=5, text_rules=[{"match": "QR", "value": "@dut_qr"}])
        self.assertTrue(why.startswith("UNHANDLED TEXT PROMPT"))


class Dut(unittest.TestCase):
    """DutManager with fake binaries. Unique binary name and wipe disabled: never touches a real chip app."""

    NAME = "fake-dut-for-sanity-tests"

    def setUp(self):
        self.apps = tempfile.mkdtemp()
        self.logs = tempfile.mkdtemp()

    def binary(self, body: str) -> None:
        path = os.path.join(self.apps, self.NAME)
        with open(path, "w") as f:
            f.write("#!/bin/bash\n" + body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def manager(self, **prof) -> DutManager:
        p = {"binary": self.NAME, "wipe": "", **prof}
        return DutManager({"d": p}, self.apps, self.logs)

    def test_fresh_start_waits_for_the_commissionable_marker(self):
        self.binary('echo boot; echo "mDNS service published: _matterc._udp"; sleep 30')
        m = self.manager()
        try:
            m.start("d", fresh=True)
            self.assertTrue(m.is_alive())
            self.assertIn("_matterc", m.ready_how)
        finally:
            m.stop()

    def test_reuse_start_accepts_the_operational_marker(self):
        self.binary('echo boot; echo "mDNS service published: _matter._tcp"; sleep 30')
        m = self.manager()
        try:
            m.start("d", fresh=False)
            self.assertIn("_matter", m.ready_how)
        finally:
            m.stop()

    def test_reuse_start_falls_back_to_a_quiet_log(self):
        self.binary('echo "booting"; echo "server up"; sleep 30')
        m = self.manager(quiet_s=2)
        try:
            m.start("d", fresh=False)
            self.assertIn("log quiet", m.ready_how)
        finally:
            m.stop()

    def test_fresh_start_does_not_accept_a_quiet_log_and_the_error_shows_the_log(self):
        self.binary('echo "only this line"; sleep 30')
        m = self.manager(ready_timeout_s=3, quiet_s=1)
        with self.assertRaises(DutError) as cm:
            m.start("d", fresh=True)
        self.assertIn("not ready within 3s", str(cm.exception))
        self.assertIn("only this line", str(cm.exception))
        self.assertFalse(m.is_alive())

    def test_early_exit_is_reported_with_the_log(self):
        self.binary('echo "crashed: no interface"; exit 7')
        m = self.manager()
        with self.assertRaises(DutError) as cm:
            m.start("d", fresh=True)
        self.assertIn("exited early (rc=7)", str(cm.exception))
        self.assertIn("no interface", str(cm.exception))

    def test_log_goes_to_the_given_step_folder(self):
        self.binary('echo "mDNS service published: _matterc._udp"; sleep 30')
        step = os.path.join(self.logs, "suites", "run-x")
        m = self.manager()
        try:
            m.start("d", fresh=True, log_dir=step)
            self.assertEqual(m.log_path, os.path.join(step, "dut.log"))
        finally:
            m.stop()

    def test_missing_binary_and_unknown_profile(self):
        m = self.manager()
        with self.assertRaises(DutError):
            m.start("d")
        with self.assertRaises(DutError):
            m.start("nope")


class ResultsVerdict(unittest.TestCase):
    def test_not_run_without_an_exclusion_is_no_go(self):
        r = Results()
        r.record("a", "suites", PASS)
        r.record("excluded-x", "coverage", NOT_RUN, "reason")
        self.assertTrue(r.is_go())
        r.record("b", "suites", NOT_RUN, "dry-run")
        self.assertFalse(r.is_go())

    def test_any_failure_is_no_go(self):
        r = Results()
        r.record("a", "suites", PASS)
        r.record("b", "cli", FAIL, "boom\nwhy")
        self.assertFalse(r.is_go())
        self.assertEqual([s["id"] for s in r.attention()[0]], ["b"])

    def test_steps_are_grouped_into_categories_and_coverage_is_separate(self):
        r = Results()
        for sid, group in (("a", "suites"), ("b", "executions"), ("c", "cli"), ("d", "coverage"), ("e", "custom")):
            r.record(sid, group, PASS)
        cats = {c: [s["id"] for s in v] for c, v in r.by_category().items()}
        self.assertEqual(cats["CLI commands"], ["b", "c"])
        self.assertEqual(cats["Coverage gate"], ["d"])
        self.assertEqual(cats["Test suites"], ["a"])
        self.assertEqual(cats["Custom tests"], ["e"])


if __name__ == "__main__":
    unittest.main()
