"""Phase 2: coverage gate. Every suite that exists in the TH must be in the manifest or explicitly excluded,
so a new suite can never be skipped unnoticed."""
from typing import Any, Dict, List

from ..results import FAIL, NOT_RUN, PASS
from ..session import Session


def load_live(s: Session) -> bool:
    """Read `available-tests --json` into s.live ({suite public_id: {test public_ids}})."""
    data = s.th.json(["available-tests", "--json"])
    if data is None:
        s.record("load-live-suites", "coverage", FAIL, "could not read `available-tests --json`")
        return False
    s.live = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            tcs = node.get("test_cases")
            if isinstance(tcs, (dict, list)):
                # Real shape: {"metadata": {"public_id": ...}, "test_cases": {<public_id>: {"metadata": ...}}}
                sid = str((node.get("metadata") or {}).get("public_id") or node.get("public_id") or node.get("name"))
                tests = set()
                for key, tc in (tcs.items() if isinstance(tcs, dict) else enumerate(tcs)):
                    pid = (tc.get("metadata") or {}).get("public_id") or tc.get("public_id")
                    tests.add(str(pid or (key if isinstance(key, str) else "")))
                tests.discard("")
                s.live.setdefault(sid, set()).update(tests)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    if not s.live:
        s.record("load-live-suites", "coverage", FAIL, "no suites found in available-tests output; JSON shape changed?")
        return False
    return True


def run(s: Session) -> None:
    g = "coverage"
    if not load_live(s):
        return
    listed = {e["suite"] for e in s.m["suites"]}
    excluded = {e["suite"]: e["reason"] for e in s.m.get("excluded", [])}
    for e in s.m["suites"]:
        if "TODO" in (e["suite"], e["test"]):
            s.record(f"manifest-{e['id']}", g, FAIL, "manifest entry still has TODO; fill suite/test from available-tests --json")
        elif e["suite"] not in s.live:
            s.record(f"manifest-{e['id']}", g, FAIL, f"suite {e['suite']!r} not found in TH")
        elif e["test"] not in s.live[e["suite"]]:
            s.record(f"manifest-{e['id']}", g, FAIL, f"test {e['test']!r} not in suite {e['suite']!r}")
        else:
            s.record(f"manifest-{e['id']}", g, PASS, f"{e['suite']} / {e['test']}")
    uncovered = sorted(set(s.live) - listed - set(excluded))
    # '<suite>-custom' suites hold side-loaded files and come and go with them. They count as exercised when a
    # custom fixture in the manifest targets that suite; otherwise they are reported, never silently covered.
    by_suite: Dict[str, List[str]] = {}
    for f in s.custom_fixtures():
        if f.get("expect_suite"):
            by_suite.setdefault(f["expect_suite"], []).append(f["id"])
    for u in [x for x in uncovered if x.endswith("-custom")]:
        uncovered.remove(u)
        if u in by_suite:
            s.record(f"custom-suite-{u}", g, PASS, f"user custom suite ({len(s.live[u])} tests) is exercised by fixture(s): {', '.join(by_suite[u])}")
        else:
            s.record(f"excluded-{u}", g, NOT_RUN, f"user custom suite ({len(s.live[u])} tests) has no matching fixture in the manifest")
    for u in uncovered:
        s.record(f"uncovered-{u}", g, FAIL, f"suite {u!r} exists in the TH ({len(s.live[u])} tests) but is neither in the manifest nor excluded")
    for suite, reason in excluded.items():
        s.record(f"excluded-{suite}", g, NOT_RUN, f"explicitly excluded: {reason}")
    s.record("live-suite-summary", g, PASS, f"{len(s.live)} live suites, {len(listed)} covered, {len(excluded)} excluded")
