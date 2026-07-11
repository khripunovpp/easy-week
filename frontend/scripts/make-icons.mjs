// Генерация брендовых PWA-иконок (морковка на тёплом фоне) в public/icons.
import { chromium } from 'playwright';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const dir = join(dirname(fileURLToPath(import.meta.url)), '..', 'public', 'icons');

const carrot = `
<svg viewBox="0 0 100 100" width="62" height="62" xmlns="http://www.w3.org/2000/svg">
  <!-- листья -->
  <g fill="#57b65f">
    <path d="M50 44 C48 30 41 24 33 25 C40 29 44 36 46 45 Z"/>
    <path d="M50 44 C50 28 54 21 62 20 C58 27 56 35 54 45 Z"/>
    <path d="M50 45 C54 33 61 29 69 31 C62 33 58 39 55 46 Z"/>
  </g>
  <!-- тело -->
  <path d="M50 92 C42 74 37 60 40 50 C42 43 58 43 60 50 C63 60 58 74 50 92 Z" fill="#ee7a3d"/>
  <g stroke="#d9622a" stroke-width="1.6" stroke-linecap="round" opacity="0.7">
    <path d="M47 60 l3 2"/>
    <path d="M52 68 l3 2"/>
    <path d="M46 74 l3 2"/>
  </g>
</svg>`;

function html(size, pad) {
  const scale = pad ? 0.72 : 0.92;
  return `<!doctype html><html><head><meta charset="utf-8">
  <style>html,body{margin:0}</style></head>
  <body>
    <div style="width:${size}px;height:${size}px;display:grid;place-items:center;
      background:linear-gradient(150deg,#fdeadf 0%,#f7ccb8 100%);">
      <div style="transform:scale(${(size / 100) * scale})">${carrot}</div>
    </div>
  </body></html>`;
}

const targets = [
  { name: 'icon-192x192.png', size: 192, pad: false },
  { name: 'icon-512x512.png', size: 512, pad: false },
  { name: 'icon-maskable-512x512.png', size: 512, pad: true },
];

const browser = await chromium.launch();
for (const t of targets) {
  const page = await browser.newPage({ viewport: { width: t.size, height: t.size } });
  await page.setContent(html(t.size, t.pad));
  await page.waitForTimeout(120);
  await page.screenshot({ path: join(dir, t.name) });
  await page.close();
  console.log('wrote', t.name);
}
await browser.close();
