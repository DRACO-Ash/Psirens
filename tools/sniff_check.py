#!/usr/bin/env python3
"""Portable checker for the code-quality sniffs in SNIFFS.md.

Standard library only, single file, Python 3.9+. Drop it into any project and
run it; it needs no pip install, no node, and no network. That is deliberate:
the compendium is meant to be handed to other teams, and a checker they cannot
run is a document they will not use.

    python3 sniff_check.py <path> [<path> ...]      check a tree
    python3 sniff_check.py --self-test              prove the checks work
    python3 sniff_check.py --list                   list the rules

Exit codes: 0 clean, 1 findings, 2 bad usage.

WHAT IT DOES NOT DO. It is not SonarQube and does not pretend to be. It covers
the sniffs that are exactly decidable from source text or a Python AST, which
is the subset that has actually cost this estate upload cycles. Rules needing
real type inference or cross-file analysis are listed in SNIFFS.md as
scanner-only. Run the real scanner as well; this is the cheap pass that stops
the expensive one being a surprise.

The self-test is the point. Every rule below carries a case that MUST be
flagged and a case that MUST NOT be, because a checker nobody has watched fail
is not a checker. `--self-test` emits a JSON assertion manifest.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Iterable, NamedTuple

MAX_COGNITIVE = 15
MAX_PARAMS = 13
PY_EXT = (".py",)
WEB_EXT = (".html", ".htm", ".js")


class Finding(NamedTuple):
    path: str
    line: int
    rule: str
    message: str


# ---------------------------------------------------------------------------
# Python: cognitive complexity
# ---------------------------------------------------------------------------
# An INDEPENDENT implementation of the Sonar cognitive-complexity algorithm:
# a structure that breaks the linear flow costs 1, and costs 1 more for every
# level it is nested inside. Cross-checked against the reference
# `cognitive_complexity` package over this repository; see SNIFFS.md for the
# agreement figure. It is an approximation of a server-side rule, so treat a
# borderline score as a prompt to check rather than as a verdict.
_LOOP_NODES = (ast.For, ast.AsyncFor, ast.While)


def _elif_chain(node: ast.If) -> tuple[list[ast.If], list[ast.stmt]]:
    """Flatten `if / elif / elif / else` into its arms and its final else.

    Python has no elif node: an elif is an If that is the only statement of the
    previous If's orelse. Walking that structure naively treats each elif as a
    deeper nesting level, which it is not, and an earlier version of this file
    did exactly that. Flattening the chain first removes the whole class of
    error.
    """
    arms, node_iter = [], node
    while True:
        orelse = node_iter.orelse
        if len(orelse) == 1 and isinstance(orelse[0], ast.If):
            node_iter = orelse[0]
            arms.append(node_iter)
            continue
        return arms, orelse


class _ComplexityVisitor:
    """Sonar's cognitive complexity: a structure that breaks the linear flow
    costs 1, and costs 1 more for every level it is nested inside.

    Kept inside the cap it enforces, which is not vanity: a tool that breaches
    its own standard invites every reader to treat the standard as advisory.
    """

    def __init__(self) -> None:
        self.score = 0

    def walk(self, node: ast.AST, nesting: int) -> None:
        for child in ast.iter_child_nodes(node):
            self.visit(child, nesting)

    def walk_all(self, nodes: Iterable[ast.AST], nesting: int) -> None:
        for node in nodes:
            self.visit(node, nesting)

    def visit(self, child: ast.AST, nesting: int) -> None:
        if isinstance(child, ast.BoolOp):
            self.score += 1                      # one per boolean sequence
            self.walk(child, nesting)
        elif isinstance(child, ast.If):
            self.visit_if(child, nesting)
        elif isinstance(child, _LOOP_NODES):
            self.score += 1 + nesting + (1 if child.orelse else 0)
            self.walk(child, nesting + 1)
        elif isinstance(child, ast.ExceptHandler):
            self.score += 1 + nesting
            self.walk(child, nesting + 1)
        elif isinstance(child, ast.IfExp):
            self.score += 1 + nesting            # ternary
            self.walk(child, nesting + 1)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.walk(child, nesting + 1)        # a nested def deepens nesting
        else:
            self.walk(child, nesting)

    def visit_if(self, node: ast.If, nesting: int) -> None:
        """The whole if/elif/else chain. The head pays the nesting penalty;
        each elif pays 1 flat; a final else pays 1 flat. Every ARM's body sits
        one level deeper, elif arms included."""
        arms, final_else = _elif_chain(node)
        self.score += 1 + nesting + len(arms) + (1 if final_else else 0)
        self.visit(node.test, nesting)
        self.walk_all(node.body, nesting + 1)
        for arm in arms:
            self.visit(arm.test, nesting)
            self.walk_all(arm.body, nesting + 1)
        self.walk_all(final_else, nesting + 1)


# Use the reference implementation when it happens to be installed, and say
# which engine produced the number. The built-in is exact on every elementary
# construct and on 384 of the 386 functions in this repository, but it
# OVER-counts deeply nested if/elif chains, and an over-count is a false
# positive: it would send somebody refactoring code the real gate accepts.
# Being explicit about the engine costs one line of output and buys the right
# to be believed.
try:  # pragma: no cover - depends on the host environment
    from cognitive_complexity.api import (  # type: ignore
        get_cognitive_complexity as _reference_cc)
    COMPLEXITY_ENGINE = "reference (cognitive_complexity package)"
except ImportError:  # pragma: no cover
    _reference_cc = None
    COMPLEXITY_ENGINE = "built-in approximation (may over-count nested elif)"


def approximate_cognitive_complexity(func: ast.AST) -> int:
    """The stdlib-only implementation. Kept as the portable fallback."""
    visitor = _ComplexityVisitor()
    visitor.walk(func, 0)
    return visitor.score


def cognitive_complexity(func: ast.AST) -> int:
    if _reference_cc is not None:
        return _reference_cc(func)
    return approximate_cognitive_complexity(func)


def param_count(func: ast.AST) -> int:
    a = func.args  # type: ignore[attr-defined]
    return (len(a.posonlyargs) + len(a.args) + len(a.kwonlyargs)
            + (1 if a.vararg else 0) + (1 if a.kwarg else 0))


def _check_python(path: str, text: str) -> list[Finding]:
    out: list[Finding] = []
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        return [Finding(path, exc.lineno or 0, "PY-SYNTAX",
                        f"cannot parse: {exc.msg}")]
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.extend(_check_function(path, node))
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            out.append(Finding(path, node.lineno, "PY-BARE-EXCEPT",
                               "bare `except:` swallows KeyboardInterrupt and "
                               "SystemExit; name the exceptions you handle"))
        if isinstance(node, ast.Compare):
            out.extend(_check_none_compare(path, node))
    return out


def _check_function(path: str, node: ast.AST) -> list[Finding]:
    out: list[Finding] = []
    name = node.name  # type: ignore[attr-defined]
    cc = cognitive_complexity(node)
    if cc > MAX_COGNITIVE:
        out.append(Finding(path, node.lineno, "PY-COMPLEXITY",  # type: ignore
                           f"{name} has cognitive complexity {cc} "
                           f"(cap {MAX_COGNITIVE}, python:S3776; engine: "
                           f"{COMPLEXITY_ENGINE})"))
    params = param_count(node)
    if params > MAX_PARAMS:
        out.append(Finding(path, node.lineno, "PY-PARAMS",  # type: ignore
                           f"{name} takes {params} parameters "
                           f"(cap {MAX_PARAMS}, python:S107); group related "
                           f"arguments into a value object"))
    for default in list(node.args.defaults) + list(node.args.kw_defaults):  # type: ignore
        if isinstance(default, (ast.List, ast.Dict, ast.Set)):
            out.append(Finding(path, node.lineno, "PY-MUTABLE-DEFAULT",  # type: ignore
                               f"{name} has a mutable default argument; it is "
                               f"created once and shared by every call"))
    return out


def _check_none_compare(path: str, node: ast.Compare) -> list[Finding]:
    out: list[Finding] = []
    for op, cmp_to in zip(node.ops, node.comparators):
        is_none = isinstance(cmp_to, ast.Constant) and cmp_to.value is None
        if is_none and isinstance(op, (ast.Eq, ast.NotEq)):
            out.append(Finding(path, node.lineno, "PY-NONE-COMPARE",
                               "compare to None with `is` / `is not`, not "
                               "`==` / `!=`"))
    return out


# ---------------------------------------------------------------------------
# Web: text rules over HTML and inline JS
# ---------------------------------------------------------------------------
# Comments are stripped BEFORE matching. A grep gate in this estate once
# matched its own explanatory comment and failed a clean build, and a rule that
# fires on the sentence describing it trains people to disable it.
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"(^|[^:])//[^\n]*")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)

WEB_RULES = (
    ("WEB-WINDOW", re.compile(r"\bwindow\."),
     "prefer globalThis (or navigator/document) over window.* (S6643)"),
    ("WEB-ARIA-ROLE",
     re.compile(r'role="(region|banner|navigation|main|contentinfo'
                r'|complementary|form)"'),
     "use the native element instead of the ARIA landmark role (S6819)"),
    ("WEB-GETATTR-DATA", re.compile(r'getAttribute\(\s*["\']data-'),
     "use element.dataset.x instead of getAttribute(\"data-x\")"),
    ("WEB-PROMISE-REJECT", re.compile(r"return\s+Promise\.reject\("),
     "prefer `throw error` over `return Promise.reject(error)` in a then "
     "callback; both reject identically and SonarJS flags the returned form"),
    # The trailing (?![\d.]) is load bearing: without it 127.0.0.1 matches,
    # because ".0" is followed by a dot rather than a digit. Found by running
    # this checker over this repository, which is the only way that class of
    # error ever surfaces.
    ("WEB-ZERO-FRACTION", re.compile(r"(?<![\w.])\d+\.0(?![\d.])"),
     "drop the zero fraction: write 2, not 2.0"),
)


def strip_comments(text: str) -> str:
    """Blank out comments, preserving line numbers so findings stay accurate."""
    def blank(match: re.Match) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))
    text = _HTML_COMMENT.sub(blank, text)
    text = _BLOCK_COMMENT.sub(blank, text)
    return _LINE_COMMENT.sub(lambda m: m.group(1) + " " * (len(m.group(0)) - 1),
                             text)


def _check_web(path: str, text: str) -> list[Finding]:
    clean = strip_comments(text)
    out: list[Finding] = []
    for rule, pattern, message in WEB_RULES:
        for match in pattern.finditer(clean):
            line = clean.count("\n", 0, match.start()) + 1
            out.append(Finding(path, line, rule, message))
    return out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def iter_files(roots: Iterable[str]) -> Iterable[str]:
    skip = {".git", ".venv", "node_modules", "__pycache__", "dist", "build"}
    for root in roots:
        if os.path.isfile(root):
            yield root
            continue
        for base, dirs, names in os.walk(root):
            dirs[:] = [d for d in dirs if d not in skip]
            for name in sorted(names):
                if name.endswith(PY_EXT + WEB_EXT):
                    yield os.path.join(base, name)


def check_paths(roots: Iterable[str]) -> list[Finding]:
    out: list[Finding] = []
    for path in iter_files(roots):
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        if path.endswith(PY_EXT):
            out.extend(_check_python(path, text))
        else:
            out.extend(_check_web(path, text))
    return out


# ---------------------------------------------------------------------------
# Self-test: every rule must reject a bad case and accept a good one
# ---------------------------------------------------------------------------
_CASES = [
    ("PY-COMPLEXITY",
     "def f(a,b,c,d):\n" + "".join(
         f"    if a=={i}:\n        for x in b:\n            while c:\n"
         f"                if d and x:\n                    return {i}\n"
         for i in range(4)) + "    return 0\n",
     "def g(a):\n    if a:\n        return 1\n    return 2\n", ".py"),
    ("PY-PARAMS",
     "def f(a,b,c,d,e,f,g,h,i,j,k,m,n,p):\n    return 0\n",
     "def g(a,b,c):\n    return 0\n", ".py"),
    ("PY-MUTABLE-DEFAULT",
     "def f(x=[]):\n    return x\n",
     "def g(x=None):\n    return x or []\n", ".py"),
    ("PY-BARE-EXCEPT",
     "def f():\n    try:\n        pass\n    except:\n        pass\n",
     "def g():\n    try:\n        pass\n    except ValueError:\n        pass\n", ".py"),
    ("PY-NONE-COMPARE",
     "def f(x):\n    return x == None\n",
     "def g(x):\n    return x is None\n", ".py"),
    ("WEB-WINDOW", "var w = window.innerWidth;\n",
     "var w = globalThis.innerWidth;\n", ".js"),
    ("WEB-ARIA-ROLE", '<div role="region"></div>\n', "<section></section>\n", ".html"),
    ("WEB-GETATTR-DATA", 'var v = el.getAttribute("data-id");\n',
     "var v = el.dataset.id;\n", ".js"),
    ("WEB-PROMISE-REJECT", "function f(){ return Promise.reject(new Error()); }\n",
     "function f(){ throw new Error(); }\n", ".js"),
    ("WEB-ZERO-FRACTION", "var n = 2.0;\n", "var n = 2;\n", ".js"),
]

# The comment-blindness case: a rule must not fire on prose describing it.
_COMMENT_CASES = [
    ("WEB-WINDOW", "// never use window.here\nvar w = globalThis.a;\n", ".js"),
    ("WEB-PROMISE-REJECT",
     "/* do not return Promise.reject( in a then */\nvar a = 1;\n", ".js"),
    ("WEB-GETATTR-DATA",
     '<!-- avoid getAttribute("data-x") -->\n<div></div>\n', ".html"),
]


def _run_case(text: str, ext: str) -> list[Finding]:
    path = "case" + ext
    return _check_python(path, text) if ext == ".py" else _check_web(path, text)


def self_test() -> int:
    results = []
    for rule, bad, good, ext in _CASES:
        flagged = {f.rule for f in _run_case(bad, ext)}
        clean = {f.rule for f in _run_case(good, ext)}
        results.append({
            "test": f"{rule}/rejects-bad", "assertion": f"{rule} in findings",
            "expected": True, "observed": rule in flagged,
            "status": "PASS" if rule in flagged else "FAIL"})
        results.append({
            "test": f"{rule}/accepts-good", "assertion": f"{rule} not in findings",
            "expected": True, "observed": rule not in clean,
            "status": "PASS" if rule not in clean else "FAIL"})
    for rule, text, ext in _COMMENT_CASES:
        flagged = {f.rule for f in _run_case(text, ext)}
        ok = rule not in flagged
        results.append({
            "test": f"{rule}/ignores-its-own-comment",
            "assertion": f"{rule} not flagged inside a comment",
            "expected": True, "observed": ok,
            "status": "PASS" if ok else "FAIL"})
    failed = [r for r in results if r["status"] == "FAIL"]
    manifest = {
        "tool": "sniff_check", "generated": datetime.now(timezone.utc).isoformat(),
        "total": len(results), "passed": len(results) - len(failed),
        "failed": len(failed), "results": results,
    }
    json.dump(manifest, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="files or directories to check")
    ap.add_argument("--self-test", action="store_true",
                    help="prove every rule rejects a bad case and accepts a good one")
    ap.add_argument("--list", action="store_true", help="list the rules")
    args = ap.parse_args(argv)

    if args.self_test:
        return self_test()
    if args.list:
        for rule, _, message in WEB_RULES:
            print(f"{rule:22} {message}")
        for rule in ("PY-COMPLEXITY", "PY-PARAMS", "PY-MUTABLE-DEFAULT",
                     "PY-BARE-EXCEPT", "PY-NONE-COMPARE"):
            print(f"{rule:22} see SNIFFS.md")
        return 0
    if not args.paths:
        ap.print_usage(sys.stderr)
        return 2

    findings = check_paths(args.paths)
    for f in sorted(findings):
        print(f"{f.path}:{f.line}: [{f.rule}] {f.message}")
    print(f"\n{len(findings)} finding(s) across "
          f"{sum(1 for _ in iter_files(args.paths))} file(s)", file=sys.stderr)
    print(f"cognitive complexity engine: {COMPLEXITY_ENGINE}", file=sys.stderr)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
