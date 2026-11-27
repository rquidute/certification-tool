"""What `th-cli --version` says about the TH under test, and whether the run's label matches it."""
import re
from typing import Dict


def parse_versions(text: str) -> Dict[str, str]:
    """Pick the fields out of `th-cli --version` output:

        Version: 0.1.0              <- the CLI
        CLI SHA: d53ca722
        version: v2.16-beta3+winter2026   <- the backend (TH under test)
        sha: 4606147-local
        sdk_sha: 5741ad7
    """
    fields = {}
    for key, pattern in (("cli", r"^Version:\s*(\S+)"), ("cli_sha", r"^CLI SHA:\s*(\S+)"), ("th", r"^version:\s*(\S+)"),
                         ("th_sha", r"^sha:\s*(\S+)"), ("sdk_sha", r"^sdk_sha:\s*(\S+)")):
        m = re.search(pattern, text, re.M)
        if m:
            fields[key] = m.group(1)
    return fields


def describe(versions: Dict[str, str]) -> str:
    if "th" not in versions:
        return "unknown (th-cli --version did not report the TH version)"
    extra = ", ".join(f"{label} {versions[k]}" for k, label in (("th_sha", "sha"), ("sdk_sha", "SDK")) if k in versions)
    return versions["th"] + (f" ({extra})" if extra else "")


def label_matches(label: str, th_version: str) -> bool:
    """A label such as 'v2.17-beta1' matches 'v2.17-beta1+spring2027'; 'v2.17-beta1' does not match 'v2.16-beta3+winter2026'."""
    a, b = label.strip().lower().lstrip("v"), th_version.strip().lower().lstrip("v")
    return bool(a) and (a in b or b in a)
