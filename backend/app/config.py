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

    # База и сеть
    db_path: str = "data/easy_week.db"
    cors_origins: str = "http://localhost:4200,http://127.0.0.1:4200"

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
