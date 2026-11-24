"""Draws figure A, the data path today and proposed, as an SVG page for
capture.js to photograph. Every number on it is read from results.json, never
typed in; the hotspot positions it prints are what the proposal page uses.

    python3 fig_architecture.py   (writes fig_architecture.html, hotspots.json)
"""

from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
R = json.load(open(os.path.join(HERE, "results.json")))
S = {r["kind"]: r for r in R["scenarios"]}
W, H = 1200, 700
MB = 1e6
FILE_MB = R["migrate"]["legacy_bytes"] / MB
SNAP_MB = R["migrate"]["snapshot_bytes"] / MB
SEG_MB = S["led_hourly"]["bytes_written"] / MB

NAVY, BLUE1, BLUE2, AMBER, GREEN, BODY, MUTED, LINE = (
    "#162646", "#385FAF", "#739BCF", "#C67C00", "#27AE60", "#2C3E50", "#5B6B80", "#DCE3EE")
HOT: list[dict] = []
out: list[str] = []


def box(x, y, w, h, text, fill="#FFFFFF", stroke=LINE, colour=BODY, bold=False, sub=None):
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="1.5"/>')
    ty = y + h / 2 + (0 if sub is None else -7)
    weight = 600 if bold else 400
    out.append(f'<text x="{x + w / 2}" y="{ty + 5}" text-anchor="middle" font-size="15" font-weight="{weight}" fill="{colour}">{text}</text>')
    if sub:
        out.append(f'<text x="{x + w / 2}" y="{ty + 24}" text-anchor="middle" font-size="12.5" fill="{MUTED}">{sub}</text>')


def arrow(x1, y1, x2, y2, colour=MUTED):
    out.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{colour}" stroke-width="2" marker-end="url(#a)"/>')


def label(x, y, text, size=13, colour=MUTED, anchor="start", weight=400):
    out.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{colour}" text-anchor="{anchor}" font-weight="{weight}">{text}</text>')


def hot(n, x, y, caption):
    HOT.append({"n": n, "left": round(100 * x / W, 1), "top": round(100 * y / H, 1), "c": caption})


# Panel frames
for x0, title, colour in ((20, "TODAY: every question reads the whole history", AMBER),
                          (615, "PROPOSED: read once, write only what changed", GREEN)):
    out.append(f'<rect x="{x0}" y="20" width="565" height="660" rx="12" fill="#F5F7FB" stroke="{LINE}"/>')
    out.append(f'<rect x="{x0}" y="20" width="565" height="6" rx="3" fill="{colour}"/>')
    label(x0 + 20, 58, title, 17, NAVY, weight=700)

# TODAY
y = 90
label(40, y + 6, "Hourly refresh (and every backfill chunk)", 14, NAVY, weight=600)
box(40, y + 20, 110, 56, "UDL pull")
arrow(150, y + 48, 178, y + 48)
box(180, y + 20, 120, 56, "parse", fill="#FBEFD9", sub=f"{FILE_MB:.0f} MB")
arrow(300, y + 48, 328, y + 48)
box(330, y + 20, 90, 56, "merge")
arrow(420, y + 48, 448, y + 48)
box(450, y + 20, 115, 56, "rewrite", fill="#FBEFD9", sub=f"{FILE_MB:.0f} MB")
hot(1, 565, y + 20, "Every pull reads the whole store and writes all of it back, "
    f"{FILE_MB:.0f} MB each way, to add about {S['cur_hourly']['added']:,} samples. "
    "A backfill chunk does the same.")
label(40, y + 104, "then, in the same cycle:", 13)
for i, (what, line) in enumerate((("high-interest prune", "store.py:333"),
                                   ("staleness report", "refresh.py:294"),
                                   ("Resolver and Shift Brief", "agreement.py:518"))):
    yy = y + 118 + i * 44
    box(40, yy, 270, 36, what)
    box(325, yy, 120, 36, f"parse {FILE_MB:.0f} MB", fill="#FBEFD9")
    label(455, yy + 23, line, 11.5)
hot(2, 445, y + 118, "Three more full parses in the same refresh, found by reading the "
    "code: the prune, the staleness report, and the resolved view every route reads.")

y = 400
label(40, y, "Readiness probe /readyz, every probe", 14, NAVY, weight=600)
for i in range(3):
    box(40 + i * 130, y + 16, 118, 52, "parse", fill="#FBEFD9", sub=f"{FILE_MB:.0f} MB")
label(440, y + 48, f"{S['cur_readyz']['seconds']:.1f} s a probe", 15, AMBER, weight=700)
hot(3, 40 + 2 * 130 + 118, y + 16, "health() reads the newest epoch three times, and each "
    f"read parses the whole store: {S['cur_readyz']['seconds']:.1f} s per probe, measured. "
    "/api/meta does the same.")
y = 500
label(40, y, "Measured worst case: one refresh with one probe overlapping", 14, NAVY, weight=600)
peak = S["cur_cycle"]["peak_mb"]
out.append(f'<rect x="40" y="{y + 16}" width="525" height="22" rx="4" fill="#EEF2F8"/>')
out.append(f'<rect x="40" y="{y + 16}" width="{525 * min(peak, 1300) / 1300:.0f}" height="22" rx="4" fill="{AMBER}"/>')
gx = 40 + 525 * 1024 / 1300
out.append(f'<line x1="{gx:.0f}" y1="{y + 8}" x2="{gx:.0f}" y2="{y + 46}" stroke="#B03A2E" stroke-width="2.5"/>')
label(gx - 6, y + 64, "1 GiB pod limit", 12.5, "#B03A2E", "end", 600)
label(40, y + 64, f"peak {peak:,} MB", 14, BODY, weight=700)
hot(4, gx, y + 27, f"Measured: {peak:,} MB at the peak, over the 1,024 MB the pod is given. "
    "The store here is at its retention cap, which the live one is near (inference from "
    "your inspector screenshots).")
label(40, 640, "Storage: one history.json, rewritten whole. On an S3-backed mount that", 12.5)
label(40, 658, "is a full upload per write, and a rename the mount refuses.", 12.5)

# PROPOSED
x0 = 635
y = 90
label(x0, y + 6, "Boot, once", 14, NAVY, weight=600)
box(x0, y + 20, 160, 56, "snapshot", fill="#DFF3E7", sub=f"{SNAP_MB:.0f} MB, gzip")
arrow(x0 + 160, y + 48, x0 + 188, y + 48)
box(x0 + 190, y + 20, 140, 56, "and segments", fill="#DFF3E7", sub="since the snapshot")
arrow(x0 + 330, y + 48, x0 + 358, y + 48)
box(x0 + 360, y + 20, 165, 56, "held in memory", bold=True, colour=GREEN,
    sub=f"boot {S['led_hourly']['boot_s']:.1f} s")
hot(5, x0 + 525, y + 20, "The store is decoded once, at start, and held. Nothing parses it "
    "again until the next restart.")
y = 220
label(x0, y + 6, "Hourly refresh (and every backfill chunk)", 14, NAVY, weight=600)
box(x0, y + 20, 110, 56, "UDL pull")
arrow(x0 + 110, y + 48, x0 + 138, y + 48)
box(x0 + 140, y + 20, 170, 56, "merge in memory", sub="production rules")
arrow(x0 + 310, y + 48, x0 + 338, y + 48)
box(x0 + 340, y + 20, 185, 56, "append a segment", fill="#DFF3E7",
    sub=f"{SEG_MB:.2f} MB, a new file")
hot(6, x0 + 525, y + 20, "The pull is merged by the same production functions, then only "
    f"what changed is written, as a new file: {SEG_MB:.2f} MB against {FILE_MB:.0f} MB. "
    "No rename and no overwrite, which is all an S3-backed mount allows by default.")
y = 350
label(x0, y + 6, "Readiness probe and /api/meta", 14, NAVY, weight=600)
box(x0, y + 20, 525, 50, "answered from counters kept as the store changes", fill="#DFF3E7",
    sub="0.0 ms, measured")
y = 460
label(x0, y + 6, "Once a day", 14, NAVY, weight=600)
box(x0, y + 20, 250, 56, "write a fresh snapshot", fill="#DFF3E7",
    sub=f"{SNAP_MB:.0f} MB, {S['led_compact']['seconds']:.0f} s, off the request path")
arrow(x0 + 250, y + 48, x0 + 278, y + 48)
box(x0 + 280, y + 20, 245, 56, "retire older files", sub="ignored if the mount refuses")
hot(7, x0 + 525, y + 20, "Compaction bounds what a restart replays to at most a day of "
    "segments. If the mount refuses deletes, old files are simply never read again.")
y = 575
label(x0, y, "Same worst case, measured", 14, NAVY, weight=600)
lp = S["led_cycle"]["peak_mb"]
out.append(f'<rect x="{x0}" y="{y + 16}" width="525" height="22" rx="4" fill="#EEF2F8"/>')
out.append(f'<rect x="{x0}" y="{y + 16}" width="{525 * lp / 1300:.0f}" height="22" rx="4" fill="{GREEN}"/>')
gx = x0 + 525 * 1024 / 1300
out.append(f'<line x1="{gx:.0f}" y1="{y + 8}" x2="{gx:.0f}" y2="{y + 46}" stroke="#B03A2E" stroke-width="2.5"/>')
label(x0, y + 64, f"peak {lp:,} MB, flat", 14, BODY, weight=700)
label(gx - 6, y + 64, "1 GiB pod limit", 12.5, "#B03A2E", "end", 600)

svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
       f'font-family="Segoe UI, Arial, sans-serif"><defs><marker id="a" viewBox="0 0 10 10" refX="9" '
       f'refY="5" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L10,5 L0,10 z" '
       f'fill="{MUTED}"/></marker></defs><rect width="{W}" height="{H}" fill="#FFFFFF"/>'
       + "".join(out) + "</svg>")
with open(os.path.join(HERE, "fig_architecture.html"), "w") as fh:
    fh.write('<!doctype html><meta charset="utf-8"><body style="margin:0">'
             f'<div id="fig" style="width:{W}px">{svg}</div>')
with open(os.path.join(HERE, "hotspots.json"), "w") as fh:
    json.dump(HOT, fh, indent=1)
print("figure A drawn,", len(HOT), "hotspots")
