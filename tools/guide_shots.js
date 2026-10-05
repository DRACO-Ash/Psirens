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
  const page = await browser.newPage({ viewport: VIEW, deviceScaleFactor: 1.5 });
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
    await shot(page.locator("#imodal"), "guide-inspector.png");
    await page.click("#cobtn");
    await page.waitForFunction(
      () => document.getElementById("comodal").classList.contains("show")
         && document.getElementById("comsg").textContent.trim() === "",
      null, { timeout: 20000 }).catch(() => {});
    await settle(page);
    await shot(page.locator("#comodal"), "guide-coplanar.png");
    await page.click("#comodal-x");
    await page.click("#imodal-x");
  }

  // The country panel acting as a filter, with a country selected.
  const first = page.locator('[data-cc]:not([data-cc=""])').first();
  await first.click();
  await settle(page);
  await shot(page.locator(".rail"), "guide-country.png");

  await browser.close();
  console.log(shots.join("\n"));
})().catch((e) => { console.error(e); process.exit(1); });
