// The SHIPPED 1.7.3 timeline, same scenario, same clip as capture.js, so the
// compare slider sets like against like. Serve the UNMODIFIED index.html.
// Usage: node capture_asis.js <app-url> <figures-dir>
const { chromium } = require("playwright");
const { banner, jpg, open } = require("./shots.js");
const [BASE, OUT] = process.argv.slice(2);
(async () => {
  const { b, p, box, clip } = await open(chromium, BASE);
  await banner(p, "AS-IS · SHIPPED 1.7.3 · SAME SCENARIO", box.y + 300 - 46, "#4A5D73");
  await p.screenshot(jpg(`${OUT}/A-as-is.jpg`, clip));
  console.log("as-is glyphs", await p.$eval("#tlcv", e => e.dataset.glyphs));
  await b.close();
})();
