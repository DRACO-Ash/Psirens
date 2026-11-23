// Real interface, offline demo belt, focus on EXAMPLE-1's burn. Figure A is the
// interface as it is; figure B paints the proposed crossing over the SAME
// view, from mock.json (computed from the served data), with a MOCKUP banner.
// Usage: node capture.js <app-url> <mock.json> <figures-dir>
const { chromium } = require("playwright");
const fs = require("fs");
const [BASE, MOCK, OUT] = process.argv.slice(2);
const M = JSON.parse(fs.readFileSync(MOCK, "utf8"));
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1500, height: 900 }, deviceScaleFactor: 2 });
  const errs = []; p.on("pageerror", e => errs.push(e.message));
  await p.goto(BASE, { waitUntil: "networkidle" });
  await p.waitForSelector(`.bev[data-ev="${M.event_id}"]`, { timeout: 20000 });
  await p.click(`.bev[data-ev="${M.event_id}"]`);
  await p.waitForSelector("#evcv", { timeout: 10000 }); await p.waitForTimeout(800);
  const evid = p.locator(".evid").first();
  await evid.scrollIntoViewIfNeeded();
  const box = await evid.boundingBox();
  const clip = { x: box.x - 8, y: box.y - 8, width: box.width + 16, height: box.height + 16 };
  await p.screenshot({ path: `${OUT}/A-evidence-as-is.jpg`, clip, type: "jpeg", quality: 88 });
  await p.evaluate((m) => {
    const c = document.getElementById("evcv"), g = c.getContext("2d");
    const W = c.getBoundingClientRect().width, H = c.getBoundingClientRect().height;
    const t1 = Date.parse(m.series_last), t0 = t1 - 96 * 3600000;
    const X = t => 6 + (t - t0) / (t1 - t0) * (W - 12);
    const tc = Date.parse(m.cross), half = m.sigma_h * 3600000 * 2;
    g.fillStyle = "rgba(39,174,96,.22)";
    g.fillRect(X(tc - half), 4, Math.max(2, X(tc + half) - X(tc - half)), H - 16);
    g.strokeStyle = "#3fd27b"; g.lineWidth = 2; g.beginPath(); g.moveTo(X(tc), 4); g.lineTo(X(tc), H - 12); g.stroke();
    const z = iso => new Date(iso).toUTCString().slice(5, 11) + " " + new Date(iso).toISOString().slice(11, 16) + "Z";
    const em = document.querySelector(".evid .em");
    em.innerHTML = `drift ${m.rate_before.toFixed(2)} to ${m.rate_after.toFixed(2)}°/day<br>` +
      `<b style="color:#3fd27b">burn ${z(m.cross)} ±${(2 * m.sigma_h).toFixed(1)} h, where the two fits cross</b><br>` +
      `3-hour detection slot ${z(m.split)}<br>two independent 48 h fits, libration modelled where the sampling resolves it`;
    const d = document.createElement("div"); d.textContent = "MOCKUP · PROPOSED, NOT BUILT";
    d.style.cssText = "position:absolute;right:8px;bottom:6px;background:#C67C00;color:#fff;font:700 10px Segoe UI,sans-serif;letter-spacing:.08em;padding:3px 7px;border-radius:3px";
    const host = document.querySelector(".evid"); host.style.position = "relative"; host.style.paddingBottom = "30px"; host.appendChild(d);
  }, M);
  const box2 = await evid.boundingBox();
  await p.screenshot({ path: `${OUT}/B-evidence-proposed.jpg`, type: "jpeg", quality: 88,
    clip: { x: box2.x - 8, y: box2.y - 8, width: box2.width + 16, height: box2.height + 16 } });
  console.log("captured; page errors:", JSON.stringify(errs));
  await b.close();
})();
