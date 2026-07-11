// E2E-демо: чат генерит план через бэкенд, затем открываем блюдо (ленивые шаги).
import { chromium, devices } from 'playwright';

const OUT = process.argv[2] ?? '.';
const base = 'http://127.0.0.1:4200';

const browser = await chromium.launch();
const ctx = await browser.newContext({ ...devices['iPhone 13'] });
const page = await ctx.newPage();
page.on('console', (m) => {
  if (m.type() === 'error') console.log('PAGE ERROR:', m.text().slice(0, 160));
});

await page.goto(base + '/chat', { waitUntil: 'networkidle' });
await page.evaluate(() => document.fonts.ready);

await page.fill('.composer__input', '5 ужинов на неделю, без свинины, впрок на 2-4 порции');
await page.click('.composer__send');

// Ждём карточку плана (реальная генерация ~6-10с).
await page.waitForSelector('.plan', { timeout: 40000 });
await page.waitForTimeout(400);
await page.evaluate(() => {
  const s = document.querySelector('.chat__stream');
  if (s) s.scrollTop = s.scrollHeight;
});
await page.waitForTimeout(300);
await page.screenshot({ path: `${OUT}/live-plan.png` });
console.log('plan captured');

// Открываем первое блюдо → ленивые шаги.
const firstDish = await page.$('.dish__main');
await firstDish.click();
await page.waitForSelector('.spinner', { timeout: 5000 }).catch(() => {});
await page.waitForSelector('.steps .step', { timeout: 40000 });
await page.waitForTimeout(400);
await page.screenshot({ path: `${OUT}/live-dish.png` });
console.log('dish captured');

await browser.close();
