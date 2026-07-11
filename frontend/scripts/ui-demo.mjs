import { chromium, devices } from 'playwright';
const OUT=process.argv[2], base='http://127.0.0.1:4200';
const b=await chromium.launch(); const ctx=await b.newContext({...devices['iPhone 13']}); const p=await ctx.newPage();

// 1) Главная
await p.goto(base+'/home',{waitUntil:'networkidle'});
await p.evaluate(()=>document.fonts.ready); await p.waitForTimeout(300);
await p.screenshot({path:`${OUT}/ui-home.png`}); console.log('home ok');

// 2) Чат + выпадашка количества
await p.goto(base+'/chat',{waitUntil:'networkidle'});
await p.click('.count__btn'); await p.waitForSelector('.count__menu'); await p.waitForTimeout(200);
await p.screenshot({path:`${OUT}/ui-count.png`}); console.log('count ok');
// выберем 6
await p.click('.count__opt:has-text("6 блюд")');

// 3) Генерация + переход в рецепт + назад (проверка сохранения чата)
await p.fill('.composer__input','азиатское');
await p.click('.composer__send');
await p.waitForSelector('.plan',{timeout:45000});
await p.click('.dish__main');
await p.waitForSelector('.d__hero, .spinner',{timeout:10000});
await p.waitForSelector('.d__hero',{timeout:45000});
await p.click('.d__back');
await p.waitForSelector('.plan',{timeout:8000}); // если план на месте — стор сработал
await p.evaluate(()=>{const s=document.querySelector('.chat__stream'); if(s)s.scrollTop=0;});
await p.waitForTimeout(300);
await p.screenshot({path:`${OUT}/ui-back.png`});
const planStill = await p.$('.plan');
console.log('back-nav preserved chat:', !!planStill);
await b.close();
