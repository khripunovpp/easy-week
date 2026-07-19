// Класс цвета блюда по его индексу в плане (палитра из 10 цветов, см. styles.scss .dish-c*).
// Один цвет на блюдо везде: эмодзи-бейджи, чипы и подсветка в готовке.
export function dishColorClass(index: number): string {
  return 'dish-c' + (((index % 10) + 10) % 10);
}
