// Drive the PROTOTYPE timeline (timeline.patch applied to the 1.7.3 SPA)
// through every state the proposal shows, with real mouse and wheel events,
// and print what the canvas publishes in its dataset at each step.
// Usage: node capture.js <app-url> <figures-dir>
const { chromium } = require("playwright");
const { banner, PROTO, jpg, open } = require("./shots.js");
const [BASE, OUT] = process.argv.slice(2);
(async () => {
  const { b, p, errs, box, clip } = await open(chromium, BASE);
  const ds = () => p.$eval("#tlcv", e => ({ ...e.dataset }));
  const mid = box.y + 44;
  await banner(p, PROTO, box.y + 300 - 46, "#C67C00");
  await p.screenshot(jpg(`${OUT}/B-full.jpg`, clip)); console.log("full", await ds());
  const hit = +(await ds()).biggest;                       // the largest cluster
  await p.mouse.move(box.x + hit, mid); await p.waitForTimeout(200);
  await p.screenshot(jpg(`${OUT}/C-hover.jpg`, clip));
  await p.mouse.down(); await p.mouse.up(); await p.waitForTimeout(250);
  console.log("pinned rows", await p.$$eval(".tltip .tlrow", r => r.length));
  await p.screenshot(jpg(`${OUT}/D-list.jpg`, clip));
  await p.locator(".tltip .tlrow").nth(1).click(); await p.waitForTimeout(700);
  console.log("focus", await p.$eval("#cv", e => e.dataset.focus));
  await p.screenshot(jpg(`${OUT}/E-focus-strip.jpg`, clip));
  await p.evaluate(() => { document.getElementById("mock-banner").style.top = "870px"; });
  await p.screenshot(jpg(`${OUT}/H-focus-app.jpg`));
  await p.evaluate(top => { document.getElementById("mock-banner").style.top = top + "px"; }, box.y + 300 - 46);
  await p.keyboard.press("Escape"); await p.waitForTimeout(300);
  await p.mouse.move(box.x + box.width - 40, mid);         // into the newest-end pile-up
  for (let i = 0; i < 7; i++) { await p.mouse.wheel(0, -100); await p.waitForTimeout(60); }
  await p.mouse.move(box.x + 60, box.y + 10); await p.waitForTimeout(100);
  console.log("wheel", await ds());
  await p.screenshot(jpg(`${OUT}/F-zoom.jpg`, clip));
  await p.mouse.move(box.x + box.width / 2, mid);
  await p.mouse.down(); await p.mouse.move(box.x + box.width / 2 + 300, mid, { steps: 8 }); await p.mouse.up();
  console.log("panned", await ds());
  await p.dblclick("#tlcv", { position: { x: 400, y: 20 } }); await p.waitForTimeout(100);
  console.log("reset", await ds());
  await p.click('#tlzoom [data-tlspan="24"]'); await p.waitForTimeout(150);
  await p.screenshot(jpg(`${OUT}/G-24h.jpg`, clip)); console.log("24h", await ds());
  console.log("errors", errs);
  await b.close();
})();
