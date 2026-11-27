"""Release sanity harness for the Matter Test Harness. See th_sanity.cli for what it does and how to run it.

Layout
    cli.py           argument parsing, the pre-run confirmation, phase order
    session.py       shared state and services for one run (what phases receive)
    results.py       step ledger, console lines, verdict, report categories
    console.py       colours, progress bars, symbols
    thcli.py         th-cli wrapper (run / json / runner state / executions / projects)
    prompts.py       pty driver that answers th-cli's option, text and file-upload prompts
    dut.py           DUT app lifecycle (fresh vs reuse readiness)
    runtest.py       run one test case and record the outcome
    report.py        report.md / report.json, console summary, `latest` pointer
    phases/          preclean, cli_commands, coverage, suites, custom, executions, abort

Tests (stdlib unittest, no th-cli / backend / DUT needed):
    python3 -m unittest discover -s scripts/sanity/tests
"""
