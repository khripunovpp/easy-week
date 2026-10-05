import { Component, computed, input } from '@angular/core';
import { Storage } from '../models/plan.model';

// Режим хранения блюда (GUIDEBOOK «Бейдж режима хранения» / «Карточка хранения»):
// по умолчанию заготовка под заморозку — ❄️ N дн (дни в морозилке); «свежее» (пользователь
// просил не замораживать, storage.freeze=false) — 🌿 N дн (дни в холодильнике) или «сразу».

/** Короткая подпись срока «свежего» блюда: 0 — съесть сразу. */
export function freshLabel(s: Storage): string {
  return s.shelfLifeDays > 0 ? `${s.shelfLifeDays} дн` : 'сразу';
}

/** Подпись режима хранения для карточки рецепта / печати. */
export function storageSub(s: Storage): string {
  if (s.freeze) return `${s.vacuum ? 'Вакуум · ' : ''}Заморозка до ${s.shelfLifeDays} дней`;
  const keep = s.shelfLifeDays > 0 ? `в холодильнике до ${s.shelfLifeDays} дн` : 'съесть сразу';
  return `Свежее, не замораживать · ${keep}`;
}

@Component({
  selector: 'ew-storage-badge',
  template: `
    @if (storage().freeze) {
      <span class="badge-frost" title="Заготовка под заморозку: срок в морозилке">❄️ {{ storage().shelfLifeDays }} дн</span>
    } @else {
      <span class="badge-fresh" [title]="title()">🌿 {{ label() }}</span>
    }
  `,
  styles: `
    :host {
      display: contents;
    }
  `,
})
export class StorageBadge {
  readonly storage = input.required<Storage>();
  readonly label = computed(() => freshLabel(this.storage()));
  readonly title = computed(() => storageSub(this.storage()));
}

// Памятка хранения от модели рецепта — строки «Метка: текст» (ai/prompt.py → _DETAIL_STORAGE).
export interface NoteRow {
  icon: string;
  label: string; // пусто — строка без метки (старые заметки одним абзацем)
  text: string;
}

const NOTE_LABELS: [RegExp, string, string][] = [
  [/^морозилка$/i, '❄️', 'Морозилка'],
  [/^холодильник$/i, '🫙', 'Холодильник'],
  [/^в день подачи$/i, '🌿', 'В день подачи'],
  [/^разогрев$/i, '🔥', 'Разогрев'],
  [/^важно$/i, '⚠️', 'Важно'],
];

/** Разбор памятки: строки с известными метками — с иконкой; остальное — как текст. */
export function parseStorageNote(note: string | undefined | null): NoteRow[] {
  const rows: NoteRow[] = [];
  // Строки — по переводу строки; некоторые модели отдают его экранированным («\\n» в тексте).
  for (const raw of (note ?? '').split(/\r?\n|\\n/)) {
    // Markdown-жирное («**Морозилка:** …») не рендерим — снимаем звёздочки; маркер списка или
    // свою эмодзи перед меткой модели тоже ставят — срезаем всё до первой буквы.
    const plain = raw.replace(/\*\*/g, '').trim();
    const line = plain.replace(/^[^\p{L}]+/u, '');
    if (!line) continue;
    const m = /^([^:]{2,20}):\s*(.+)$/.exec(line);
    const hit = m && NOTE_LABELS.find(([re]) => re.test(m[1].trim()));
    if (m && hit) rows.push({ icon: hit[1], label: hit[2], text: m[2].trim() });
    else rows.push({ icon: '', label: '', text: plain });
  }
  return rows;
}
