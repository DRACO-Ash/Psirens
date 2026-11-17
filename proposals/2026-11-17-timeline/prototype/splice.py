"""Refresh the live demo inside proposal.src.html from the prototype itself:
the timeline block from the (patched) SPA, and the scenario events.

Usage: python splice.py <repo-root>
Idempotent: it rewrites only what sits between the @@...-BEGIN@@ and
@@...-END@@ markers, so the demo can never drift from the code the figures
were captured from."""
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
here = Path(__file__).resolve().parent
page = here.parent / "proposal.src.html"
spa = (root / "src/psirens/static/index.html").read_text(encoding="utf-8")
a = spa.index("// -- timeline: zoomable, pannable, clustered")
b = spa.index("// -- halos on the plot")
code = spa[a:b].rstrip() + "\n"
events = json.loads((here / "events.json").read_text())["events"]
html = page.read_text(encoding="utf-8")


def put(text: str, name: str, body: str) -> str:
    pat = re.compile(r"(/\* @@%s-BEGIN@@ \*/).*?(/\* @@%s-END@@ \*/)" % (name, name), re.S)
    assert len(pat.findall(text)) == 1, name
    return pat.sub(lambda m: m.group(1) + body + m.group(2), text)


html = put(html, "TIMELINE", "\n" + code)
html = put(html, "EVENTS", json.dumps(events, separators=(",", ":")))
page.write_text(html, encoding="utf-8")
print(f"spliced {code.count(chr(10))} lines of timeline code and {len(events)} events")
