// Проверка экспорта: страница печати (preview) + реальный PDF через chromium.
import { chromium, devices } from 'playwright';

const OUT = process.argv[2];
const planId = process.argv[3];
const base = 'http://127.0.0.1:4200';

const browser = await chromium.launch();

// Превью на телефоне.
const ctx = await browser.newContext({ ...devices['iPhone 13'] });
const page = await ctx.newPage();
await page.goto(`${base}/print/${planId}`, { waitUntil: 'networkidle' });
await page.evaluate(() => document.fonts.ready);
await page.waitForSelector('.doc', { timeout: 45000 });
await page.waitForTimeout(400);
await page.screenshot({ path: `${OUT}/print-preview.png` });
console.log('preview captured');

// Реальный PDF (десктопный контекст, print media применяется автоматически в page.pdf()).
const ctx2 = await browser.newContext();
const page2 = await ctx2.newPage();
await page2.goto(`${base}/print/${planId}`, { waitUntil: 'networkidle' });
await page2.evaluate(() => document.fonts.ready);
await page2.waitForSelector('.doc', { timeout: 45000 });
await page2.waitForTimeout(500);
await page2.pdf({ path: `${OUT}/easy-week-plan.pdf`, format: 'A4', printBackground: true, margin: { top: '14mm', bottom: '14mm', left: '14mm', right: '14mm' } });
console.log('pdf generated');

await browser.close();
