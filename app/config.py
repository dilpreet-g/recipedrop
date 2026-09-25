from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    mealie_url: str = ""
    mealie_token: str = ""
    gemini_api_key: str = ""
    media_max_bytes: int = Field(default=52_428_800, gt=0)
    media_max_duration_seconds: int = Field(default=300, gt=0)


settings = Settings()
