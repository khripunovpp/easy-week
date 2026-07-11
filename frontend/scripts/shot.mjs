// Скриншот экрана на мобильном вьюпорте + диагностика горизонтального переполнения.
// Использование: node scripts/shot.mjs <path> <out.png>
import { chromium, devices } from 'playwright';

const path = process.argv[2] ?? '/chat';
const out = process.argv[3] ?? 'shot.png';
const base = process.env.BASE ?? 'http://127.0.0.1:4200';

const browser = await chromium.launch();
const context = await browser.newContext({ ...devices['iPhone 13'] });
const page = await context.newPage();

await page.goto(base + path, { waitUntil: 'networkidle' });
await page.evaluate(() => document.fonts.ready);
await page.waitForTimeout(600);

// CLICK=<selector> CLICKN=<n> — кликнуть первые n элементов (для демо-состояний).
if (process.env.CLICK) {
  const n = Number(process.env.CLICKN ?? 1);
  const els = await page.$$(process.env.CLICK);
  for (let i = 0; i < Math.min(n, els.length); i++) {
    await els[i].click();
  }
  await page.waitForTimeout(250);
}

// SCROLL=bottom|<selector> — прокрутить контейнер перед снимком.
if (process.env.SCROLL) {
  const sel = process.env.SCROLL === 'bottom' ? '.chat__stream' : process.env.SCROLL;
  await page.evaluate((s) => {
    const el = document.querySelector(s);
    if (el) el.scrollTop = el.scrollHeight;
  }, sel);
  await page.waitForTimeout(400);
}

// Диагностика: что шире вьюпорта?
const diag = await page.evaluate(() => {
  const vw = document.documentElement.clientWidth;
  const overflow = [];
  for (const el of document.querySelectorAll('*')) {
    const r = el.getBoundingClientRect();
    if (r.right > vw + 0.5 || r.left < -0.5) {
      overflow.push(
        `${el.tagName.toLowerCase()}.${(el.className || '').toString().split(' ')[0]} ` +
          `left=${r.left.toFixed(0)} right=${r.right.toFixed(0)} w=${r.width.toFixed(0)}`
      );
    }
  }
  return { vw, scrollWidth: document.documentElement.scrollWidth, overflow: overflow.slice(0, 12) };
});
console.log('viewport =', diag.vw, 'scrollWidth =', diag.scrollWidth);
console.log('overflowing:', diag.overflow.length ? diag.overflow : 'none');

await page.screenshot({ path: out });
await browser.close();
