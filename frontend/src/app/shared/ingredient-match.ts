// Общие утилиты фаззи-сопоставления ингредиентов (используются в сравнении рецептов
// и в подсветке ингредиентов в шагах готовки).

const STOP = new Set(['и', 'из', 'с', 'со', 'в', 'во', 'на', 'для', 'до', 'по']);

// Значимые токены имени: нижний регистр, без скобок-уточнений, пунктуации, коротких/стоп-слов.
export function ingTokens(name: string): string[] {
  return (name || '')
    .toLowerCase()
    .replace(/\([^)]*\)/g, ' ')
    .replace(/[^\p{L}\p{N}\s]/gu, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .split(' ')
    .filter((t) => t.length > 1 && !STOP.has(t));
}

// Символьная близость строк (Левенштейн → доля совпадения 0..1) — ловит опечатки/окончания.
export function levRatio(a: string, b: string): number {
  const m = a.length;
  const n = b.length;
  if (!m || !n) return m === n ? 1 : 0;
  const d = Array.from({ length: n + 1 }, (_, i) => i);
  for (let i = 1; i <= m; i++) {
    let prev = d[0];
    d[0] = i;
    for (let j = 1; j <= n; j++) {
      const tmp = d[j];
      d[j] = Math.min(d[j] + 1, d[j - 1] + 1, prev + (a[i - 1] === b[j - 1] ? 0 : 1));
      prev = tmp;
    }
  }
  return 1 - d[n] / Math.max(m, n);
}

// Совпадение токена (точное или фаззи ≥0.8 — «помидоры»≈«помидор», но не «говяжий»/«свиной»).
export function tokenMatch(a: string, b: string): boolean {
  return a === b || (a.length > 2 && b.length > 2 && levRatio(a, b) >= 0.8);
}
