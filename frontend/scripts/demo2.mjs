// Живой цикл: генерация → Принять → вкладка Планы → вкладка Покупки.
import { chromium, devices } from 'playwright';

const OUT = process.argv[2] ?? '.';
const base = 'http://127.0.0.1:4200';

const browser = await chromium.launch();
const ctx = await browser.newContext({ ...devices['iPhone 13'] });
const page = await ctx.newPage();

await page.goto(base + '/chat', { waitUntil: 'networkidle' });
await page.evaluate(() => document.fonts.ready);

await page.fill('.composer__input', '4 ужина, без свинины, впрок на 2-4 порции');
await page.click('.composer__send');
await page.waitForSelector('.plan', { timeout: 40000 });

// Принять план.
await page.evaluate(() => {
  const s = document.querySelector('.chat__stream');
  if (s) s.scrollTop = s.scrollHeight;
});
await page.click('.plan__accept');
await page.waitForSelector('.plan__decided .tag--ok', { timeout: 10000 });
await page.waitForTimeout(300);
await page.screenshot({ path: `${OUT}/live-accepted.png` });
console.log('accepted captured');

// Вкладка Планы.
await page.click('a.tab[aria-label="Планы"]');
await page.waitForSelector('.prow', { timeout: 15000 });
await page.waitForTimeout(400);
await page.screenshot({ path: `${OUT}/live-plans.png` });
console.log('plans captured');

// Вкладка Покупки.
await page.click('a.tab[aria-label="Покупки"]');
await page.waitForSelector('.row', { timeout: 15000 });
await page.waitForTimeout(400);
await page.screenshot({ path: `${OUT}/live-shopping.png` });
console.log('shopping captured');

await browser.close();
