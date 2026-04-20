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

    # ---------------------------------------------------------------- #
    # Bank feeds (SISS Data Services — v1.1 feature)                   #
    # ---------------------------------------------------------------- #
    # Defaults are empty / production values; the bank_feeds service
    # raises on use if client_id / secret / subscription_key are unset.
    # Set these via env or .env once SISS onboarding produces a real
    # credential set. See acsiss/09-open-question-answers.md Q2 for why
    # SISS_SANDBOX is a separate flag.
    siss_client_id: str = Field(default="", alias="SISS_CLIENT_ID")
    siss_client_secret: str = Field(default="", alias="SISS_CLIENT_SECRET")
    siss_subscription_key: str = Field(default="", alias="SISS_SUBSCRIPTION_KEY")
    siss_token_url: str = Field(
        default="https://auth.sissdata.com.au/oauth/token",
        alias="SISS_TOKEN_URL",
    )
    siss_api_base: str = Field(
        default="https://api.sissdata.com.au/cdr-au/v1/",
        alias="SISS_API_BASE",
    )
    siss_sandbox: bool = Field(default=False, alias="SISS_SANDBOX")

    # ---------------------------------------------------------------- #
    # ABR lookup (Australian Business Register — v1.1 feature)         #
    # ---------------------------------------------------------------- #
    # The ABR SearchByABN JSON API needs a "GUID" (API key) issued by
    # abr.business.gov.au. Empty by default; the abr service raises on
    # use when unset so Community builds never hit the upstream.
    abr_api_guid: str = Field(default="", alias="ABR_API_GUID")
    abr_api_base: str = Field(
        default="https://abr.business.gov.au/json",
        alias="ABR_API_BASE",
    )


settings = Settings()
