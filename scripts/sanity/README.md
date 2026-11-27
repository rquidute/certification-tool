# TH release sanity harness

A repeatable sanity run for a new Matter Test Harness (TH) version. It drives the real `th-cli` against a
running backend and a real DUT app (`chip-all-clusters-app`), covers **every suite with one test**, exercises
**every `th-cli` command**, tests **custom (side-loaded) YAML and Python tests**, and ends with a report and a
**GO / NO-GO** verdict.

Standard library only: nothing to install on the TH host.

> **Scope.** What gets tested is defined in `scripts/sanity/manifest.json`; edit it to fit the release you are validating
> (see [Scope of the test](#scope-of-the-test-manifestjson)).
>
> **What a GO means.** One test per suite is a *smoke test* of the TH: loaders, runner, prompts, CLI and the
> custom-test flow all work. It is **not** test-case coverage. It will catch a broken suite or runner; it will not
> catch a regression inside one specific test case.

---

## Quick start

On the TH host (the machine that has `th-cli`, the backend and `~/apps/chip-all-clusters-app`):

```bash
cd ~/certification-tool

# 1. See which suites / tests exist, to fill or check scripts/sanity/manifest.json
python3 scripts/sanity/run_sanity.py --list-suites

# 2. Safe rehearsal: reads only, starts/stops/deletes nothing
python3 scripts/sanity/run_sanity.py --dry-run

# 3. The real run
python3 scripts/sanity/run_sanity.py --version-label v2.17-beta1
```

The real run asks you to **close the TH web UI** and press Enter first (see [Safety behaviour](#safety-behaviour)).
Exit code: `0` GO, `1` NO-GO, `2` refused to start or cancelled before anything was changed.

### Prerequisites

| Needed | Why |
|---|---|
| `th-cli` on `PATH` (or `th_cli` in the manifest) | every step goes through it |
| TH backend running and **idle** | the test engine runs one test at a time |
| `chip-all-clusters-app` in `apps_dir` (default `~/apps`) | the DUT for most suites |
| Python 3 | the harness itself |
| TH web UI **closed**, no other runs | the TH sends prompts to every connected client |

---

## Scope of the test: `manifest.json`

**`scripts/sanity/manifest.json` defines the scope of the sanity run. Edit it to change what is tested: no code change
is needed.** The shipped file is a starting point; add, remove or swap entries to fit the release you are validating.

| What you control in the manifest | Key |
|---|---|
| which suites are tested, which test represents each, which DUT it uses, fresh or reused credentials | `suites[]` |
| suites that are deliberately **not** tested, with the reason (shown in the report as `excluded-…`) | `excluded[]` |
| which SDK tests are side-loaded to exercise the `<suite>-custom` suites | `custom[]` |
| which test is aborted while running, and after how many seconds | `abort_run` |
| DUT apps: binary, arguments, readiness patterns | `dut_profiles` |
| how interactive prompts are answered | `default_prompt_answers`, and `prompt_answers` / `text_answers` per entry |
| which leftover projects are cleaned up before a run | `cleanup` |
| where `th-cli`, the apps and the TH checkout live, and the per-run timeout | `th_cli`, `apps_dir`, `th_root`, `default_run_timeout_s` |

The coverage gate keeps the scope honest: if the TH has a suite that is neither in `suites` nor in `excluded`, the run
is NO-GO. Dropping a suite from the scope therefore has to be written down, with a reason.

What is **not** in the manifest, because it is code: the list of `th-cli` commands that get exercised
(`th_sanity/phases/cli_commands.py` and `executions.py`), the order of the phases (`PHASES` in `cli.py`) and the verdict
rules. See [Extending it](#extending-it).

**Scope in the shipped manifest**

One test per suite (`suites[]`):

| Step | Suite | Test | DUT |
|---|---|---|---|
| `yaml-chip-tool` | FirstChipToolSuite | `TC-ACE-1.1` | all-clusters |
| `yaml-manual` | FirstManualSuite | `TC-ACT-2.1` | none (every prompt accepted as PASS) |
| `onboarding-payload` | OnboardingPayloadTestSuite | `TC-DD-1.1` | all-clusters (QR code taken from the DUT's log) |
| `python-auto-commissioning` | Python Testing Suite - Auto commissioning | `TC_ACE_1_3` | all-clusters |
| `python-mandatories` | Python Testing Suite - Mandatories | `TC_IDM_10_2` | all-clusters |
| `python-no-auto-commissioning` | Python Testing Suite - No auto commissioning | `TC_CGEN_2_5` | all-clusters |
| `python-old-format` | Python Testing Suite - Old script format | `TC_ACE_1_2` | all-clusters |
| `python-old-format-reuse-credentials` | Python Testing Suite - Old script format | `TC_ACE_1_2` | all-clusters, **reuse** credentials |

Custom (side-loaded) tests (`custom[]`), each copied from the SDK folder into the custom folder:

| Step | Kind | Test | Must appear in | |
|---|---|---|---|---|
| `yaml-automated` | YAML | `TC-ACE-1.1` | FirstChipToolSuite-custom | run |
| `yaml-manual` | YAML | `TC-ACT-2.1` | FirstManualSuite-custom | run |
| `yaml-simulated` | YAML | `TC-CC-3.4` | FirstAppSuite-custom | load only |
| `python-auto-commissioning` | Python | `TC_ACE_1_3` | Python Testing Suite - Auto commissioning-custom | run |
| `python-no-auto-commissioning` | Python | `TC_CGEN_2_5` | Python Testing Suite - No auto commissioning-custom | run |
| `python-old-format` | Python | `TC_ACE_1_2` | Python Testing Suite - Old script format-custom | run |

Excluded on purpose: `FirstAppSuite` (simulated YAML needs an external controller to commission the chip-app).
Abort-while-running uses `python-auto-commissioning`, aborted 8 s after the runner reports `running`.

Typical edits: swap a suite's `test` for a faster or more representative one (`--list-suites` shows valid ids), add a
suite that appeared in a new TH version, exclude a suite you cannot automate (with a `reason`), or point `th_root` /
`apps_dir` at a different checkout. After editing, `python3 scripts/sanity/run_sanity.py --dry-run` validates the
manifest against the live TH without running anything.

---

## Command-line options

| Option | Effect |
|---|---|
| `--manifest PATH` | manifest to use (default: `scripts/sanity/manifest.json`) |
| `--out-dir DIR` | where run folders go (default: `sanity_reports` in the current directory) |
| `--version-label TEXT` | TH version string for the report header and the run-folder name; if omitted, the output of `th-cli --version` is used |
| `--dry-run` | coverage gate + read-only CLI commands only. Starts no DUT, deletes nothing, needs no confirmation |
| `--list-suites` | print live suites (id, size, sample test ids) and exit; creates no folder |
| `--yes` | skip the "close the TH UI" confirmation (unattended runs; without a terminal it is required) |
| `--kill-existing` | allow the harness to stop a DUT app that is already running (otherwise it refuses to start) |
| `--only ID[,ID...]` | **focused run** for iterating on a failing suite: runs only those `suites[]` entries (plus the entries they `depends_on`) in a throwaway project and skips the CLI, custom and execution phases. The report is marked *not a release verdict*. Example: `--only python-mandatories` |
| `--no-preclean` | do not delete leftover `sanity-*` projects from earlier sessions |
| `--color auto\|always\|never` | colour output; `auto` = only on a terminal and when `NO_COLOR` is unset. Use `always` with `tee`, then `less -R` |

---

## What a run does

```
 start-up gates ─▶ pre-flight ─▶ phase 1 ─▶ phase 2 ─▶ phase 3 ─▶ phase 4 ─▶ phase 5 ─▶ cleanup ─▶ report
```

**Start-up gates** (nothing is changed if any of them stops the run, exit code 2)
1. A DUT app already running → refuse, naming the pid and command line (unless `--kill-existing`).
2. "Close the TH UI" confirmation (skipped by `--dry-run` / `--yes`).

**Pre-flight**: delete projects left by earlier sanity sessions (regex-matched, capped; see [manifest](#manifest-reference)).
With `--kill-existing`, a running DUT app is stopped here and recorded as the step `stop-existing-dut`.

| # | Phase | What it checks |
|---|---|---|
| 1 | **CLI commands** | `--version`, `test-runner-status`, `available-tests` (4 variants), `project` list/create/update/edit/applicable-tests/export/import, `rescan-tests`. With the engine idle: `abort-testing` must be **refused** (HTTP 409 "not active") and the engine must stay idle. Uses a throwaway project |
| 2 | **Coverage gate** | every suite that exists in the TH is either in the manifest or explicitly excluded; every manifest entry points at a suite/test that exists. A new suite nobody added fails the run |
| 3 | **Test suites** | one representative test per suite, DUT started `fresh` or `reuse` as each entry says |
| 4 | **Custom tests** | SDK tests are copied into the custom folders, **one** rescan, each must appear in its expected `<suite>-custom` suite, runnable ones are run, files removed, **one** rescan, each must be gone |
| 5 | **Executions and runs** | `test-run-execution` list/show/log/pics-export/rename/archive/unarchive/export/import/delete, `project logs`, **repeat** of a previous run (new execution, same final state), and **abort of a run that is actually running** (abort accepted, `th-cli` exits, engine returns to idle, execution ends `cancelled`) |

**Cleanup** (always, except `--dry-run`): abort any run, stop the DUT, delete the throwaway projects (not scoped to what
the harness started, see limitations). A second Ctrl-C
does not abandon the remaining cleanup steps.

### Verdict rules

* A step is **PASS** only if it ran *and* matched its expectation.
* **FAIL**: it ran and did not match, or infrastructure broke (DUT did not start, unhandled prompt, timeout, no results line).
* **NOT_RUN**: it did not run. This makes the verdict **NO-GO** unless the step id starts with `excluded-`
  (an explicit, reasoned exclusion in the manifest).
* **GO** = no FAIL and no unexplained NOT_RUN.
* **Exit code 0 is not enough for a pass.** `th-cli` exits 0 when nothing failed, which includes a test that was skipped or
  not applicable. A test only passes if the `Results:` summary says `passed`; anything else is reported as
  "exit code 0 but no test passed".
* **The label must match the TH.** The report records what `th-cli --version` says the TH under test is (backend version,
  sha, SDK sha). If `--version-label` is given and does not match that version, the step `th-version-matches-label`
  fails, because the report would claim a version that was not tested.
* `th-cli` returns exit code 1 for both "a test failed" and "infrastructure error", so the harness tells them apart:
  a `Results:` line means the test ran; none means infrastructure (reported as `INFRA: …`).

---

## Output

Each run gets its own folder; every step keeps its own logs in a subfolder.

```
sanity_reports/
├── latest -> sanity_20261127_153750_v2.17-beta1        symlink to the newest run
└── sanity_20261127_153750_v2.17-beta1/
    ├── report.md                                        summary by category + "Needs attention"
    ├── report.json                                      same data, machine readable
    ├── suites/run-yaml-chip-tool/
    │   ├── transcript.out                               "$ <th-cli command>" first, then what th-cli printed, live
    │   ├── dut.log                                      the DUT app's log for this step
    │   └── th-cli-run.log                               th-cli's own run log, copied from ./run_logs
    ├── custom/custom-rescan-add/cli-output.txt          full rescan output
    └── cli/execution-log/
        ├── cli-output.txt                               the command, exit code, duration and FULL output
        └── exec_log.txt                                 file the command exported or downloaded (-o)
```

Every step that runs a `th-cli` command (CLI commands, rescans, project and execution commands, cleanup deletes,
the abort request) keeps `cli-output.txt` in its folder: the command line (shell-quoted, so it can be pasted back),
the exit code, the duration and the **full** output, not the 400 characters shown on the console when a step fails.
Test runs keep `transcript.out` instead; its first line is the command (`$ th-cli run-tests -t … `), followed by a
`# DUT started with: …` line when the test had a DUT, a blank line and then the live output. Internal read-only queries that are not steps (e.g. polling the runner state)
are not saved.

Every step also records **the command line it ran** (shell-quoted, so it can be pasted back) and, for tests, **how the
DUT app was started**. They are in `report.json` (`command`, `dut_command`), in a *Command* column of `report.md`, in the
*Needs attention* section, and on the console right under a failing step.

When a test fails (not an infrastructure error), the step also carries a digest of the failing lines from the `th-cli`
output (`[FAILED]` / `[ERROR]` tree entries, `ERROR` log lines, tracebacks), so the reason is visible without opening
`transcript.out`.

The console and `report.md` group results into **Test suites**, **Custom tests**, **CLI commands**, **Coverage gate**
and **Harness**. Coverage checks are counted separately: they are a pre-flight gate, not test results.

While a test runs you get a status line every 30 s: elapsed time, phase/suite counter and the last line `th-cli`
printed. Every line starts with the total elapsed time.

---

## Safety behaviour

The harness changes real things (kills processes, deletes projects, copies files into the TH). These are the guards:

| Situation | Behaviour |
|---|---|
| DUT app already running | **Refuses to start** (exit 2) and names it. `--kill-existing` opts in; the stop is a recorded step and fails if the app is still running (e.g. owned by root). It is **not restored** afterwards |
| Process list cannot be read (`ps` failed) | Refuses to start instead of assuming nothing runs |
| TH UI open | Asks you to close it and waits for Enter (n / Ctrl-C / Ctrl-D cancels, nothing changed) |
| Test engine busy at start | `runner-idle-before-abort` fails and the idle-abort check is skipped, so that check never aborts a run it did not start (the final cleanup does, see [limitations](#known-limitations-and-open-findings)) |
| Leftover projects | Only names matching `cleanup.project_name_regex` are deleted; refuses if more than `cleanup.max_delete` match; never in `--dry-run` |
| Custom folder already has the fixture file | Step is skipped (`NOT_RUN`); a user file is never overwritten or deleted |
| Custom YAML test with the same id as the SDK test | Not run: `th-cli run-tests -t <id>` cannot choose between them, so a PASS could silently be the SDK test |
| Unknown interactive prompt | The run is stopped and the prompt text is shown. The harness never guesses an answer |
| Timeout / idle / unhandled prompt | `th-cli` is killed **and** `abort-testing` is sent, so the backend run does not block the next step |
| Ctrl-C | Cleanup still runs (abort, stop DUT, delete own projects) and the report is written |

Matching a DUT process looks at the **start of the command line** (`./chip-all-clusters-app …`,
`/home/u/apps/chip-all-clusters-app …`). `tail -f …/chip-all-clusters-app` or an editor that mentions the path is
neither reported nor killed.

---

## Manifest reference

`manifest.json` is the only file you edit to change *what* is tested (see [Scope of the test](#scope-of-the-test-manifestjson)). Keys starting with `_` are ignored.

```jsonc
{
  "th_cli": "th-cli",                  // executable
  "th_root": "~/certification-tool",   // used in custom fixture paths as {th_root}
  "apps_dir": "~/apps",                // where DUT binaries live
  "default_run_timeout_s": 900,        // per test run

  "dut_profiles": {
    "all-clusters": {
      "binary": "chip-all-clusters-app",
      "args": [],                                            // command-line arguments
      "ready_marker": "mDNS service published: _matterc\\._udp",   // regex; fresh start (optional, this is the default)
      "ready_marker_reuse": "mDNS service published: _matter\\._tcp", // regex; reuse start (optional, default shown)
      "ready_timeout_s": 60,
      "quiet_s": 5,                                          // reuse only: log quiet this long = booted
      "wipe": "/tmp/chip_* /tmp/repl-storage.json"           // factory reset for a fresh start; "" disables
    }
  },

  "suites": [ /* one entry per suite, see below */ ],
  "excluded": [ { "suite": "FirstAppSuite", "reason": "…" } ],
  "abort_run": { "from": "python-auto-commissioning", "after_s": 8 },
  "cleanup": { "project_name_regex": "…", "max_delete": 25 },
  "default_prompt_answers": [ /* rules, see below */ ],
  "custom": [ /* fixtures, see below */ ]
}
```

### `suites[]` entry

| Key | Meaning |
|---|---|
| `id` | step name (`run-<id>`) and key for `depends_on` / `abort_run.from` |
| `suite`, `test` | suite `public_id` and test case `public_id`, exactly as `--list-suites` prints them (YAML ids look like `TC-ACE-1.1`, Python ids like `TC_ACE_1_3`) |
| `dut` | DUT profile name, or `null` for tests that need no DUT |
| `dut_start` | `fresh` (default: wipe state, factory-reset) or `reuse` (keep the app's saved credentials) |
| `expect` | `pass` (default) or `fail` (a test that is *supposed* to fail) |
| `depends_on` | another entry `id`; skipped (`NOT_RUN`) if that one did not pass |
| `prompt_answers`, `text_answers` | answers for this step only, tried before the defaults |
| `test_args` | inline test parameters appended to `th-cli run-tests` after `--`, e.g. `["--int-arg", "PIXIT.CGEN.TCRevision:1"]`. Needed by tests that read PIXIT values; the SDK script's own header comment lists the values its CI uses |
| `config` | optional run-config file (path relative to the manifest), passed as `-c` |

A suite missing from `suites` **and** `excluded` fails the coverage gate. Entries still containing `TODO` fail it too.

### Prompt answers

`th-cli` asks four kinds of questions; the harness answers them through a pseudo-terminal:

| Prompt | How it is answered |
|---|---|
| Option prompt (`1: YES  2: NO … Please enter a number`) | first rule whose `match` (regex on the prompt text) hits **and** whose `choose` (regex on an option label) hits an option; step rules first, then `default_prompt_answers` |
| Text prompt (`Enter value:`) | `text_answers` rule: `{"match": "…", "value": "literal-or-token"}`. Tokens: `@dut_qr` = the `SetupQRCode: [MT:…]` the DUT printed at startup, `@qr_disc` = the discriminator (hex) decoded from that QR code |
| File upload (`Enter the path to the file to upload`) | always Enter (skip) |
| Anything else | run stopped, prompt shown, `abort-testing` sent |

Defaults shipped in the manifest: *"Should the DUT be commissioned"* → YES; *"reuse the previous commissioning"* → NO
(a freshly started DUT cannot reuse old credentials; the reuse step overrides it to YES). A catch-all rule
`{"match": ".", "choose": "(?i)^(pass|yes|ok|done|continue|y)$"}` accepts every manual-test prompt as PASS and never
picks FAIL.

### `custom[]` fixture

An SDK test is copied into the custom folder to exercise the matching `<suite>-custom` suite.

| Key | Meaning |
|---|---|
| `id`, `kind` | step name, `yaml` or `python` |
| `src`, `dest_dir` | file to copy and the custom folder (`{th_root}` is expanded) |
| `test` | the test's id (matched loosely: punctuation and case ignored) |
| `expect_suite` | suite it must appear in after the rescan, e.g. `Python Testing Suite - Auto commissioning-custom` |
| `dut`, `prompt_answers`, `text_answers`, `test_args`, `expect` | as for suites |
| `run: false`, `run_note` | only check that it loads (used for the simulated YAML suite) |

User `…-custom` suites that already exist (the user's own side-loaded files) count as exercised when a fixture
targets that suite; otherwise they are listed as `excluded-…` (NOT_RUN, allowed).

### DUT readiness

The harness starts the DUT itself (the TH backend never does) and waits until it is ready:

* **fresh** start: factory reset (`wipe`), then waits for `mDNS service published: _matterc._udp`, the "I am
  commissionable" announcement.
* **reuse** start: nothing is wiped, so the device keeps its fabric and does **not** announce `_matterc._udp`. It is
  ready when `mDNS service published: _matter._tcp` appears, or when its log has been quiet for `quiet_s`.
* A start that fails prints the last 15 lines of the DUT log.

---

## Architecture

```
scripts/sanity/
├── run_sanity.py            14-line entry point; adds its folder to sys.path and calls th_sanity.cli.main
├── manifest.json            what to test
├── tests/                   unit tests (stdlib unittest)
└── th_sanity/
    ├── cli.py               arguments, start-up gates, phase order, cleanup, report
    ├── session.py           Session: the one object phases receive (state + services)
    ├── results.py           Results: step ledger, console lines, verdict, report categories
    ├── console.py           colours, progress bars, symbols (pure functions + one switch)
    ├── thcli.py             ThCli: wrapper around the th-cli executable
    ├── prompts.py           PromptDriver: pty runner that answers th-cli's prompts
    ├── dut.py               DutManager: start/stop the DUT, readiness, find running apps
    ├── runtest.py           run_test(): one test case through `th-cli run-tests`
    ├── digest.py            failing lines of a th-cli run, shown with a failed test
    ├── report.py            report.md / report.json, console summary, `latest` link
    └── phases/
        ├── preclean.py      stale-project removal, stop-existing-dut, final cleanup
        ├── cli_commands.py  phase 1
        ├── coverage.py      phase 2 (+ load_live, shared with custom)
        ├── suites.py        phase 3
        ├── custom.py        phase 4
        ├── executions.py    phase 5a: execution commands, repeat
        └── abort.py         phase 5b: abort while running
```

### How the pieces fit

```
        cli.main ──builds──▶ Session ──owns──▶ ThCli        (talks to th-cli)
            │                   │      ├────▶ DutManager    (starts/stops the DUT app)
            │                   │      ├────▶ PromptDriver  (runs th-cli in a pty, answers prompts)
            │                   │      └────▶ Results       (records steps, prints, decides GO/NO-GO)
            │                   └─ state: project_id, exec_ids, exec_meta, live suites
            ▼
   PHASES = [(title, fn), …]   each fn(session) records steps through session.record / session.cli_step
            │
            ▼
      report.write_report(session)
```

* **`Session`** holds the manifest, the services above and the state phases share (the throwaway project id, the
  executions produced by passed suite runs, the suites currently live in the TH). Phases take it as their only argument
  and never use globals. Services can be injected, which is how the tests run without `th-cli`, a backend or a DUT.
* **`Results`** is the single place that records and prints steps. `record(id, group, status, detail)` stores the step
  and prints it; `group` decides the report category and the log subfolder.
* **`ThCli`** wraps `th-cli`: `run` (text), `json` (parsed), plus `runner_state`, `wait_state`, `exec_state`,
  `last_execution`, `exec_set`, `projects`, `find_project`. The CLI prints enum reprs (`TestRunnerState.IDLE`), which
  `enum_name` normalises to `idle`.
* **`PromptDriver.run`** starts `th-cli` in a pty and loops: read output → recognise a prompt → answer → repeat. It ends
  on EOF (a pty keeps looking readable after the process exits, so EOF must be checked explicitly), timeout, or idle
  (180 s with neither output nor growth of `th-cli`'s own log file). On any non-clean end it sends `abort-testing`.
* **`run_test`** = start DUT (if any) → `PromptDriver.run` → classify (`Results:` line, exit code, DUT still alive) →
  record → copy `th-cli`'s run log into the step folder → return the new execution id.
* **Phases** are plain functions, so a phase can be tested in isolation by giving it a `Session` with fakes.

### Hardcoded timings

| What | Value |
|---|---|
| status line while a test runs | every 30 s |
| idle limit while a test runs | 180 s without output or log growth |
| DUT ready timeout | `ready_timeout_s` (60 s) |
| abort test: wait for `running` / join after abort / wait for `idle` | 90 s / 120 s / 60 s |
| per-run timeout | `default_run_timeout_s` (900 s) |

---

## Extending it

**Add a test to an existing suite coverage** → edit `manifest.json` only: add a `suites[]` entry (or change `test`).
`--list-suites` shows valid ids. No code change.

**A new suite appeared in the TH and the run is NO-GO** → the coverage gate did its job. Add it to `suites[]` or to
`excluded[]` with a reason.

**A step stops on `UNHANDLED PROMPT`** → the failure shows the prompt text. Add a rule to that step's
`prompt_answers` (or to `default_prompt_answers` if it applies everywhere).

**A new DUT app** (e.g. the camera app) → add a `dut_profiles` entry (binary, args, readiness regexes) and reference it
from a suite's `dut`.

**A new kind of check** → add a module under `phases/` with `run(s: Session)`, record steps with `s.record(...)` or
`s.cli_step(...)`, and register it in `PHASES` in `cli.py`. Use a new `group` name only if it needs its own report
category (add it to `CATEGORY` in `results.py` and `STEP_DIRS` in `session.py`).

**A new prompt type** → add its recognition to `PromptDriver.run` in `prompts.py` and a test in `tests/test_core.py`.

---

## Tests

```bash
python3 -m unittest discover -s scripts/sanity/tests
```

Stdlib `unittest`, about 30 s, **no `th-cli`, backend or DUT needed**. They cover prompt answering, the pty driver
(bash scripts behaving like `th-cli`), the DUT manager (fake binaries), the verdict, project cleanup, the coverage gate,
the custom-test flow, repeat, abort-while-running, the report and the start-up gates. `tests/fakes.py` has the test
doubles (`FakeTh`, `FakePrompts`, `FakeDut`).

Tests that spawn real processes skip themselves when `ps` is not usable. The fake DUT binaries have unique names and
the factory-reset `wipe` is disabled in tests, so they never touch a real app or `/tmp/chip_*`.

---

## Known limitations and open findings

* **One test per suite is a smoke test**, not coverage (see the top of this file).
* **Hardware-dependent suites** (BLE, Thread, Wi-Fi, NFC pairing) cannot run against a plain all-clusters app.
  Exclude them with a reason; they will show as `NOT_RUN`, never as PASS. The simulated YAML suite (`FirstAppSuite`)
  needs an external controller to commission the chip-app and is excluded the same way.
* **`th-cli rescan-tests` does not pick up custom YAML tests (observed on TH v2.16-beta3+winter2026).** After copying three
  YAML files into the custom folder, the rescan reported 5 collections, no Custom YAML collection, and listed only the
  `.py` files under "Custom files". The code explains it: `support/yaml_tests/__init__.py` builds the custom YAML
  collection once, when the module is imported, and a rescan only re-imports already-loaded modules. The user guide's
  side-load steps cover "Yaml or Python" scripts and say no backend restart is needed. The three YAML custom fixtures
  fail until this is fixed or the guide is corrected; the failure message says so.
* **`execution-pics-export`** passes on the documented 404 "No PICS were used"; the export path itself is then **not
  exercised** (the step detail says so).
* **TC-DD-1.1 errors on this TH with `'Settings' object has no attribute 'ENABLE_CONTAINER_LOGS'`** right after the QR code
  is entered (the harness answers with the DUT's real code). On `release/v2.16-beta3+winter2026` that setting exists
  (`config.py`) and `container_manager.py` reads it; commit `fed8dd8` (#414) removed both on the v2.17 work. The error
  means the running backend has the reader without the setting, i.e. mixed v2.16 / v2.17 code or a stale image. Check
  the deployment (`th-cli --version`, the backend image, the checkout) before blaming the test.
* **TC_IDM_10_2 fails with "Problems with conformance"** on `chip-all-clusters-app`: it is the device conformance test, it
  runs correctly, and the all-clusters app is not meant to be conformant. It is a poor representative for the Mandatories
  suite on this DUT; pick another test from that suite (`--only python-mandatories` makes trying candidates quick).
* **TC_CGEN_2_5 needs PIXIT parameters** (`PIXIT.CGEN.FailsafeExpiryLengthSeconds`, `RequiredTCAcknowledgements`,
  `TCRevision`); without them it dies with a `KeyError`. The manifest now passes them with `test_args`. Whether the
  all-clusters app supports Terms and Conditions (and whether the project's PICS enable `CGEN.S.F00`) is not known; a skipped
  test is reported as FAIL, not as a pass.
* **File-upload prompts are always skipped.** A manual test that needs a real file will fail visibly.
* **Reuse readiness** was confirmed on the Pi: a device that kept its fabric announces `_matter._tcp`, not
  `_matterc._udp`. If a reuse start ever fails, read that step's `dut.log` (the quiet-log fallback exists for other builds).
* **`th-cli project logs` times out on a project with several executions** (`ReadTimeout` after about 6 s). All `project`
  commands share a helper that builds the HTTP client with the default timeout, while rescan, execution import/export
  and abort set 30-120 s; `project logs` even prints "this may take a while". Suspected CLI bug; the step fails on purpose.
* **`th-cli` can report an incomplete run** ("only 0 of 1 selected test case(s) have results", "backend couldn't send
  all of the run's log records") even when the test shows `[PASSED]`. The harness counts that as FAIL (INFRA). It looks
  like a TH/CLI streaming flake, not a harness problem, and it is deliberately not retried or hidden.
* **The DUT app is not restored.** `--kill-existing` stops an app that was already running; the harness leaves no DUT
  running at the end.
* **Final cleanup is not scoped to what the harness started.** It always sends `abort-testing` and stops any DUT app
  matching a profile, even if the harness never started one (for example when the run failed in phase 1). Known, not yet changed.

---

## Troubleshooting

| Symptom | Likely cause / what to do |
|---|---|
| Exit 2, "A DUT app is already running" | stop it (Ctrl-C in its terminal) or pass `--kill-existing` |
| Exit 2, "Not running in a terminal" | close the UI yourself and pass `--yes` |
| `runner-idle-before-abort` FAIL | a test run is active on the TH; wait for it or `th-cli abort-testing` |
| `UNHANDLED PROMPT` / `UNHANDLED TEXT PROMPT` | add an answer rule (see above); the failure shows the exact prompt |
| "no th-cli output and no log growth for 180s" | `th-cli` is waiting on something the harness does not recognise; the last output is in the failure and in `transcript.out` |
| DUT "not ready within 60s" | read `dut.log` in the step folder; check the interface/arguments and the readiness regex for that start mode |
| `manifest-… still has TODO` / `not found in TH` | fix the manifest entry from `--list-suites` |
| Leftover `sanity-harness-…` projects | the next run deletes them (pre-flight); or `th-cli project delete -i <id> -y` |
| A custom fixture file is left in a custom folder after a crash | delete it by hand; the harness refuses to touch a file that already exists there |
| No colours over SSH/`tee` | `--color always`, view with `less -R`; garbled symbols → `export LANG=C.UTF-8` |
