// Доменные модели «Easy Week».
// Ответ ассистента в чате приходит как JSON этой формы — из него рендерится страница плана.

export type PlanStatus = 'draft' | 'accepted' | 'rejected';

export type IngredientCategory =
  | 'Мясо и птица'
  | 'Рыба'
  | 'Овощи'
  | 'Бакалея'
  | 'Молочное'
  | 'Специи'
  | 'Прочее';

export interface Ingredient {
  name: string;
  qty: number;
  unit: string; // г, кг, шт, мл, ст.л. …
  category: IngredientCategory;
}

export interface Storage {
  vacuum: boolean;
  freeze: boolean;
  shelfLifeDays: number; // срок хранения в заморозке
  note?: string; // как разморозить / разогреть
}

export interface Dish {
  id: string;
  name: string;
  emoji: string;
  servings: number;
  prepMin: number;
  cookMin: number;
  tags: string[]; // напр. «на ужин», «впрок»
  storage: Storage;
  tips: string[];
  steps: string[];
  ingredients: Ingredient[];
  detailProvider?: string; // какая модель сгенерила развёрнутый рецепт (активный вариант)
  activeModel?: string; // ключ активной модели-варианта рецепта
  variantModels?: string[]; // ключи моделей, для которых вариант уже сгенерирован
}

export interface WeekPlan {
  id: string;
  conversationId?: string;
  title: string;
  weekLabel: string; // напр. «14–20 июля»
  status: PlanStatus;
  provider?: string; // модель, составившая план (DeepSeek | Cloudflare)
  dishes: Dish[];
}

export type ChatRole = 'user' | 'assistant';

// Ответ ассистента может быть текстом-уточнением или нести готовый план.
export interface ChatMessage {
  id: string;
  role: ChatRole;
  text?: string;
  plan?: WeekPlan;
  model?: string; // ключ модели, сгенерившей ответ (для оценки 👍/👎)
  serverId?: string; // серверный id сообщения (для оценки); у intro/ошибок нет
}

// Позиция в списке покупок — агрегируется из всех блюд плана.
export interface ShoppingItem {
  name: string;
  qty: number;
  unit: string;
  category: IngredientCategory;
  checked: boolean;
}
