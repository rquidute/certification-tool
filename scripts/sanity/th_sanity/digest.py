"""Pull the interesting lines out of a th-cli run so a failed test explains itself on the console and in the report."""
import re
from typing import List

# th-cli marks tree entries '- <step> [FAILED]' / '[ERROR]'; test logs and tracebacks use the usual words.
INTERESTING = re.compile(r"\[(?:FAILED|ERROR)\]|Traceback|Exception|AssertionError|\bERROR\b|\bFAIL(?:ED)?\b", re.I)


def failure_digest(output: str, limit: int = 12) -> List[str]:
    """First `limit` distinct lines that look like a failure, without the final 'Results:' summary line."""
    picked: List[str] = []
    seen = set()
    for raw in output.splitlines():
        line = raw.rstrip()
        key = line.strip()
        if not key or key.startswith("Results:") or key in seen or not INTERESTING.search(line):
            continue
        seen.add(key)
        picked.append(line[:200])
        if len(picked) == limit:
            break
    return picked
