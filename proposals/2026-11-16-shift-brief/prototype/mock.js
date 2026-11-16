"use strict";
// PROPOSAL MOCKUP ONLY. Injects the proposed Shift Brief into the RUNNING app
// and captures it. Nothing here ships. Every event and number is read from
// the prototype detector's events.json or from the store itself.
const fs = require("fs");
const { chromium } = require("playwright");
const [BASE, OUT, EVJ, HIST, HRR] = process.argv.slice(2);
const EV = JSON.parse(fs.readFileSync(EVJ, "utf8"));
const STORE = JSON.parse(fs.readFileSync(HIST, "utf8")).objects;
const END = new Date(EV.end).getTime();
const LAST_VISIT = Date.parse("2026-11-15T12:00:00Z");
const H = 3600e3;
const hrs = t => (END - Date.parse(t)) / H;
const ago = t => { const h = hrs(t); return h < 36 ? `${Math.round(h)} h` : `${(h / 24).toFixed(1)} d`; };
const last = id => { const s = STORE[id].samples; return s[s.length - 1]; };

// ---- event wording, from detector fields only --------------------------
const KIND = {
  STARTED:   { g: "⇆", c: "#f0a52e", t: "E-W manoeuvre" },
  EW:        { g: "⇆", c: "#f0a52e", t: "E-W manoeuvre" },
  STOPPED:   { g: "■", c: "#3fc1b0", t: "Drift stopped" },
  NS:        { g: "⇅", c: "#b48cff", t: "N-S manoeuvre" },
  SUSPECTED: { g: "?",      c: "#f0a52e", t: "Leaving its track" },
  QUIET:     { g: "◌", c: "#8ea3c4", t: "Gone quiet" },
};
const dir = r => (r < 0 ? "W" : "E");
function detail(e) {
  if (e.type === "STARTED" || e.type === "EW")
    return `now drifting ${Math.abs(e.after).toFixed(2)}°/day ${dir(e.after)}`;
  if (e.type === "STOPPED") return `was ${Math.abs(e.before).toFixed(2)}°/day ${dir(e.before)} · on station at ${last(e.id).sub_lon_deg.toFixed(1)}°`;
  if (e.type === "NS") return `inclination ${e.dinc > 0 ? "+" : ""}${e.dinc.toFixed(3)}°`;
  if (e.type === "SUSPECTED") return `departing its fitted track · confirms after 24 h of fixes`;
  if (e.type === "QUIET") return `no fix for ${Math.round(e.age_h)} h · rest of belt <1 h`;
  return "";
}
// When a QUIET object crossed the 12 h threshold is the event time an operator cares about.
const evTime = e => e.type === "QUIET" ? new Date(Date.parse(e.when) + 12 * H).toISOString() : e.when;
const events = EV.events.map(e => ({ ...e, name: e.name.replace(/ \(.*\)/, ""), t: evTime(e), k: KIND[e.type] }))
  .sort((a, b) => Date.parse(b.t) - Date.parse(a.t));
const fresh = events.filter(e => Date.parse(e.t) >= LAST_VISIT);
const older = events.filter(e => Date.parse(e.t) < LAST_VISIT);
const RANKC = { 1: "#E2483D", 2: "#E9863A", 3: "#D9B23A", 4: "#3E8FD0", 5: "#8199B8" };

// Projection for the SY-12 02 card: distance to SJ-21 at the confirmed rate.
const sy = events.find(e => e.id === "50322");
const syLon = last("50322").sub_lon_deg, sjLon = last("49330").sub_lon_deg;
const reachD = Math.abs(syLon - sjLon) / Math.abs(sy.after);
const projLine = `on this drift, reaches SJ-21’s longitude in ~${reachD.toFixed(1)} d`;

function row(e, sel) {
  const st = e.state === "SUSPECTED"
    ? `<span class="bst sus">SUSPECTED</span>` : `<span class="bst ok">CONFIRMED</span>`;
  const extra = e.id === "50322" ? `<div class="bx">↳ ${projLine}</div>` : "";
  return `<div class="brow${sel ? " sel" : ""}${Date.parse(e.t) >= LAST_VISIT ? " unread" : ""}">
    <span class="bg" style="color:${e.k.c};border-color:${e.k.c}${e.state === "SUSPECTED" ? ";border-style:dashed" : ""}">${e.k.g}</span>
    <div class="bm"><div class="bn"><span class="rk" style="background:${RANKC[e.rank]}">${e.rank}</span>${e.name}<span class="ba">${ago(e.t)}</span></div>
    <div class="bd"><b style="color:${e.k.c}">${e.k.t}</b> · ${detail(e)}</div>${extra}<div>${st}</div></div></div>`;
}
function briefHtml(selId) {
  return `<div class="card brief"><h2>Shift brief <span class="bnew">${fresh.length} new</span></h2>
  <div class="bsum"><span><b>150</b> in view</span><span><b>20</b> R1–R2</span><span><b>${events.length}</b> events · 4 d</span></div>
  <div class="bgp">Since you last looked · 15 Nov 12:00Z</div>
  ${fresh.map(e => row(e, e.id === selId)).join("")}
  <div class="bgp">Earlier</div>
  ${older.map(e => row(e, e.id === selId)).join("")}
  <div class="bkeys"><kbd>j</kbd><kbd>k</kbd> step · <kbd>Enter</kbd> focus · <kbd>Esc</kbd> back to belt</div></div>`;
}
const CSS = `
.brief{padding:12px 12px 10px}
.brief h2{display:flex;align-items:center;gap:8px}
.bnew{background:#f0a52e;color:#05070e;border-radius:9px;padding:1px 7px;font-size:9.5px;letter-spacing:.04em}
.bsum{display:flex;gap:10px;font:10px var(--mono);color:var(--dim);margin:0 0 8px;flex-wrap:wrap}
.bsum b{color:var(--ink);font-weight:600}
.bgp{font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--faint);margin:8px 0 4px;border-top:1px solid var(--line);padding-top:7px}
.brow{display:flex;gap:8px;padding:6px 6px;border-radius:6px;margin:0 -6px 2px;position:relative}
.brow.unread::before{content:"";position:absolute;left:-4px;top:13px;width:5px;height:5px;border-radius:50%;background:#f0a52e}
.brow.sel{background:#16233d;box-shadow:inset 0 0 0 1px #2b3d63}
.bg{flex:none;width:20px;height:20px;border:1.5px solid;border-radius:50%;display:grid;place-items:center;font-size:11px;font-weight:700;margin-top:1px}
.bm{min-width:0;flex:1}
.bn{display:flex;align-items:center;gap:5px;font-size:11.5px;font-weight:600;color:var(--ink)}
.brow:not(.unread) .bn{font-weight:500;color:#c7d4ea}
.rk{display:inline-grid;place-items:center;width:13px;height:13px;border-radius:50%;font-size:8.5px;color:#05070e;font-weight:700}
.ba{margin-left:auto;font:10px var(--mono);color:var(--faint)}
.bd{font-size:10.5px;color:var(--dim);line-height:1.35;margin-top:1px}
.bx{font-size:10px;color:#f0c27a;margin-top:2px}
.bst{display:inline-block;font:8.5px var(--mono);letter-spacing:.08em;border-radius:3px;padding:0 4px;margin-top:3px}
.bst.ok{color:#3fc1b0;border:1px solid #1f5b55}
.bst.sus{color:#f0a52e;border:1px dashed #6b4a14}
.bkeys{font-size:9.5px;color:var(--faint);margin-top:8px;border-top:1px solid var(--line);padding-top:7px}
.bkeys kbd{font:9px var(--mono);border:1px solid var(--line2);border-radius:3px;padding:0 3px;color:var(--dim)}
.tline{position:absolute;left:0;right:64px;top:0;height:76px;background:#0a1222f2;border-bottom:1px solid #1f2c47;z-index:4;pointer-events:none}
.mockstamp{position:fixed;left:50%;transform:translateX(-50%);top:22px;z-index:50;background:#2a1a05;border:1px solid #6b4a14;color:#f0c27a;font:600 11px var(--sans);letter-spacing:.12em;padding:4px 10px;border-radius:4px}
.evid{border:1px solid #2b3d63;border-radius:6px;padding:8px;margin:0 0 10px;background:#0b1322}
.evid h4{margin:0 0 4px;font-size:9.5px;letter-spacing:.14em;color:#f0a52e;text-transform:uppercase}
.evid .el{font-size:11px;color:var(--ink);margin:2px 0}
.evid .ef{font:10px var(--mono);color:var(--dim);margin-top:4px;line-height:1.4}
`;

// ---- geometry: mirror the app's own scale(), invX(), invY(), zoomAround()
const PAD = { L: 54, R: 18, T: 16, B: 54 };
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
function mapper(cvInfo, view) {
  const { w, h, r } = cvInfo, k = r.width / w;
  const xb = lon => PAD.L + (lon - view.lonMin) / (view.lonMax - view.lonMin) * (w - PAD.L - PAD.R);
  const yb = inc => h - PAD.B - (inc - view.incMin) / (view.incMax - view.incMin) * (h - PAD.T - PAD.B);
  return { x: lon => xb(lon) * k, y: inc => yb(inc) * k, k,
    invLon: cx => view.lonMin + ((cx * w / r.width) - PAD.L) / (w - PAD.L - PAD.R) * (view.lonMax - view.lonMin),
    invInc: cy => view.incMin + (h - PAD.B - cy * h / r.height) / (h - PAD.T - PAD.B) * (view.incMax - view.incMin) };
}
function zoomAround(view, cx, cy, k) {
  const LW = clamp((view.lonMax - view.lonMin) * k, 0.05, 360), IW = clamp((view.incMax - view.incMin) * k, 0.01, 15);
  const fx = (cx - view.lonMin) / (view.lonMax - view.lonMin), fy = (cy - view.incMin) / (view.incMax - view.incMin);
  const lonMin = clamp(cx - fx * LW, -180, 180 - LW), incMin = clamp(cy - fy * IW, 0, 15 - IW);
  return { lonMin, lonMax: lonMin + LW, incMin, incMax: incMin + IW };
}

// ---- in-page painters (serialised into the page) ------------------------
function paintTimeline(arg) {
  const { evs, start, end, lastVisit, selId } = arg;
  const host = document.getElementById("plot");
  let tl = document.querySelector(".tline"); if (tl) tl.remove();
  tl = document.createElement("div"); tl.className = "tline"; host.appendChild(tl);
  const W = tl.clientWidth, Hh = 76, d = devicePixelRatio, c = document.createElement("canvas");
  c.width = W * d; c.height = Hh * d; c.style.width = W + "px"; c.style.height = Hh + "px"; tl.appendChild(c);
  const g = c.getContext("2d"); g.scale(d, d);
  const x0 = 150, x1 = W - 28, GY = 44, AY = 58;
  const tx = t => x0 + (t - start) / (end - start) * (x1 - x0);
  // shaded "since you last looked" region
  g.fillStyle = "#16233d"; g.fillRect(tx(lastVisit), 4, x1 + 10 - tx(lastVisit), Hh - 8);
  g.strokeStyle = "#f0a52e"; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(tx(lastVisit), 4); g.lineTo(tx(lastVisit), Hh - 4); g.stroke(); g.setLineDash([]);
  // left key
  g.fillStyle = "#f0a52e"; g.font = "700 9.5px Segoe UI, sans-serif"; g.textAlign = "left";
  g.fillText("EVENTS", 14, 22);
  g.fillStyle = "#8ea3c4"; g.font = "9.5px Segoe UI, sans-serif";
  g.fillText("last 4 days", 14, 36);
  g.fillStyle = "#f0c27a"; g.fillText("▮ since you last", 14, 54); g.fillText("   looked", 14, 66);
  // axis
  g.strokeStyle = "#2b3d63"; g.lineWidth = 1; g.beginPath(); g.moveTo(x0, AY); g.lineTo(x1, AY); g.stroke();
  g.fillStyle = "#5b7093"; g.font = "9.5px Cascadia Mono, Consolas, monospace"; g.textAlign = "center";
  for (let t = Math.ceil(start / 864e5) * 864e5; t <= end; t += 864e5) {
    g.beginPath(); g.moveTo(tx(t), AY - 3); g.lineTo(tx(t), AY + 3); g.stroke();
    g.fillText(new Date(t).toISOString().slice(5, 10).replace("-", "/"), tx(t), AY + 13);
  }
  g.fillStyle = "#eaf1fb"; g.textAlign = "right"; g.fillText("now", x1, AY + 13);
  // labels on two rows, greedy, so neighbours never overprint
  const placed = [[], []];
  const sorted = evs.map(e => ({ ...e, X: tx(e.tms) })).sort((a, b) => a.X - b.X);
  sorted.forEach(e => {
    g.font = `${e.id === selId ? 700 : 500} 9.5px Segoe UI, sans-serif`;
    const w = g.measureText(e.short).width, L = e.X - w / 2, R = e.X + w / 2;
    let row = placed.findIndex(r => r.every(q => R + 6 < q[0] || L - 6 > q[1]));
    if (row < 0) row = 1;
    placed[row].push([L, R]); e.ly = row === 0 ? 14 : 26;
  });
  sorted.forEach(e => {
    const on = e.id === selId, r = on ? 9.5 : 7.5;
    g.strokeStyle = "#2b3d63"; g.beginPath(); g.moveTo(e.X, e.ly + 3); g.lineTo(e.X, GY - r); g.stroke();
    if (on) { g.strokeStyle = "#ffffff"; g.lineWidth = 1.5; g.beginPath(); g.arc(e.X, GY, r + 4, 0, 7); g.stroke(); }
    g.fillStyle = "#0a1222"; g.beginPath(); g.arc(e.X, GY, r, 0, 7); g.fill();
    g.strokeStyle = e.c; g.lineWidth = 1.6; if (e.sus) g.setLineDash([2.5, 2]);
    g.beginPath(); g.arc(e.X, GY, r, 0, 7); g.stroke(); g.setLineDash([]);
    g.fillStyle = e.c; g.font = `700 ${on ? 10.5 : 9}px Segoe UI, sans-serif`; g.textAlign = "center"; g.fillText(e.g, e.X, GY + (on ? 4 : 3.2));
    g.fillStyle = on ? "#ffffff" : "#c7d4ea"; g.font = `${on ? 700 : 500} 9.5px Segoe UI, sans-serif`;
    g.fillText(e.short, e.X, e.ly);
  });
}
function paintOverlay(arg) {
  const { cvr, rings, veil, trail, proj, labels, masks } = arg;
  let o = document.getElementById("mockov"); if (o) o.remove();
  o = document.createElement("canvas"); o.id = "mockov";
  const d = devicePixelRatio, host = document.getElementById("plot"), hr = host.getBoundingClientRect();
  Object.assign(o.style, { position: "absolute", left: (cvr.left - hr.left) + "px", top: (cvr.top - hr.top) + "px",
    width: cvr.width + "px", height: cvr.height + "px", pointerEvents: "none", zIndex: 3 });
  o.width = cvr.width * d; o.height = cvr.height * d; host.appendChild(o);
  const g = o.getContext("2d"); g.scale(d, d);
  if (veil) { g.fillStyle = "rgba(5,7,14,0.82)"; g.fillRect(0, 0, cvr.width, cvr.height); }
  // the app's own name labels sit under the veil; cover them so each name is drawn once
  (masks || []).forEach(([x, y]) => { g.fillStyle = "rgba(5,7,14,0.97)"; g.fillRect(x + 9, y - 21, 230, 15); });
  if (trail) {
    const seg = (pts, col, w, dash) => { g.strokeStyle = col; g.lineWidth = w; g.setLineDash(dash || []);
      g.beginPath(); pts.forEach((p, i) => (i ? g.lineTo(p[0], p[1]) : g.moveTo(p[0], p[1]))); g.stroke(); g.setLineDash([]); };
    seg(trail.before, "#7f93b3", 2);
    trail.before.forEach(p => { g.fillStyle = "#7f93b3"; g.beginPath(); g.arc(p[0], p[1], 1.8, 0, 7); g.fill(); });
    seg(trail.after, "#f0a52e", 2.6);
    trail.after.forEach(p => { g.fillStyle = "#f0a52e"; g.beginPath(); g.arc(p[0], p[1], 2, 0, 7); g.fill(); });
    const b = trail.burn; g.fillStyle = "#ffffff"; g.save(); g.translate(b[0], b[1]); g.rotate(Math.PI / 4); g.fillRect(-5, -5, 10, 10); g.restore();
  }
  if (proj) {
    g.strokeStyle = "#f0c27a"; g.lineWidth = 1.4; g.setLineDash([6, 5]);
    g.beginPath(); g.moveTo(proj.from[0], proj.from[1]); g.lineTo(proj.to[0], proj.to[1]); g.stroke(); g.setLineDash([]);
    g.fillStyle = "#f0c27a"; g.beginPath(); g.moveTo(proj.to[0], proj.to[1]); g.lineTo(proj.to[0] + 9, proj.to[1] - 5); g.lineTo(proj.to[0] + 9, proj.to[1] + 5); g.fill();
  }
  rings.forEach(r => {
    g.strokeStyle = r.c; g.lineWidth = r.w || 2; if (r.dash) g.setLineDash([3, 3]);
    g.beginPath(); g.arc(r.x, r.y, r.r || 12, 0, 7); g.stroke(); g.setLineDash([]);
    if (r.dot) { g.fillStyle = r.c; g.beginPath(); g.arc(r.x, r.y, 4.5, 0, 7); g.fill(); }
    if (r.g) {
      g.fillStyle = "#0a1222"; g.beginPath(); g.arc(r.x + 13, r.y - 13, 8, 0, 7); g.fill();
      g.strokeStyle = r.c; g.lineWidth = 1.4; if (r.dash) g.setLineDash([2.5, 2]); g.beginPath(); g.arc(r.x + 13, r.y - 13, 8, 0, 7); g.stroke(); g.setLineDash([]);
      g.fillStyle = r.c; g.font = "700 9.5px Segoe UI, sans-serif"; g.textAlign = "center"; g.fillText(r.g, r.x + 13, r.y - 9.5);
    }
  });
  labels.forEach(l => {
    g.font = `${l.b ? 600 : 500} ${l.s || 11}px Segoe UI, sans-serif`; g.textAlign = l.a || "left";
    g.strokeStyle = "rgba(4,8,16,.9)"; g.lineWidth = 3; g.lineJoin = "round"; g.strokeText(l.t, l.x, l.y);
    g.fillStyle = l.c || "#eaf1fb"; g.fillText(l.t, l.x, l.y);
  });
}

(async () => {
  const browser = await chromium.launch();
  const page = await (await browser.newContext({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 2 })).newPage();
  await page.goto(BASE, { waitUntil: "networkidle" });
  await page.waitForSelector("[data-id]"); await page.waitForTimeout(700);
  await page.screenshot({ path: OUT + "/A-as-is.png" });
  console.log("A-as-is.png");

  await page.addStyleTag({ content: CSS });
  const cvInfo = await page.evaluate(() => { const c = document.getElementById("cv");
    return { w: c.width, h: c.height, r: c.getBoundingClientRect().toJSON() }; });
  const view0 = { lonMin: -180, lonMax: 180, incMin: 0, incMax: 15 };
  const M0 = mapper(cvInfo, view0);
  const startMs = Date.parse(Object.values(STORE)[0].samples[0].epoch);
  const tlArg = sel => ({ evs: events.map(e => ({ id: e.id, tms: Date.parse(e.t), c: e.k.c, g: e.k.g, sus: e.state === "SUSPECTED",
      short: e.name })), start: startMs, end: END, lastVisit: LAST_VISIT, selId: sel,
  });

  // ---- B: overview ------------------------------------------------------
  await page.evaluate(html => { const rail = document.querySelector(".rail"), cards = rail.querySelectorAll(".card");
    [0, 1, 2].forEach(i => { cards[i].style.display = "none"; });
    rail.insertAdjacentHTML("afterbegin", html); }, briefHtml(null));
  await page.evaluate(paintTimeline, tlArg(null));
  const rings = events.map(e => { const s = last(e.id);
    return { x: M0.x(s.sub_lon_deg), y: M0.y(s.inclination_deg), c: e.k.c, r: 11, w: 2.2, dash: e.state === "SUSPECTED" || e.type === "QUIET", g: e.k.g }; });
  await page.evaluate(paintOverlay, { cvr: cvInfo.r, rings, veil: false, labels: [] });
  await page.evaluate(() => document.body.insertAdjacentHTML("beforeend", '<div class="mockstamp">PROPOSAL MOCKUP · ILLUSTRATIVE EVENTS ON SYNTHETIC GEOMETRY</div>'));
  await page.mouse.move(5, 5);
  await page.screenshot({ path: OUT + "/B-brief-overview.png" });
  console.log("B-brief-overview.png");

  // ---- C: focus on SHIYAN 12 02 -----------------------------------------
  await page.locator('.rail [data-id="50322"]').first().click();
  await page.waitForTimeout(1500);
  await page.evaluate(() => { const m = document.getElementById("imodal");
    m.style.top = "224px"; m.style.maxHeight = "calc(100vh - 236px)"; m.style.overflow = "auto"; });
  // Real zoom: 19 wheel ticks centred on (-48.5E, 0 deg), mirrored exactly.
  const px = Math.round(cvInfo.r.left + M0.x(-48.5)), py = Math.round(cvInfo.r.top + M0.y(0));
  let view = { ...view0 };
  await page.mouse.move(px, py);
  for (let i = 0; i < 19; i += 1) {
    const M = mapper(cvInfo, view);
    view = zoomAround(view, M.invLon(px - cvInfo.r.left), M.invInc(py - cvInfo.r.top), 0.8);
    await page.mouse.wheel(0, -100); await page.waitForTimeout(40);
  }
  await page.mouse.move(5, 5); await page.waitForTimeout(400);
  const M = mapper(cvInfo, view);
  console.log("view after zoom", JSON.stringify(view));
  const burnMs = Date.parse(sy.when);
  const pts = STORE["50322"].samples.map(s => ({ t: Date.parse(s.epoch), p: [M.x(s.sub_lon_deg), M.y(s.inclination_deg)] }));
  const before = pts.filter(q => q.t < burnMs).map(q => q.p), after = pts.filter(q => q.t >= burnMs).map(q => q.p);
  const head = after[after.length - 1], burn = after[0];
  const sjX = M.x(sjLon), sj = last("49330"), sjA = last("67302");
  await page.evaluate(paintOverlay, {
    cvr: cvInfo.r, veil: true,
    masks: [head, [M.x(sj.sub_lon_deg), M.y(sj.inclination_deg)], [M.x(sjA.sub_lon_deg), M.y(sjA.inclination_deg)]],
    trail: { before, after, burn },
    proj: { from: [head[0] - 8, head[1]], to: [sjX + 14, head[1]] },
    rings: [
      { x: head[0], y: head[1], c: "#f0a52e", r: 13, w: 2.4, dot: true },
      { x: M.x(sj.sub_lon_deg), y: M.y(sj.inclination_deg), c: "#E9863A", r: 9, w: 1.6, dot: true },
      { x: M.x(sjA.sub_lon_deg), y: M.y(sjA.inclination_deg), c: "#E9863A", r: 9, w: 1.6, dot: true },
    ],
    labels: [
      { t: "SHIYAN 12 02", x: head[0], y: head[1] - 44, b: 1, s: 13, a: "center" },
      { t: `now ${Math.abs(sy.after).toFixed(2)}\u00b0/day W`, x: head[0], y: head[1] - 28, c: "#f0c27a", s: 11.5, a: "center" },
      { t: "\u25c6 burn \u00b7 15 Nov 16:09Z", x: burn[0], y: burn[1] + 30, c: "#ffffff", b: 1, s: 11, a: "center" },
      { t: "on station before the burn", x: burn[0], y: burn[1] + 46, c: "#7f93b3", s: 10.5, a: "center" },
      { t: `reaches SJ-21\u2019s longitude in ~${reachD.toFixed(1)} d at this rate`, x: (head[0] + sjX) / 2, y: head[1] - 12, c: "#f0c27a", a: "center", b: 1, s: 11.5 },
      { t: "SJ-21", x: M.x(sj.sub_lon_deg), y: M.y(sj.inclination_deg) - 18, a: "center", c: "#eaf1fb", b: 1 },
      { t: "SJ-29A", x: M.x(sjA.sub_lon_deg) + 16, y: M.y(sjA.inclination_deg) + 4, a: "left", c: "#eaf1fb", b: 1 },
    ],
  });
  await page.evaluate(html => { const b = document.querySelector(".brief"); b.outerHTML = html; }, briefHtml("50322"));
  await page.evaluate(paintTimeline, tlArg("50322"));
  // Evidence block at the top of the REAL inspector.
  const series = STORE["50322"].samples.map(s => [Date.parse(s.epoch), s.sub_lon_deg]);
  await page.evaluate(arg => {
    const insp = document.getElementById("insp");
    const div = document.createElement("div"); div.className = "evid";
    div.innerHTML = `<h4>Event evidence</h4><div class="el"><b style="color:#f0a52e">⇆ E-W manoeuvre</b> · <span style="color:#3fc1b0">CONFIRMED</span></div>
      <canvas width="560" height="190" style="width:280px;height:95px;display:block;margin:6px 0"></canvas>
      <div class="ef">drift ${arg.rb}°/d → ${arg.ra}°/d (W)<br>burn 15 Nov 16:09Z · two 48 h fits, libration modelled<br>${arg.proj}</div>`;
    insp.prepend(div);
    const c = div.querySelector("canvas"), g = c.getContext("2d"); g.scale(2, 2);
    const s = arg.s, t0 = s[0][0], t1 = s[s.length - 1][0];
    const lo = Math.min(...s.map(p => p[1])) - 0.05, hi = Math.max(...s.map(p => p[1])) + 0.05;
    const X = t => 8 + (t - t0) / (t1 - t0) * 264, Y = v => 88 - (v - lo) / (hi - lo) * 80;
    g.strokeStyle = "#1b2740"; g.strokeRect(0.5, 0.5, 279, 94);
    g.strokeStyle = "#ffffff"; g.setLineDash([3, 3]); g.beginPath(); g.moveTo(X(arg.burn), 4); g.lineTo(X(arg.burn), 92); g.stroke(); g.setLineDash([]);
    s.forEach(p => { g.fillStyle = p[0] < arg.burn ? "#7f93b3" : "#f0a52e"; g.beginPath(); g.arc(X(p[0]), Y(p[1]), 1.6, 0, 7); g.fill(); });
    const fit = (a, b) => { const q = s.filter(p => p[0] >= a && p[0] < b), n = q.length;
      const mx = q.reduce((u, p) => u + p[0], 0) / n, my = q.reduce((u, p) => u + p[1], 0) / n;
      const sl = q.reduce((u, p) => u + (p[0] - mx) * (p[1] - my), 0) / q.reduce((u, p) => u + (p[0] - mx) ** 2, 0);
      return t => my + sl * (t - mx); };
    const fb = fit(arg.burn - 48 * 36e5, arg.burn), fa = fit(arg.burn, t1 + 1);
    g.lineWidth = 1.6; g.strokeStyle = "#8ea3c4"; g.beginPath(); g.moveTo(X(arg.burn - 48 * 36e5), Y(fb(arg.burn - 48 * 36e5))); g.lineTo(X(arg.burn), Y(fb(arg.burn))); g.stroke();
    g.strokeStyle = "#f0a52e"; g.beginPath(); g.moveTo(X(arg.burn), Y(fa(arg.burn))); g.lineTo(X(t1), Y(fa(t1))); g.stroke();
    g.fillStyle = "#5b7093"; g.font = "9px Consolas, monospace"; g.fillText("longitude °E", 12, 13); g.textAlign = "right"; g.fillText("4 days → now", 272, 13);
  }, { s: series, burn: burnMs, rb: sy.before.toFixed(2), ra: sy.after.toFixed(2), proj: projLine });
  await page.waitForTimeout(300);
  await page.screenshot({ path: OUT + "/C-event-focus.png" });
  console.log("C-event-focus.png");
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
