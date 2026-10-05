#!/usr/bin/env python3
"""Produce a single-file, offline copy of the Operator Guide for review.

The shipped guide loads its figures as separate served assets, which is right
for the app and useless for someone who wants to open the thing from a laptop
before approving an upload. This inlines every figure as a data URI and adds a
one-line banner saying what the file is, so the reviewer can exercise the
sandbox, the drills, the tour and the self-check with no server at all.

It NEVER edits the shipped page: input and output are different files.
Stdlib only, single file, deterministic.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import sys

_IMG = re.compile(r'src="(guide-[A-Za-z0-9_-]+\.png)"')
_BANNER = (
    '<div class="note warn" style="margin:18px 26px 0">'
    "<b>Standalone review copy.</b> Every figure is embedded, so this file "
    "works with no server and nothing is fetched from anywhere. In the "
    "application the same page is served at <code>/static/guide.html</code> "
    "and reached from the <b>Guide</b> control at the top right. The only "
    "things that differ here: the version line in the header cannot be read "
    "from the running app, and <b>Open PSIRENS</b> has nowhere to go."
    "</div>"
)


def inline(page: str, static_dir: str) -> tuple[str, int]:
    """Replace each figure reference with a base64 data URI."""
    count = 0

    def sub(match: re.Match) -> str:
        nonlocal count
        path = os.path.join(static_dir, match.group(1))
        with open(path, "rb") as fh:
            data = base64.b64encode(fh.read()).decode("ascii")
        count += 1
        return 'src="data:image/png;base64,' + data + '"'

    return _IMG.sub(sub, page), count


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--static", default="src/psirens/static")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    src = os.path.join(args.static, "guide.html")
    with open(src, encoding="utf-8") as fh:
        page = fh.read()
    out, count = inline(page, args.static)
    if count == 0:
        print("FAIL: no figures were inlined; the page would be blank", file=sys.stderr)
        return 1
    marker = '<div class="shell">'
    if marker not in out:
        print("FAIL: could not find the page shell to place the banner", file=sys.stderr)
        return 1
    out = out.replace(marker, _BANNER + "\n" + marker, 1)
    out = out.replace("<title>PSIRENS Operator Guide</title>",
                      "<title>PSIRENS Operator Guide (review copy)</title>", 1)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(out)
    size = os.path.getsize(args.out) / 1048576
    print(f"wrote {args.out}: {count} figures inlined, {size:.1f}MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
