// Shared by capture.js and capture_asis.js.
const banner = (p, text, top, colour) => p.evaluate(([t, y, c]) => {
  const d = document.createElement("div"); d.textContent = t; d.id = "mock-banner";
  d.style.cssText = `position:fixed;right:28px;top:${y}px;z-index:9999;background:${c};color:#fff;font:700 12px Segoe UI,sans-serif;letter-spacing:.08em;padding:6px 12px;border-radius:4px;pointer-events:none`;
  document.body.appendChild(d); }, [text, top, colour]);
const PROTO = "PROTOTYPE · NOT BUILT · SYNTHETIC SCENARIO";
const jpg = (path, clip) => (clip ? { path, clip, type: "jpeg", quality: 86 } : { path, type: "jpeg", quality: 86 });
async function open(chromium, base) {
  const b = await chromium.launch();
  const ctx = await b.newContext({ viewport: { width: 1600, height: 920 }, deviceScaleFactor: 1.5 });
  // last look 34 hours ago, so the brief has a split and the strip a shaded period
  await ctx.addInitScript((v) => { try { localStorage.setItem("psirens-last-look", v); } catch (e) { /* convenience */ } },
                          new Date(Date.now() - 34 * 3600e3).toISOString());
  const p = await ctx.newPage(); const errs = [];
  p.on("pageerror", e => errs.push(e.message)); p.on("console", m => { if (m.type() === "error") errs.push(m.text()); });
  await p.goto(base, { waitUntil: "networkidle" }); await p.waitForSelector(".bev", { timeout: 20000 }); await p.waitForTimeout(800);
  const box = await p.locator("#tline").boundingBox();
  return { b, p, errs, box, clip: { x: 0, y: box.y - 8, width: 1600, height: 300 } };
}
module.exports = { banner, PROTO, jpg, open };
