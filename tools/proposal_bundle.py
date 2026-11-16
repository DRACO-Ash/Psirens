#!/usr/bin/env python3
"""Bundle a change proposal into one self-contained, dynamic HTML file.

Owner rule, 16 November 2026: every change proposal ships as a dynamic,
formatted HTML file, with images, before anything is implemented.

A proposal lives in ``proposals/<date>-<slug>/``:

    proposal.src.html   the page, referencing figures as ``figures/<name>``
    figures/            the images it shows
    prototype/          whatever produced the evidence (optional)

This script inlines every ``figures/<name>`` reference as a data URI and
writes ``PROPOSAL.html`` beside the source, so the result opens from a
download, a phone or an email attachment with no server and no network.

It REFUSES to write when:
  * a referenced figure does not exist (the page would show a broken image);
  * a figure in ``figures/`` is referenced by nothing (the 1.6.16 orphan
    lesson: a figure nobody points at is a figure nobody checked);
  * the page carries an em dash or a double hyphen in prose (house style);
  * the page uses ``window.`` (the SPA's own rule; use ``globalThis``).

Stdlib only. ``--self-test`` runs offline and counts its own checks.
"""
from __future__ import annotations

import argparse
import base64
import re
import sys
import tempfile
from pathlib import Path

MEDIA = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
         ".webp": "image/webp", ".svg": "image/svg+xml", ".gif": "image/gif"}
REF = re.compile(r"""(["'(])(figures/[A-Za-z0-9._-]+)(["')])""")
SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.S | re.I)


def prose(html: str) -> str:
    """The reader's text: scripts, styles, comments and tags (with their
    attributes, where CSS writes ``var(--x)``) removed."""
    html = SCRIPT_OR_STYLE.sub(" ", html)
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.S)
    return re.sub(r"<[^>]*>", " ", html)


def figure_problems(src: Path, html: str) -> list[str]:
    figdir = src.parent / "figures"
    found: list[str] = []
    refs = {m.group(2) for m in REF.finditer(html)}
    for ref in sorted(refs):
        p = src.parent / ref
        if not p.is_file():
            found.append(f"missing figure: {ref}")
        elif p.suffix.lower() not in MEDIA:
            found.append(f"unsupported figure type: {ref}")
    if figdir.is_dir():
        found += [f"orphan figure, referenced by nothing: figures/{p.name}"
                  for p in sorted(figdir.iterdir())
                  if p.is_file() and f"figures/{p.name}" not in refs]
    return found


def style_problems(html: str) -> list[str]:
    found: list[str] = []
    for n, line in enumerate(prose(html).splitlines(), 1):
        if "\u2014" in line:
            found.append(f"em dash in prose near source line {n}")
        if re.search(r"(?<![-!<])--(?![->])", line):
            found.append(f"double hyphen in prose near source line {n}")
    if re.search(r"\bwindow\.", html):
        found.append("uses window.; use globalThis")
    return found


def problems(src: Path) -> list[str]:
    html = src.read_text(encoding="utf-8")
    return figure_problems(src, html) + style_problems(html)


def bundle(src: Path, out: Path) -> int:
    html = src.read_text(encoding="utf-8")

    def inline(m: re.Match[str]) -> str:
        p = src.parent / m.group(2)
        data = base64.b64encode(p.read_bytes()).decode("ascii")
        return f"{m.group(1)}data:{MEDIA[p.suffix.lower()]};base64,{data}{m.group(3)}"

    out.write_text(REF.sub(inline, html), encoding="utf-8")
    return out.stat().st_size


def self_test() -> int:
    checks = 0
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "figures").mkdir()
        (root / "figures" / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        src = root / "proposal.src.html"
        src.write_text('<p>ok</p><img src="figures/a.png">', encoding="utf-8")
        assert problems(src) == []
        checks += 1
        size = bundle(src, root / "PROPOSAL.html")
        out = (root / "PROPOSAL.html").read_text(encoding="utf-8")
        assert "data:image/png;base64," in out and "figures/a.png" not in out and size > 0
        checks += 1
        (root / "figures" / "b.png").write_bytes(b"x")
        assert any("orphan" in p for p in problems(src))
        checks += 1
        src.write_text('<img src="figures/a.png"><img src="figures/b.png"><img src="figures/z.png">', encoding="utf-8")
        assert any("missing figure: figures/z.png" in p for p in problems(src))
        checks += 1
        src.write_text('<img src="figures/a.png"><img src="figures/b.png"><p>a — b</p>', encoding="utf-8")
        assert any("em dash" in p for p in problems(src))
        checks += 1
        src.write_text('<img src="figures/a.png"><img src="figures/b.png"><p>a -- b</p>', encoding="utf-8")
        assert any("double hyphen" in p for p in problems(src))
        checks += 1
        src.write_text('<img src="figures/a.png"><img src="figures/b.png"><!-- a -- b --><script>i--</script>', encoding="utf-8")
        assert problems(src) == [], problems(src)
        checks += 1
        src.write_text('<img src="figures/a.png"><img src="figures/b.png"><b style="color:var(--s1)">x</b>', encoding="utf-8")
        assert problems(src) == [], problems(src)
        checks += 1
        src.write_text('<img src="figures/a.png"><img src="figures/b.png"><script>window.x=1</script>', encoding="utf-8")
        assert any("window." in p for p in problems(src))
        checks += 1
    print(f"self-test OK: {checks} checks")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("folder", nargs="?", help="proposals/<date>-<slug>")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if not a.folder:
        ap.error("give the proposal folder, or --self-test")
    src = Path(a.folder) / "proposal.src.html"
    if not src.is_file():
        print(f"no proposal.src.html in {a.folder}", file=sys.stderr)
        return 2
    found = problems(src)
    if found:
        for p in found:
            print(f"REFUSED: {p}", file=sys.stderr)
        return 1
    out = src.parent / "PROPOSAL.html"
    size = bundle(src, out)
    print(f"wrote {out} ({size / 1024:.0f} KiB, self-contained)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
