"""RED CASE for the bandit-family gate (1.7.14). Never imported, never run.

The App Store's CATO score for 1.7.13 read 97, not 100, on three bandit B101
findings: `assert` in shipped code, which `python -O` strips. The gate runs
ruff's flake8-bandit port (`S`) over `src` and must flag every line below. If
it stops doing so, the gate is broken and the build fails.
"""

import pickle
import subprocess


def held(data):
    assert data is not None          # S101, the 1.7.13 finding
    return data


def run(cmd):
    return subprocess.run(cmd, shell=True, check=False)   # S602


def thaw(blob):
    return pickle.loads(blob)        # S301


def calc(text):
    return eval(text)                # S307
