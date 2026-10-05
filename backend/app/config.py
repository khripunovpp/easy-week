from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Cloudflare Workers AI
    cf_account_id: str = ""
    cf_api_token: str = ""
    # 8b-fast: ~2с/запрос — спеки блюд, шаги, мелочи.
    cf_model: str = "@cf/meta/llama-3.1-8b-instruct-fast"
    cf_model_small: str = "@cf/meta/llama-3.2-3b-instruct"
    # mistral-24b (Cloudflare): развёрнутые рецепты + нормализация списка покупок.
    cf_model_menu: str = "@cf/mistralai/mistral-small-3.1-24b-instruct"
    cf_model_judge: str = "@cf/mistralai/mistral-small-3.1-24b-instruct"

    # DeepSeek: генерация плана (блюда + короткие шаги). Быстрый, качественный.
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"

    @property
    def deepseek_configured(self) -> bool:
        return bool(self.deepseek_api_key)

    # Gemini (Google AI Studio) — придумывание рецептов.
    # gemini-flash-latest — алиас на актуальную flash-модель (2.5-flash недоступна новым ключам).
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_model: str = "gemini-flash-latest"

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key)

    # Anthropic (Claude) — придумывание рецептов. Ключ из console.anthropic.com.
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    # haiku-4-5 — самая дешёвая актуальная модель ($1/$5), но вполне толковая.
    anthropic_model: str = "claude-haiku-4-5"

    @property
    def anthropic_configured(self) -> bool:
        return bool(self.anthropic_api_key)

    # Дневные лимиты генерации на Claude (дорогая модель), 0 = без лимита.
    # Задаются через .env: ANTHROPIC_DAILY_PLANS / ANTHROPIC_DAILY_RECIPES.
    # Дневные лимиты Claude: 0 — без лимита (по умолчанию; расход видно в статистике запросов,
    # Профиль → Модели). Включить — ANTHROPIC_DAILY_PLANS / ANTHROPIC_DAILY_RECIPES в .env.
    anthropic_daily_plans: int = 0
    anthropic_daily_recipes: int = 0

    # OpenRouter — один API ко многим моделям (в т.ч. бесплатным «:free»). Пробуем как замену
    # Cloudflare на дешёвых задачах (покупки, извлечение предпочтений). Модель — любая чатовая
    # с OpenRouter; nemotron-3-super:free даёт чистый JSON на русском при выключенном reasoning.
    # NB: respan/span-01-lite:free — «decisions»-модель (скоринг), chat/completions не умеет.
    # Бесплатные модели: ~20 запросов/мин и дневной лимит (50/день без купленных кредитов).
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "nvidia/nemotron-3-super-120b-a12b:free"

    @property
    def openrouter_configured(self) -> bool:
        return bool(self.openrouter_api_key)

    # Озвучка шагов (ai/tts.py, GET /api/tts): OpenRouter /audio/speech, бесплатная Fish Audio
    # (русский, mp3). Кэш аудио — data/tts. Голос ОБЯЗАТЕЛЕН: без voice Fish каждый раз берёт
    # случайного диктора (замер F0: 308/239/118 Гц на трёх прогонах). Из OpenAI-имён Fish через
    # OpenRouter принимает только «alloy» — стабильный мужской голос (~100 Гц). Deepgram Flux —
    # flux-*-en (только английский).
    openrouter_tts_model: str = "fish-audio/s2.1-pro-free:free"
    openrouter_tts_voice: str = "alloy"
    tts_max_chars: int = 1200  # длиннее шага не бывает; защита от злоупотребления
    # Свой дневной лимит генераций озвучки (реальных вызовов модели; кэш не считается), 0 — без
    # лимита. Бережёт общий лимит бесплатных моделей OpenRouter (50/сутки на ВСЕ :free-модели —
    # покупки и предпочтения тоже там). .env: TTS_DAILY_LIMIT.
    tts_daily_limit: int = 20

    # Модель рецептов по умолчанию: deepseek | gemini | cloudflare | anthropic | openrouter
    recipe_model_default: str = "deepseek"

    # База и сеть
    db_path: str = "data/easy_week.db"
    cors_origins: str = "http://localhost:4200,http://127.0.0.1:4200"

    # Доступ по общему паролю (один на все устройства). Только из .env — в репо не коммитим.
    # APP_PASSWORD пустой → авторизация выключена (удобно в деве), на старте — warning в лог.
    app_password: str = ""
    # Ключ подписи сессионной куки. Пусто → выводится из APP_PASSWORD. Пароль подмешивается
    # в ключ всегда, так что смена пароля разлогинивает все устройства.
    app_secret: str = ""
    # Флаг Secure у куки: auto (по https / X-Forwarded-Proto) | true | false.
    auth_cookie_secure: str = "auto"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.app_password)

    @property
    def ai_log_dir(self) -> str:
        # рядом с БД (persist): data/ai-logs (native) или /data/ai-logs (Docker)
        return str(Path(self.db_path).parent / "ai-logs")

    @property
    def cf_configured(self) -> bool:
        return bool(self.cf_account_id and self.cf_api_token)

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
