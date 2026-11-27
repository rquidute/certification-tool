"""Phase 4: side-loaded tests. Copy SDK tests into the custom folders, rescan once, check each shows up in its
'<suite>-custom' suite, run the ones that can run, remove them, rescan once more."""
import os
import re
import shutil
from typing import Dict, List, Optional, Tuple

from ..results import FAIL, NOT_RUN, PASS
from ..runtest import run_test
from ..session import Session
from . import coverage


def find_custom(live: Dict[str, set], base: str, suite: Optional[str] = None) -> Optional[Tuple[str, str]]:
    """(suite, test id) of a side-loaded variant of `base`. Python overrides get a '-custom' id suffix;
    custom YAML tests land in a '<suite>-custom' suite, possibly with the same id as the SDK test."""
    norm = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())  # noqa: E731
    for sname, tests in live.items():
        if suite is not None and sname != suite:
            continue
        for t in tests:
            if norm(base) in norm(t) and ("custom" in t.lower() or sname.lower().endswith("-custom")):
                return sname, t
    return None


def _write(path: str, text: str) -> None:
    with open(path, "w") as fh:
        fh.write(text)


def run(s: Session) -> None:
    g = "custom"
    fixtures = s.custom_fixtures()
    s.results.progress = "custom tests"
    if s.dry_run:
        for f in fixtures:
            s.record(f"custom-{f['id']}", g, NOT_RUN, "dry-run")
        return
    placed: List[Tuple[dict, str]] = []
    try:
        for f in fixtures:
            src = f["src"].format(th_root=s.th_root)
            dest_dir = f["dest_dir"].format(th_root=s.th_root)
            dest = os.path.join(dest_dir, os.path.basename(src))
            if os.path.exists(dest):
                s.record(f"custom-{f['id']}", g, NOT_RUN, f"{dest} already exists; refusing to overwrite or delete a user file")
            elif not os.path.isfile(src):
                s.record(f"custom-{f['id']}", g, FAIL, f"fixture source missing: {src}")
            elif not os.path.isdir(dest_dir):
                s.record(f"custom-{f['id']}", g, FAIL, f"custom folder missing: {dest_dir}")
            else:
                shutil.copyfile(src, dest)
                placed.append((f, dest))
        if not placed:
            return
        _, rescan_out = s.cli_step("custom-rescan-add", g, ["rescan-tests"], timeout=180)
        rescan_file = os.path.join(s.step_dir(g, "rescan-add"), "rescan.out")
        _write(rescan_file, rescan_out)
        coverage.load_live(s)
        for f, _ in placed:
            _check_one(s, f, rescan_file)
    finally:
        for _, dest in placed:
            if os.path.exists(dest):
                os.remove(dest)
        if placed:
            _, out_rm = s.cli_step("custom-rescan-remove", g, ["rescan-tests"], timeout=180)
            _write(os.path.join(s.step_dir(g, "rescan-remove"), "rescan.out"), out_rm)
            coverage.load_live(s)
            for f, _ in placed:
                left = find_custom(s.live, f["test"], f.get("expect_suite"))
                s.record(f"custom-{f['id']}-removed", g, PASS if not left else FAIL,
                         "custom variant gone after removal + rescan" if not left else f"{left[1]} STILL listed after removal + rescan")


def _check_one(s: Session, f: dict, rescan_file: str) -> None:
    g, sid, want = "custom", f["id"], f.get("expect_suite")
    hit = find_custom(s.live, f["test"], want)
    if not hit:
        elsewhere = find_custom(s.live, f["test"])
        where = f"found in a different suite {elsewhere[0]!r}" if elsewhere else "not listed in any custom suite"
        note = ""
        if f.get("kind", "") == "yaml" or str(f["src"]).endswith((".yaml", ".yml")):
            note = ("\nNOTE: the custom YAML collection is built when the backend imports "
                    "support/yaml_tests/__init__.py, so a rescan may not reload it (backend restart needed?)")
        s.record(f"custom-{sid}-visible", g, FAIL, f"{f['test']} expected in {want!r}: {where}{note}\nfull rescan output: {rescan_file}")
        return
    s.record(f"custom-{sid}-visible", g, PASS, f"{hit[1]} in suite {hit[0]!r}")
    if f.get("run") is False:
        s.record(f"custom-{sid}-run", g, NOT_RUN, f.get("run_note", "not run by design"))
    elif "custom" not in hit[1].lower():
        # Same id as the SDK test: `th-cli run-tests -t <id>` cannot say which one to run, so a
        # PASS here could silently be the SDK test. Refuse rather than report a false pass.
        s.record(f"custom-{sid}-run", g, NOT_RUN,
                 f"custom id {hit[1]!r} is identical to the SDK test id; th-cli cannot select the custom variant unambiguously")
    else:
        s.results.progress = f"custom {sid}"
        eid = run_test(s, f"custom-{sid}-run", g, hit[1], f.get("expect", "pass"), f.get("prompt_answers", []),
                       f.get("dut"), "fresh", text_answers=f.get("text_answers"))
        if eid:
            s.exec_ids[f"custom-{sid}"] = eid
