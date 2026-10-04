import { MODEL_LABELS, RecipeModel } from '../services/preferences';

// Текст ошибки AI-запроса для экрана. 502 бэка несёт сырой ответ провайдера («Gemini 503: {…json…}»
// или обрыв JSON) — показывать его нельзя, поэтому по коду ответа: перегрузка провайдера,
// модель не ответила, нет связи. 429 (дневной лимит) и прочие короткие тексты бэка — как есть.
// failed — что не получилось: «рецепт не собран», «новая версия не собрана — текущая на месте».
const OVERLOAD = /\b(503|529|429|overload|high demand|unavailable|resource_exhausted|rate limit)/i;

export function aiFailText(err: unknown, model: string, failed: string): string {
  const e = err as { status?: number; error?: { detail?: unknown } } | null;
  const status = e?.status ?? 0;
  const detail = typeof e?.error?.detail === 'string' ? e.error.detail : '';
  const label = MODEL_LABELS[model.split(':')[0] as RecipeModel] ?? 'Модель';
  if (status === 0) return `Нет связи с сервером — ${failed}.`;
  if (status === 429 && detail) return detail;
  if (status === 502) {
    return OVERLOAD.test(detail)
      ? `${label} сейчас перегружена — ${failed}. Повторите позже или выберите другую модель.`
      : `${label} не ответила — ${failed}. Повторите или выберите другую модель.`;
  }
  if (detail && detail.length <= 160 && !detail.includes('{')) return detail;
  return `Не получилось — ${failed}.`;
}
