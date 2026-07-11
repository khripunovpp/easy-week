import { chromium, devices } from 'playwright';
const b=await chromium.launch(); const p=await (await b.newContext({...devices['iPhone 13']})).newPage();
const errs=[]; p.on('console',m=>{if(m.type()==='error')errs.push(m.text().slice(0,120));});
await p.goto('http://127.0.0.1:4200/chat',{waitUntil:'networkidle'});
await p.fill('.composer__input','3 ужина без свинины');
await p.click('.composer__send');
try { await p.waitForSelector('.plan',{timeout:40000}); console.log('PLAN OK via proxy'); }
catch { console.log('NO PLAN. errors:', errs.join(' | ')); }
await b.close();
