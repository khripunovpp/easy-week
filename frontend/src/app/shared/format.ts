// Минуты → человекочитаемая длительность: «5 ч 40 мин» / «5 ч» / «40 мин».
export function formatDuration(mins: number): string {
  const h = Math.floor(mins / 60);
  const m = mins % 60;
  if (h && m) return `${h} ч ${m} мин`;
  if (h) return `${h} ч`;
  return `${m} мин`;
}
