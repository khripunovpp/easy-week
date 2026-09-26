// Минуты → человекочитаемая длительность: «5 ч 40 мин» / «5 ч» / «40 мин».
export function formatDuration(mins: number): string {
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  if (h && m) return `${h} ч ${m} мин`;
  if (h) return `${h} ч`;
  return `${m} мин`;
}

// Дата генерации (ISO) → короткая подпись в локальном времени: «сегодня, 14:05» /
// «вчера, 09:30» / «26 сен, 14:05» (год — только если не текущий). Пусто/битая дата → ''.
export function formatGeneratedAt(iso: string | null | undefined): string {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  const time = d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  const now = new Date();
  const day = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diffDays = Math.round((day(now) - day(d)) / 86_400_000);
  if (diffDays === 0) return `сегодня, ${time}`;
  if (diffDays === 1) return `вчера, ${time}`;
  const date = d
    .toLocaleDateString('ru-RU', {
      day: 'numeric',
      month: 'short',
      ...(d.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}),
    })
    .replace('.', '');
  return `${date}, ${time}`;
}
