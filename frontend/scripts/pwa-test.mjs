// Проверка PWA: регистрация service worker, манифест и работа офлайн.
import { chromium, devices } from 'playwright';

const base = process.env.BASE ?? 'http://127.0.0.1:4321';
const out = process.argv[2] ?? 'pwa-offline.png';

const browser = await chromium.launch();
const context = await browser.newContext({ ...devices['iPhone 13'], serviceWorkers: 'allow' });
const page = await context.newPage();

await page.goto(base + '/', { waitUntil: 'networkidle' });

// Ждём регистрации и активации service worker.
await page.waitForFunction(
  async () => {
    if (!('serviceWorker' in navigator)) return false;
    const reg = await navigator.serviceWorker.getRegistration();
    return !!(reg && reg.active);
  },
  { timeout: 40000 },
);

const info = await page.evaluate(async () => {
  const reg = await navigator.serviceWorker.getRegistration();
  const mani = await fetch('/manifest.webmanifest').then((r) => r.json());
  return { swScope: reg?.scope, swActive: !!reg?.active, name: mani.name, icons: mani.icons.length };
});
console.log('SW active:', info.swActive, '| scope:', info.swScope);
console.log('Manifest:', info.name, '| icons:', info.icons);

// Прогреем кэш (навигация по маршрутам), затем уходим в офлайн.
await page.goto(base + '/shopping', { waitUntil: 'networkidle' });
await page.goto(base + '/', { waitUntil: 'networkidle' });

await context.setOffline(true);
await page.reload({ waitUntil: 'domcontentloaded' });
await page.evaluate(() => document.fonts.ready);
await page.waitForTimeout(600);

const offlineOk = await page.evaluate(() => !!document.querySelector('.chat__stream, .plan, ew-chat'));
console.log('Offline render OK:', offlineOk);

await page.screenshot({ path: out });
await context.setOffline(false);
await browser.close();

if (!info.swActive || !offlineOk) process.exit(2);
