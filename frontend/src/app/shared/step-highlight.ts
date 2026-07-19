import { tokenMatch } from './ingredient-match';

// Один «владелец» ингредиента: токены (ingTokens), id блюда и класс его цвета (.dish-cN).
export interface HlOwner {
  tokens: string[];
  dishId: string;
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

// Класс подсветки по владельцам ингредиента и активному фильтру блюд:
// — фильтр пуст: один владелец → цвет блюда, несколько → нейтральный «общий»;
// — фильтр активен: среди выбранных владельцев один → его цвет; несколько → «общий»;
//   ни одного выбранного → ингредиент чужой в этом шаге → затухание (--dim, цвет сохраняем).
function classFor(owners: Map<string, string>, selected: ReadonlySet<string>): string {
  const ids = [...owners.keys()];
  const base = (list: string[]) => (list.length === 1 ? owners.get(list[0])! : 'dish-hl--shared');
  if (!selected.size) return base(ids);
  const sel = ids.filter((id) => selected.has(id));
  if (sel.length) return base(sel);
  return base(ids) + ' dish-hl--dim';
}

// Матч ингредиента начиная со слова parts[pi]. Берём самое длинное совпадение (фраза важнее
// одиночного токена) и собираем всех блюд-владельцев на этой позиции.
function matchAt(
  parts: Part[],
  pi: number,
  phrases: HlOwner[],
  selected: ReadonlySet<string>,
): { end: number; cls: string } | null {
  const ownersByEnd = new Map<number, Map<string, string>>();
  let best = -1;
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
    if (!ok) continue;
    if (lastWord > best) best = lastWord;
    let owners = ownersByEnd.get(lastWord);
    if (!owners) ownersByEnd.set(lastWord, (owners = new Map()));
    owners.set(ph.dishId, ph.colorClass);
  }
  if (best < 0) return null;
  return { end: best, cls: classFor(ownersByEnd.get(best)!, selected) };
}

// Подсветить в тексте шага упоминания ингредиентов цветом блюда-владельца (с учётом фильтра).
// Возвращает безопасный HTML (текст экранирован, вставляются только наши <span>).
export function highlightStepText(
  text: string,
  owners: HlOwner[],
  selected: ReadonlySet<string> = new Set(),
): string {
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
    const hit = matchAt(parts, pi, phrases, selected);
    if (hit) {
      const raw = parts.slice(pi, hit.end + 1).map((p) => p.text).join('');
      out += `<span class="dish-hl ${hit.cls}">${esc(raw)}</span>`;
      pi = hit.end + 1;
    } else {
      out += esc(parts[pi].text);
      pi++;
    }
  }
  return out;
}
