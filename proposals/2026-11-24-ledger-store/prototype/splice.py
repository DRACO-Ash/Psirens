"""Puts the prototype's measured output into the proposal page, between the
DATA markers, so every number the page shows comes from results.json,
hotspots.json and Ash's live backfill report rather than from typing.

    python3 splice.py   (rewrites ../proposal.src.html in place)
"""

from __future__ import annotations

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "..", "proposal.src.html")
report = json.load(open(os.path.join(HERE, "live-backfill-report.json")))
ok = [c["seconds"] - c.get("waited_s", 0) for c in report["chunks"] if c["ok"]]
timeouts = [c for c in report["chunks"] if not c["ok"] and "Timeout" in c["detail"]]
data = {
    "results": json.load(open(os.path.join(HERE, "results.json"))),
    "hotspots": json.load(open(os.path.join(HERE, "hotspots.json"))),
    "live": {"chunks": len(report["chunks"]), "ok": len(ok), "timeouts": len(timeouts),
             "ok_min_s": round(min(ok)), "ok_max_s": round(max(ok)),
             "added": report["summary"]["added"]},
}
blob = json.dumps(data, separators=(",", ":"))
html = open(PAGE, encoding="utf-8").read()
html, n = re.subn(r"<!--DATA-->.*?<!--/DATA-->",
                  lambda _: '<!--DATA--><script id="data" type="application/json">'
                  + blob + "</script><!--/DATA-->", html, flags=re.S)
assert n == 1, "the page must carry exactly one DATA block"
open(PAGE, "w", encoding="utf-8").write(html)
print("spliced", len(blob), "bytes of measured data")
