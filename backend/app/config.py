from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Cloudflare Workers AI
    cf_account_id: str = ""
    cf_api_token: str = ""
    # 8b-fast: ~2с/запрос — спеки блюд, шаги, мелочи.
    cf_model: str = "@cf/meta/llama-3.1-8b-instruct-fast"
    cf_model_small: str = "@cf/meta/llama-3.2-3b-instruct"
    # mistral-24b: ~6с, реалистичное меню без «небылиц» + строгий валидатор.
    cf_model_menu: str = "@cf/mistralai/mistral-small-3.1-24b-instruct"
    cf_model_judge: str = "@cf/mistralai/mistral-small-3.1-24b-instruct"

    # База и сеть
    db_path: str = "data/easy_week.db"
    cors_origins: str = "http://localhost:4200,http://127.0.0.1:4200"

    @property
    def cf_configured(self) -> bool:
        return bool(self.cf_account_id and self.cf_api_token)

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
