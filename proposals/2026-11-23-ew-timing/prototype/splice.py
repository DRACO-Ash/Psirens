#!/usr/bin/env python3
"""Write the prototype's measured output into proposal.src.html between the
DATA markers, so every number and chart on the page comes from the run and
none is typed by hand. Run after crossing.py and capture.sh."""
import json
from pathlib import Path

here = Path(__file__).parent
page = here.parent / "proposal.src.html"
data = {"results": json.loads((here / "results.json").read_text()),
        "mock": json.loads((here / "mock.json").read_text()),
        "seed": json.loads((here / "seed.json").read_text())}
src = page.read_text()
start, end = "<!--DATA-->", "<!--/DATA-->"
a, b = src.index(start) + len(start), src.index(end)
blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
page.write_text(src[:a] + '<script id="data" type="application/json">' + blob + "</script>" + src[b:])
print("spliced %d bytes of prototype output" % len(blob))
