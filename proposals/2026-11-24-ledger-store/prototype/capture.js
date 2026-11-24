// Photographs the two figure pages into ../figures/. Run from this folder:
//   python3 fig_architecture.py && node capture.js
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1240, height: 800 }, deviceScaleFactor: 1.5 });
  const figs = path.join(__dirname, "..", "figures");
  await page.goto("file://" + path.join(__dirname, "fig_architecture.html"));
  await page.locator("#fig").screenshot({ path: path.join(figs, "A-data-path.png") });
  const report = JSON.parse(fs.readFileSync(path.join(__dirname, "live-backfill-report.json"), "utf8"));
  await page.goto("file://" + path.join(__dirname, "fig_backfill.html"));
  await page.evaluate((r) => draw(r), report);
  await page.locator("#fig").screenshot({ path: path.join(figs, "B-live-backfill.png") });
  await browser.close();
  console.log("figures written");
})();
