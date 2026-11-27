#!/usr/bin/env python3
"""Release sanity harness for the Matter Test Harness (TH). Entry point; the code lives in th_sanity/.

    python3 scripts/sanity/run_sanity.py --help
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from th_sanity.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
