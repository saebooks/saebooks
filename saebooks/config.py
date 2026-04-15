from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    edition: Literal["community", "enterprise"] = Field(
        default="community", alias="SAEBOOKS_EDITION"
    )
    log_level: str = Field(default="INFO", alias="SAEBOOKS_LOG_LEVEL")
    bind_host: str = Field(default="127.0.0.1", alias="SAEBOOKS_BIND_HOST")
    bind_port: int = Field(default=8000, alias="SAEBOOKS_BIND_PORT")

    database_url: str = Field(
        default="postgresql+asyncpg://saebooks:change-me-local-only@db:5432/saebooks",
        alias="DATABASE_URL",
    )

    seed_company_name: str = Field(default="", alias="SEED_COMPANY_NAME")
    seed_company_legal_name: str = Field(default="", alias="SEED_COMPANY_LEGAL_NAME")
    seed_company_trading_name: str = Field(default="", alias="SEED_COMPANY_TRADING_NAME")
    seed_company_abn: str = Field(default="", alias="SEED_COMPANY_ABN")
    seed_company_acn: str = Field(default="", alias="SEED_COMPANY_ACN")
    seed_company_base_currency: str = Field(default="AUD", alias="SEED_COMPANY_BASE_CURRENCY")
    seed_company_fin_year_start_month: int = Field(
        default=7, alias="SEED_COMPANY_FIN_YEAR_START_MONTH"
    )


settings = Settings()
