// Ключ продукта для отметок «куплено» — один на продукт, общий для режимов «Общий» и
// «По рецептам». Повторяет _canon_name из backend/app/services/shopping.py: нижний регистр,
// ё→е, без пунктуации и слов-шумов, лёгкий стемминг ед./мн. числа, слова по алфавиту.
// Так «Перец чёрный», «чёрный перец», «Чёрный перец молотый» → один ключ.

const NOISE = new Set(['молотый', 'молотая', 'свежемолотый', 'свежий', 'свежая', 'сушёный', 'сушеный']);

export function productKey(name: string): string {
  const s = name.toLowerCase().replace(/ё/g, 'е').replace(/[^а-я0-9 ]/g, ' ');
  const toks: string[] = [];
  for (let t of s.split(/\s+/)) {
    if (!t || NOISE.has(t)) continue;
    if (t.length >= 4 && (t.endsWith('ы') || t.endsWith('и'))) t = t.slice(0, -1);
    toks.push(t);
  }
  return toks.sort().join(' ');
}

/** Один продукт в разных формулировках: ключи равны или слова одного входят в другой
 *  («лук» ↔ «лук репчатый»). Нужен, т.к. общий список чистит модель, а «по рецептам» —
 *  исходные названия ингредиентов. */
export function sameProduct(a: string, b: string): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  const ta = a.split(' ');
  const tb = new Set(b.split(' '));
  const [small, big] = ta.length <= tb.size ? [ta, tb] : [b.split(' '), new Set(ta)];
  return small.every((t) => big.has(t));
}
