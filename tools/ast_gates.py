"""The SonarQube AST proxies, extracted so they can be pointed at a red case.

Two rules the platform enforces server-side and the local eslint proxy cannot
see: python:S3776 (cognitive complexity, cap 15) and python:S107 (parameter
count, cap 13). Both have failed a real upload.

This lives in a script rather than a heredoc inside `simulate-pipeline.sh` for
one reason: a gate nobody has watched fail is not a gate. To watch this one
fail, something has to be able to run it against code that MUST be flagged, and
assert that it was. That is `--expect flagged`, which the loop runs against
`gate-cases/` before it trusts the same checker against `src/`.

Not part of the deploy archive: the packaging allowlist does not name `tools/`.
Not analysed by SonarQube either, which declares `sonar.sources=src`.
"""

from __future__ import annotations

import argparse
import ast
import glob
import sys

from cognitive_complexity.api import get_cognitive_complexity

MAX_CC = 15
MAX_PARAMS = 13


def _param_count(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    a = node.args
    return (len(a.posonlyargs) + len(a.args) + len(a.kwonlyargs)
            + (1 if a.vararg else 0) + (1 if a.kwarg else 0))


def findings(paths: list[str], max_cc: int, max_params: int) -> list[str]:
    """Every rule breach in these files, as printable lines."""
    out: list[str] = []
    for path in sorted(paths):
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            cc = get_cognitive_complexity(node)
            if cc > max_cc:
                out.append(f"{path}:{node.lineno} {node.name} "
                           f"cc={cc} (max {max_cc}, S3776)")
            params = _param_count(node)
            if params > max_params:
                out.append(f"{path}:{node.lineno} {node.name} "
                           f"params={params} (max {max_params}, S107)")
    return out


def _expand(patterns: list[str]) -> list[str]:
    files: list[str] = []
    for pattern in patterns:
        files.extend(glob.glob(pattern))
    return files


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+", help="files or globs to check")
    ap.add_argument("--max-cc", type=int, default=MAX_CC)
    ap.add_argument("--max-params", type=int, default=MAX_PARAMS)
    ap.add_argument("--expect", choices=("clean", "flagged"), default="clean",
                    help="clean: fail if anything is flagged (the real check). "
                         "flagged: fail if NOTHING is flagged (the red case, "
                         "which proves the check still works).")
    ap.add_argument("--require", type=int, default=0,
                    help="with --expect flagged, the minimum number of "
                         "findings the red case must produce")
    args = ap.parse_args(argv)

    files = _expand(args.paths)
    if not files:
        print(f"FAIL: no files matched {args.paths}; the gate would have "
              f"passed by examining nothing")
        return 1

    found = findings(files, args.max_cc, args.max_params)
    if args.expect == "clean":
        if found:
            print("\n".join(found))
            return 1
        print(f"cognitive complexity OK (<={args.max_cc}) and parameter "
              f"counts OK (<={args.max_params}) across {len(files)} files")
        return 0

    if len(found) < max(1, args.require):
        print(f"FAIL: the red case produced {len(found)} findings, expected at "
              f"least {max(1, args.require)}. The AST gate is NOT working, so "
              f"a clean run against src proves nothing.")
        print("\n".join(found))
        return 1
    print(f"AST gate bites: {len(found)} findings on the red case "
          f"({', '.join(f.split()[1] for f in found)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
