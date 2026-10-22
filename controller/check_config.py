"""The installer's view of ``wakeci check``: prints the paths a configuration uses.

    python3 controller/check_config.py CONFIG.json

It runs from an unpacked bundle, so it finds the package next to this directory. On success
it prints one ``kind<TAB>value`` line per path the installer has to prepare (see
``wakeci/config_check.py``); on failure the reason goes to stderr and the exit code is 1.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from wakeci.config_check import check, paths_main  # noqa: E402,F401  (check: for tests)

run = paths_main

if __name__ == "__main__":
    sys.exit(paths_main(sys.argv[1:]))
