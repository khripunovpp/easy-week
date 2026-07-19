import { tokenMatch } from './ingredient-match';

// Один «владелец» ингредиента: его токены (ingTokens) + класс цвета блюда (.dish-cN).
export interface HlOwner {
  tokens: string[];
  colorClass: string;
}

interface Part {
  text: string;
  word: boolean;
  norm: string;
}

function esc(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Разбить текст на слова и разделители (с сохранением исходных подстрок).
function split(text: string): Part[] {
  const parts: Part[] = [];
  const re = /(\p{L}+|\p{N}+)/gu;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parts.push({ text: text.slice(last, m.index), word: false, norm: '' });
    parts.push({ text: m[0], word: true, norm: m[0].toLowerCase() });
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push({ text: text.slice(last), word: false, norm: '' });
  return parts;
}

// Матч фразы (owner.tokens) начиная со слова parts[pi]; возвращает индекс последнего слова + цвет.
function matchAt(
  parts: Part[],
  pi: number,
  phrases: HlOwner[],
): { end: number; colorClass: string } | null {
  for (const ph of phrases) {
    let k = 0;
    let j = pi;
    let lastWord = -1;
    let ok = true;
    while (k < ph.tokens.length) {
      while (j < parts.length && !parts[j].word) j++;
      if (j >= parts.length || !tokenMatch(parts[j].norm, ph.tokens[k])) {
        ok = false;
        break;
      }
      lastWord = j;
      j++;
      k++;
    }
    if (ok) return { end: lastWord, colorClass: ph.colorClass };
  }
  return null;
}

// Подсветить в тексте шага упоминания ингредиентов цветом блюда-владельца.
// Возвращает безопасный HTML (текст экранирован, вставляются только наши <span>).
export function highlightStepText(text: string, owners: HlOwner[]): string {
  const t = text || '';
  const phrases = owners.filter((o) => o.tokens.length).sort((a, b) => b.tokens.length - a.tokens.length);
  if (!phrases.length) return esc(t);

  const parts = split(t);
  let out = '';
  let pi = 0;
  while (pi < parts.length) {
    if (!parts[pi].word) {
      out += esc(parts[pi].text);
      pi++;
      continue;
    }
    const hit = matchAt(parts, pi, phrases);
    if (hit) {
      const raw = parts.slice(pi, hit.end + 1).map((p) => p.text).join('');
      out += `<span class="dish-hl ${hit.colorClass}">${esc(raw)}</span>`;
      pi = hit.end + 1;
    } else {
      out += esc(parts[pi].text);
      pi++;
    }
  }
  return out;
}
