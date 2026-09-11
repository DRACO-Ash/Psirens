/**
 * The layout guard. Drives the served page in a real browser and asserts the
 * co-planar modal resizes in BOTH directions without the canvas ratcheting.
 *
 * This exists because version 1.6.2 reached Active on the App Store carrying a
 * live layout defect, and the check written to catch it asserted only that the
 * canvas CSS box changed after a resize. That is true by construction, because
 * the box is the thing being resized, so the assertion could never fail and it
 * passed while the bug was live. The owner found it, not the build.
 *
 * The bug: `.comodal-bd` is a flex item, so it defaulted to `min-height:auto`,
 * which takes the CANVAS ELEMENT'S width/height ATTRIBUTES as an intrinsic
 * floor. `sizeCo` sets those attributes from the measured box on every redraw,
 * so the body could only ratchet larger. It overflowed the modal and
 * `overflow:hidden` clipped the plot, which read as a zoom.
 *
 * So the three assertions below are chosen to make that specific failure
 * impossible to miss, and each one CAN fail:
 *   1. the backing store matches the box (a stale store means a stretched plot);
 *   2. the body never exceeds the modal (the overflow that clipped the plot);
 *   3. shrinking actually shrinks (the ratchet only showed on the way down).
 *
 * Usage: node tools/layout_probe.js <base-url>
 * Needs playwright and a browser; the loop treats an unrunnable probe as a
 * failure unless ALLOW_SKIPPED_LAYOUT_PROBE=1.
 */
"use strict";

const BASE = process.argv[2] || "http://127.0.0.1:8123";
const TOL = 1.5; // px: rounding in sizeCo, not slack for a real mismatch

function fail(msg) {
  console.log("FAIL: " + msg);
  process.exitCode = 1;
}

async function measure(page, label) {
  const m = await page.evaluate(() => {
    const modal = document.getElementById("comodal");
    const body = document.querySelector("#comodal .comodal-bd");
    const cv = document.getElementById("cocanvas");
    const mr = modal.getBoundingClientRect();
    const br = body.getBoundingClientRect();
    const cr = cv.getBoundingClientRect();
    const dpr = Math.min(2, globalThis.devicePixelRatio || 1);
    return {
      modalH: mr.height, modalW: mr.width,
      bodyH: br.height, bodyW: br.width,
      boxH: cr.height, boxW: cr.width,
      storeH: cv.height, storeW: cv.width,
      dpr,
      rendered: document.getElementById("comsg").textContent.trim() === "",
    };
  });
  console.log(
    `  ${label.padEnd(14)} modal ${Math.round(m.modalW)}x${Math.round(m.modalH)}` +
    `  body ${Math.round(m.bodyW)}x${Math.round(m.bodyH)}` +
    `  box ${Math.round(m.boxW)}x${Math.round(m.boxH)}` +
    `  store ${m.storeW}x${m.storeH}`);
  return m;
}

async function setModal(page, w, h) {
  await page.evaluate(([ww, hh]) => {
    const m = document.getElementById("comodal");
    m.style.width = ww + "px";
    m.style.height = hh + "px";
  }, [w, h]);
  await page.waitForTimeout(250); // ResizeObserver, then the redraw
}

function checkBackingStore(m, label) {
  const wantW = Math.round(m.boxW * m.dpr);
  const wantH = Math.round(m.boxH * m.dpr);
  if (Math.abs(m.storeW - wantW) > TOL || Math.abs(m.storeH - wantH) > TOL) {
    fail(`${label}: canvas backing store ${m.storeW}x${m.storeH} does not match ` +
         `its box ${wantW}x${wantH} (dpr ${m.dpr}). The plot is drawn at one ` +
         `size and displayed at another.`);
  }
}

function checkBodyFits(m, label) {
  if (m.bodyH > m.modalH + TOL) {
    fail(`${label}: modal body is ${Math.round(m.bodyH)}px tall inside a ` +
         `${Math.round(m.modalH)}px modal. It has overflowed, and ` +
         `overflow:hidden is clipping the plot. This is the 1.6.2 ratchet.`);
  }
}

(async () => {
  let chromium;
  try {
    ({ chromium } = require("playwright"));
  } catch (e) {
    console.log("UNAVAILABLE: playwright is not resolvable (" + e.code + ")");
    process.exit(2); // distinct from a failure: the loop decides what to do
  }
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1400, height: 900 } });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (c) => { if (c.type() === "error") { errors.push(c.text()); } });

  try {
    await page.goto(BASE, { waitUntil: "networkidle" });

    // Drive the real controls: watchlist row, then the inspector's Co-Planar
    // button. Playwright's evaluate cannot see the page's top-level bindings,
    // and that is a feature here: the probe can only reach the app the way an
    // operator does.
    await page.waitForSelector("[data-id]", { timeout: 15000 });
    const ids = await page.locator("[data-id]").evaluateAll(
      (els) => els.map((e) => e.dataset.id));
    if (!ids.length) {
      fail("the watchlist is empty, so there is nothing to select.");
      throw new Error("no watchlist");
    }

    // Prefer an object that actually has neighbours: an empty chart would make
    // every assertion below vacuous.
    let opened = null;
    for (const id of ids) {
      await page.locator(`[data-id="${id}"]`).first().click();
      await page.waitForSelector("#cobtn", { timeout: 10000 });
      await page.click("#cobtn");
      await page.waitForFunction(
        () => document.getElementById("comodal").classList.contains("show")
           && document.getElementById("comsg").textContent.trim() === ""
           && document.getElementById("cocanvas").getBoundingClientRect().width > 2,
        null, { timeout: 15000 }).catch(() => {});
      const title = await page.textContent("#comodal-ttl");
      const n = /\((\d+) in/.exec(title || "");
      if (n && Number(n[1]) > 0) { opened = { id, title }; break; }
      await page.click("#comodal-x");
    }
    if (!opened) {
      fail("no watchlist object has a co-planar neighbour, so the chart would " +
           "be empty and the resize assertions vacuous.");
      throw new Error("no populated target");
    }
    console.log("  target: " + opened.id + "  " + opened.title.trim());
    await page.waitForTimeout(400); // the fetch, then the first sizeCo

    const open = await measure(page, "at open");
    // Guard against the probe passing on an empty modal: if the chart never
    // loaded, nothing below is exercising the code under test.
    if (!open.rendered) {
      fail("the co-planar chart did not render, so the resize assertions below " +
           "would be vacuous. Check /api/coplanar for the selected object.");
      throw new Error("chart did not render");
    }
    checkBackingStore(open, "at open");
    checkBodyFits(open, "at open");

    await setModal(page, 900, 800);
    const grown = await measure(page, "after grow");
    checkBackingStore(grown, "after grow");
    checkBodyFits(grown, "after grow");

    await setModal(page, 520, 420);
    const shrunk = await measure(page, "after shrink");
    checkBackingStore(shrunk, "after shrink");
    checkBodyFits(shrunk, "after shrink");

    // The ratchet only ever showed on the way down: the body grew with the
    // modal and then refused to come back.
    if (shrunk.bodyH >= grown.bodyH - TOL) {
      fail(`the body did not shrink: ${Math.round(grown.bodyH)}px at a 800px ` +
           `modal, still ${Math.round(shrunk.bodyH)}px at a 420px modal. The ` +
           `canvas attributes are acting as an intrinsic floor again.`);
    }
    if (errors.length) {
      fail("page errors during the probe: " + errors.slice(0, 3).join(" | "));
    }
    if (!process.exitCode) {
      console.log("LAYOUT PROBE OK (backing store tracks the box, body fits the " +
                  "modal, and shrinking shrinks)");
    }
  } catch (e) {
    fail("probe could not complete: " + String(e).split("\n")[0]);
  } finally {
    await browser.close();
  }
})();
