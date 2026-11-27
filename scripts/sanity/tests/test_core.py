import os
import stat
import subprocess
import tempfile
import time
import unittest

from fakes import FakeTh, make_session  # noqa: F401  (also sets sys.path)
from th_sanity import prompts
from th_sanity.dut import DutError, DutManager, list_processes, parse_ps
from th_sanity.prompts import PromptDriver, answer_for, qr_discriminator
from th_sanity.results import FAIL, NOT_RUN, PASS, Results

def ps_usable() -> bool:
    try:
        list_processes()
        return True
    except DutError:
        return False


PS_OK = ps_usable()
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

    def test_transcript_starts_with_the_command_then_notes_then_the_output(self):
        transcript = os.path.join(self.tmp, "t.out")
        args = self.script('echo "hello from th-cli"')
        self.driver.run(args, [], 20, idle=2, transcript=transcript, transcript_notes=["DUT started with: /apps/x --wifi"])
        lines = open(transcript).read().splitlines()
        self.assertEqual(lines[0], "$ bash " + args[1])
        self.assertEqual(lines[1], "# DUT started with: /apps/x --wifi")
        self.assertEqual(lines[2], "")
        self.assertIn("hello from th-cli", lines[3:])

    def test_the_command_header_is_not_part_of_the_returned_output(self):
        out, _, _ = self.driver.run(self.script('echo only-this'), [], 20, idle=2,
                                    transcript=os.path.join(self.tmp, "t.out"))
        self.assertNotIn("$ bash", out)
        self.assertIn("only-this", out)

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

    # --- processes that are already running (never use a real app name: tests must not touch real apps)
    def spawn(self, name=None):
        """A process whose command line starts with the fake binary path, like a compiled app would."""
        path = os.path.join(self.apps, name or self.NAME)
        with open(path, "w") as f:
            f.write('#!/bin/bash\nexec -a "$0" sleep 60\n')
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
        proc = subprocess.Popen([path])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        time.sleep(0.5)  # let exec replace the shell so `ps` shows the final command line
        return proc

    @unittest.skipUnless(PS_OK, "`ps` is not usable here")
    def test_find_running_matches_the_executable_only(self):
        proc = self.spawn()
        decoy_file = os.path.join(self.apps, "notes", self.NAME)
        os.makedirs(os.path.dirname(decoy_file))
        open(decoy_file, "w").write("x")
        decoy = subprocess.Popen(["tail", "-f", decoy_file])  # mentions the path but is not the app
        self.addCleanup(lambda: (decoy.kill(), decoy.wait()))
        time.sleep(0.3)
        found = self.manager().find_running()
        self.assertEqual([pid for pid, _ in found], [proc.pid])
        self.assertIn(self.NAME, found[0][1])

    @unittest.skipUnless(PS_OK, "`ps` is not usable here")
    def test_find_running_is_empty_when_nothing_runs(self):
        self.assertEqual(self.manager().find_running(), [])

    @unittest.skipUnless(PS_OK, "`ps` is not usable here")
    def test_find_running_ignores_the_managers_own_child(self):
        self.binary('echo "mDNS service published: _matterc._udp"; exec -a "$0" sleep 60')
        m = self.manager()
        try:
            m.start("d", fresh=True)
            time.sleep(0.3)
            self.assertEqual(m.find_running(), [])
        finally:
            m.stop()

    @unittest.skipUnless(PS_OK, "`ps` is not usable here")
    def test_stop_kills_a_leftover_app_but_not_a_process_that_only_mentions_it(self):
        proc = self.spawn()
        decoy_file = os.path.join(self.apps, "notes", self.NAME)
        os.makedirs(os.path.dirname(decoy_file))
        open(decoy_file, "w").write("x")
        decoy = subprocess.Popen(["tail", "-f", decoy_file])
        self.addCleanup(lambda: (decoy.kill(), decoy.wait()))
        m = self.manager()
        m.stop()
        proc.wait(timeout=5)
        self.assertIsNotNone(proc.returncode)
        self.assertIsNone(decoy.poll())  # still running
        self.assertEqual(m.find_running(), [])

    # --- the same logic with an injected process list (works without `ps`)
    def with_processes(self, rows, binary="chip-all-clusters-app"):
        return DutManager({"d": {"binary": binary}}, self.apps, self.logs, lister=lambda: rows)

    def test_running_apps_are_found_by_the_start_of_their_command_line(self):
        rows = [(10, "/home/u/apps/chip-all-clusters-app --wifi"), (11, "./chip-all-clusters-app"),
                (12, "tail -f /home/u/apps/chip-all-clusters-app"), (13, "vim chip-all-clusters-app.sh"),
                (14, "/x/not-chip-all-clusters-app"), (15, "python3 run_sanity.py --manifest chip-all-clusters-app")]
        found = self.with_processes(rows).find_running()
        self.assertEqual([pid for pid, _ in found], [10, 11])

    def test_the_harness_itself_and_its_own_child_are_never_reported(self):
        rows = [(os.getpid(), "chip-all-clusters-app"), (777, "chip-all-clusters-app"), (778, "chip-all-clusters-app")]
        m = self.with_processes(rows)
        m.proc = type("P", (), {"pid": 777})()
        self.assertEqual([pid for pid, _ in m.find_running()], [778])

    def test_a_failing_process_listing_is_an_error_not_an_empty_list(self):
        def boom():
            raise DutError("cannot list running processes with `ps`: nope")
        with self.assertRaises(DutError):
            DutManager({"d": {"binary": "x"}}, self.apps, self.logs, lister=boom).find_running()

    def test_parse_ps(self):
        text = "  101 /sbin/init splash\n 2002 ./chip-all-clusters-app --wifi\nnot a row\n"
        self.assertEqual(parse_ps(text), [(101, "/sbin/init splash"), (2002, "./chip-all-clusters-app --wifi")])

    def test_missing_binary_and_unknown_profile(self):
        m = self.manager()
        with self.assertRaises(DutError):
            m.start("d")
        with self.assertRaises(DutError):
            m.start("nope")


# Lines taken from `ps -aux` on the TH host (Raspberry Pi), reduced to the `ps -eo pid=,args=` form the harness reads.
REAL_PS = """
    1 /sbin/init fixrtc
  825 /usr/bin/python3 /usr/share/unattended-upgrades/unattended-upgrade-shutdown --wait-for-signal
 1051 /usr/bin/dockerd -H fd:// --containerd=/run/containerd/containerd.sock
 4621 -bash
 6234 /home/ubuntu/.vscode-server/cli/servers/Stable-2242/server/node /home/ubuntu/.vscode-server/cli/servers/Stable-2242/server/out/server-main.js --connection-token=remotessh
527097 traefik traefik --providers.docker --providers.docker.constraints=Label(`traefik.constraint-label-stack`, `chip-test-harness.com`) --providers.docker.exposedbydefault=false
527273 python3 ./app/main.py
529165 postgres: postgres app fd00:6d61:7474:6572::4(59086) idle
730942 ./chip-all-clusters-app
731306 sleep 180
731529 ps -aux
"""


class RealHostProcessList(unittest.TestCase):
    def test_only_the_chip_app_in_a_real_process_list_is_found(self):
        m = DutManager({"all-clusters": {"binary": "chip-all-clusters-app"}}, "/nonexistent", "/nonexistent",
                       lister=lambda: parse_ps(REAL_PS))
        self.assertEqual(m.find_running(), [(730942, "./chip-all-clusters-app")])

    def test_the_backend_running_in_docker_is_not_mistaken_for_a_dut(self):
        m = DutManager({"all-clusters": {"binary": "chip-all-clusters-app"}}, "/nonexistent", "/nonexistent",
                       lister=lambda: parse_ps(REAL_PS))
        self.assertNotIn(527273, [pid for pid, _ in m.find_running()])


# `th-cli --version` as printed on the TH host
VERSION_OUTPUT = """th-cli: A CLI tool for Matter Test Harness

Version: 0.1.0
CLI SHA: d53ca722
version: v2.16-beta3+winter2026
sha: 4606147-local
sdk_sha: 5741ad7
sdk_docker_tag: 5741ad7fa624f34bb032eb6a955485f4ad0ed253
db_revision: b7e2f1a9c3d4
"""


class TestHarnessVersion(unittest.TestCase):
    def test_the_th_version_is_the_backend_line_not_the_cli_line(self):
        from th_sanity.versions import describe, parse_versions
        v = parse_versions(VERSION_OUTPUT)
        self.assertEqual(v["th"], "v2.16-beta3+winter2026")
        self.assertEqual(v["cli"], "0.1.0")
        self.assertEqual(describe(v), "v2.16-beta3+winter2026 (sha 4606147-local, SDK 5741ad7)")

    def test_unreadable_output_is_reported_as_unknown(self):
        from th_sanity.versions import describe, parse_versions
        self.assertEqual(parse_versions("Not able to retrieve versions from server"), {})
        self.assertIn("unknown", describe({}))

    def test_a_label_must_not_claim_another_version(self):
        from th_sanity.versions import label_matches
        th = "v2.16-beta3+winter2026"
        self.assertFalse(label_matches("v2.17-beta1", th))       # the mislabelled run
        self.assertTrue(label_matches("v2.16-beta3", th))
        self.assertTrue(label_matches("2.16-beta3+winter2026", th))
        self.assertTrue(label_matches("v2.17-beta1", "v2.17-beta1+spring2027"))
        self.assertFalse(label_matches("", th))


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
