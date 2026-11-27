"""Console styling for the sanity harness (standard library only).

Colour is off when output is not a terminal or NO_COLOR is set; `set_color` overrides that.
"""
import os
import sys

USE_COLOR = False
UNICODE = (getattr(sys.stdout, "encoding", "") or "").lower().startswith("utf")
_CODES = {"bold": "1", "dim": "2", "inv": "7", "red": "31", "green": "32", "yellow": "33", "cyan": "36", "gray": "90"}

SYMBOL = {"PASS": "✔" if UNICODE else "+", "FAIL": "✘" if UNICODE else "x", "NOT_RUN": "○" if UNICODE else "-"}
LABEL = {"PASS": "PASS", "FAIL": "FAIL", "NOT_RUN": "SKIP"}
STATUS_COLOR = {"PASS": "green", "FAIL": "red", "NOT_RUN": "yellow"}


def set_color(mode: str) -> None:
    """mode: auto | always | never."""
    global USE_COLOR
    USE_COLOR = mode == "always" or (mode == "auto" and sys.stdout.isatty() and "NO_COLOR" not in os.environ)


def paint(text: str, *styles: str) -> str:
    if not USE_COLOR or not styles:
        return text
    return "\033[" + ";".join(_CODES[s] for s in styles) + "m" + text + "\033[0m"


def bar(done: int, total: int, width: int = 12) -> str:
    filled = int(width * done / total) if total else 0
    full, empty = ("█", "░") if UNICODE else ("#", "-")
    return paint(full * filled, "green") + paint(empty * (width - filled), "gray")


def rule(width: int = 60) -> str:
    return paint(("─" if UNICODE else "-") * width, "gray")


def note(text: str) -> None:
    """Dim, indented progress line printed under a step."""
    print(paint(f"      {'…' if UNICODE else '...'} {text}", "gray"), flush=True)
