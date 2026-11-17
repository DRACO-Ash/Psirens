// Capture the Operator Guide's figures from the RUNNING app.
//
// Every figure in the guide is a screenshot of the real interface, driven the
// way an operator drives it: click the row, open the panel, press the button.
// Nothing here draws a mock-up. The DATA behind the figures is illustrative
// (see tools/guide_seed.py) and the guide says so beside each figure.
//
// Usage: node tools/guide_shots.js <base-url> <out-dir> [stale]
"use strict";
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const BASE = process.argv[2] || "http://127.0.0.1:8139";
const OUT = process.argv[3] || "src/psirens/static";
const STALE_ONLY = process.argv[4] === "stale";
const VIEW = { width: 1600, height: 920 };

const shots = [];
async function shot(target, name, opts) {
  const file = path.join(OUT, name);
  await target.screenshot(Object.assign({ path: file }, opts || {}));
  const kb = Math.round(fs.statSync(file).size / 1024);
  shots.push(`${name} ${kb}KB`);
}

async function settle(page) {
  await page.waitForTimeout(900);
}

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 1.5 });
  // A previous visit 34 hours ago, so the Shift Brief figure shows both halves:
  // new since you last looked, and earlier.
  const lastLook = new Date(Date.now() - 34 * 3600e3).toISOString();
  await ctx.addInitScript((v) => { try { localStorage.setItem("psirens-last-look", v); } catch (e) { /* figures only */ } }, lastLook);
  const page = await ctx.newPage();
  page.on("pageerror", (e) => { console.log("  page error: " + e.message); });
  await page.goto(BASE, { waitUntil: "networkidle" });
  await page.waitForSelector("[data-id]", { timeout: 20000 });
  await settle(page);

  if (STALE_ONLY) {
    await page.waitForSelector("#stale:not([hidden])", { timeout: 20000 });
    await shot(page.locator("#stale"), "guide-stale.png");
    await browser.close();
    console.log(shots.join("\n"));
    return;
  }

  await shot(page, "guide-overview.png");
  await shot(page.locator(".bar"), "guide-bar.png");
  await shot(page.locator(".rail"), "guide-rail.png");
  await shot(page.locator(".stage"), "guide-plot.png");
  await shot(page.locator(".legend"), "guide-legend.png");
  await shot(page.locator(".top"), "guide-top.png");
  await shot(page.locator("#briefcard"), "guide-brief.png");
  console.log("RAILGEOM " + JSON.stringify(await page.evaluate(() => {
    const rr = document.querySelector(".rail").getBoundingClientRect();
    const rel = (e) => { const r = e.getBoundingClientRect();
      return [(r.left - rr.left) / rr.width * 100, (r.top - rr.top) / rr.height * 100,
              r.width / rr.width * 100, r.height / rr.height * 100].map((v) => +v.toFixed(1)); };
    const bb = document.getElementById("briefcard").getBoundingClientRect();
    const relb = (e) => { const r = e.getBoundingClientRect();
      return [(r.left - bb.left) / bb.width * 100, (r.top - bb.top) / bb.height * 100,
              r.width / bb.width * 100, r.height / bb.height * 100].map((v) => +v.toFixed(1)); };
    return { cards: Array.from(document.querySelectorAll(".rail > .card")).map(rel),
             brief: { head: relb(document.querySelector("#briefcard h2")),
                      rows: Array.from(document.querySelectorAll(".bev")).map((e) => [e.textContent.slice(0, 30), ...relb(e)]),
                      secs: Array.from(document.querySelectorAll(".bsec")).map(relb),
                      keys: relb(document.querySelector(".bkeys")) } };
  })));

  // Focus mode (1.7.0), reached the way the guide teaches it: step the brief
  // with j until the E-W manoeuvre is selected, then Enter.
  await page.mouse.click(5, 5);
  for (let i = 0; i < 8; i++) {
    await page.keyboard.press("j");
    const cur = await page.evaluate(() => {
      const c = document.querySelector(".bev.cur"); return c ? c.textContent : ""; });
    if (/E-W manoeuvre/.test(cur)) { break; }
  }
  await page.keyboard.press("Enter");
  await page.waitForSelector("#evcv", { timeout: 10000 }).catch(() => {});
  await settle(page);
  if (!(await page.$eval("#cv", (e) => e.dataset.focus))) {
    console.log("FAIL: focus mode did not engage, the figure would show the belt");
    process.exitCode = 1;
  }
  await shot(page, "guide-focus.png");
  // Hotspot geometry for the guide, as percentages of each figure, measured
  // from the live layout rather than guessed from a picture.
  console.log("GEOMETRY " + JSON.stringify(await page.evaluate(() => {
    const W = innerWidth, H = innerHeight, pc = (r) => [r.left / W * 100, r.top / H * 100, r.width / W * 100, r.height / H * 100].map((v) => +v.toFixed(1));
    const q = (s) => { const e = document.querySelector(s); return e ? pc(e.getBoundingClientRect()) : null; };
    return { brief: q("#briefcard"), tline: q("#tline"), focusbar: q("#focusbar"), imodal: q("#imodal"), evid: q(".evid"), watch: q("#watch"), plot: q("#plot") };
  })));
  await page.keyboard.press("Escape");
  await page.click("#imodal-x");
  await settle(page);

  // The inspector, opened from a watchlist row exactly as an operator would.
  // Only rows that are actually on screen above the footer: with the full
  // high-interest list seeded the watchlist is long, and a row scrolled under
  // the footer cannot be clicked the way an operator would click it.
  const ids = await page.locator("[data-id]").evaluateAll(
    (els, limit) => els.filter((e) => {
      const b = e.getBoundingClientRect();
      return b.top > 90 && b.bottom < limit;
    }).map((e) => e.dataset.id), VIEW.height - 70);
  if (!ids.length) {
    console.log("FAIL: no watchlist row is clickable on screen");
    process.exitCode = 1;
  }
  let opened = null;
  for (const id of ids.slice(0, 8)) {
    await page.locator(`[data-id="${id}"]`).first().click();
    await page.waitForSelector("#imodal.show", { timeout: 10000 }).catch(() => {});
    // The neighbourhood is an SGP4 screen over every object within 10 degrees,
    // so it lands well after the panel does. Wait for the result, not a timer.
    await page.waitForFunction(
      () => !(document.getElementById("nb").textContent || "")
        .includes("computing"), null, { timeout: 30000 }).catch(() => {});
    await settle(page);
    const neighbours = await page.locator("#nb .nb2").count();
    if (neighbours > 2) { opened = id; break; }
  }
  if (opened === null) {
    console.log("FAIL: no object had neighbours, the inspector figure would be empty");
    process.exitCode = 1;
  } else {
    // Open the first neighbour so the figure shows the closest-approach line
    // and the TLE block an operator actually reads, not just a collapsed row.
    await page.locator("#nb .nb2 summary").first().click();
    await settle(page);
    // A TALL viewport for this one figure, so the whole panel fits unscrolled.
    // Until 1.7.4 it was captured at 920 px, which scrolled the panel past its
    // identity and state rows while the guide's hotspots still pointed at
    // them. The hotspot positions are now MEASURED here and printed, so the
    // guide's table is copied from the capture rather than estimated.
    await page.setViewportSize({ width: VIEW.width, height: 1900 });
    await settle(page);
    await page.evaluate(() => { const m = document.getElementById("imodal"); m.scrollTop = 0;
      m.querySelectorAll("*").forEach((e) => { e.scrollTop = 0; }); });
    await page.waitForFunction(
      () => !(document.getElementById("nb").textContent || "").includes("computing"),
      null, { timeout: 30000 }).catch(() => {});
    // The resize re-renders the neighbourhood, which collapses the row opened
    // above; open it again so the figure carries a neighbour's TLE block.
    const firstNb = page.locator("#nb .nb2").first();
    if (!(await firstNb.evaluate((d) => d.open))) { await firstNb.locator("summary").click(); }
    await settle(page);
    // Clip below the third neighbour: the full list of twenty adds nothing.
    const box = await page.evaluate(() => {
      const m = document.getElementById("imodal").getBoundingClientRect();
      const rows = document.querySelectorAll("#nb .nb2");
      const last = rows[Math.min(2, rows.length - 1)].getBoundingClientRect();
      return { x: m.left, y: m.top, width: m.width, height: last.bottom + 10 - m.top };
    });
    await shot(page, "guide-inspector.png", { clip: box });
    const spots = await page.evaluate((clip) => {
      const pct = (r) => [((r.left - clip.x) / clip.width) * 100, ((r.top - clip.y) / clip.height) * 100,
        (r.width / clip.width) * 100, (r.height / clip.height) * 100].map((v) => +v.toFixed(1));
      const out = {};
      const add = (k, el) => { if (el) { out[k] = pct(el.getBoundingClientRect()); } };
      add("evidence", document.querySelector("#imodal .evbox, #imodal [class*=evid]"));
      add("identity", document.querySelector("#imodal .ins-h"));
      document.querySelectorAll("#imodal .ins-dl dt").forEach((dt) => {
        const dd = dt.nextElementSibling; const a = dt.getBoundingClientRect(), b = dd.getBoundingClientRect();
        out[dt.textContent.trim()] = pct({ left: a.left, top: Math.min(a.top, b.top), width: b.right - a.left,
          height: Math.max(a.bottom, b.bottom) - Math.min(a.top, b.top) });
      });
      add("cobtn", document.getElementById("cobtn"));
      add("tgttle", document.getElementById("tgttle"));
      add("nbhead", document.querySelector("#imodal .ins-nb-h"));
      add("neighbour", document.querySelector("#nb .nb2"));
      add("nbtle", document.querySelector("#nb .nb2 .nb2bd"));
      return out;
    }, box);
    console.log("INSPECTOR HOTSPOTS " + JSON.stringify(spots));
    await page.setViewportSize(VIEW);
    await settle(page);
    await page.click("#cobtn");
    await page.waitForFunction(
      () => document.getElementById("comodal").classList.contains("show")
         && document.getElementById("comsg").textContent.trim() === "",
      null, { timeout: 20000 }).catch(() => {});
    await settle(page);
    // Select a point so the figure shows the detail panel (1.6.17), not just
    // the scatter. The sweep uses the page's own hit test rather than
    // recomputing the plot's padding and log scales, which would drift apart.
    const spot = await page.evaluate(() => {
      const cv = document.getElementById("cocanvas");
      const b = cv.getBoundingClientRect();
      for (let y = 0; y < b.height; y += 7) {
        for (let x = 0; x < b.width; x += 7) {
          cv.style.cursor = "default";
          cv.dispatchEvent(new MouseEvent("mousemove", {
            clientX: b.left + x, clientY: b.top + y, bubbles: true }));
          if (cv.style.cursor === "pointer") { return { x: b.left + x, y: b.top + y }; }
        }
      }
      return null;
    });
    if (spot === null) {
      console.log("FAIL: no co-planar point was clickable, the figure would " +
                  "not show the detail panel");
      process.exitCode = 1;
    } else {
      await page.mouse.click(spot.x, spot.y);
      await settle(page);
    }
    await shot(page.locator("#comodal"), "guide-coplanar.png");
    await page.click("#comodal-x");
    await page.click("#imodal-x");
  }

  // The country panel acting as a filter, with a country selected. Collapsed
  // below the watchlist from 1.7.0, so open it first, as an operator would.
  await page.click("#foldcountry > summary");
  const first = page.locator('[data-cc]:not([data-cc=""])').first();
  await first.click();
  await settle(page);
  await shot(page.locator("#foldcountry"), "guide-country.png");

  await browser.close();
  console.log(shots.join("\n"));
})().catch((e) => { console.error(e); process.exit(1); });
