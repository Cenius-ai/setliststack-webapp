#!/usr/bin/env python3
"""Convenience launcher.

`python3 main.py <command>` is exactly `python3 setliststack.py <command>`; the
whole tool lives in setliststack.py so it stays a single read-through file.
"""

import sys

from setliststack import main

if __name__ == "__main__":
    sys.exit(main())
