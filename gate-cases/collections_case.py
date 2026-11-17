"""Red case for the collection-idiom gate (ruff, 1.7.6). Deliberately bad.

Each block is a pattern from the family Code Quality raised on PSIRENS 1.7.5
(python:S7517, S8517, S7508, S7504) or its close ruff equivalent. The loop
runs ruff over this file and FAILS if any listed code is missing, so a ruff
that stopped selecting a rule, or stopped reading the file, cannot pass quietly.
"""


def newest(names, rank):
    return sorted(names, key=rank)[0]            # FURB192: use min()


def walk():
    objects = ["a", "bb"]
    total = 0
    for oid in list(objects):                    # PERF101: unnecessary list()
        total += len(oid)
    return total


def values(table: dict[str, int]):
    total = 0
    for k in table:                              # PLC0206: iterate .items()
        total += table[k]
    return total


def ordered(items):
    return list(sorted(items))                   # C413: redundant list()


def keys(table):
    return [k for k in table.keys()]             # SIM118: iterate the dict


def first(table):
    return list(table.values())[0]               # RUF015: next(iter(...))
