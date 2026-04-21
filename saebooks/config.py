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
    # Field-level encryption (Batch II)                                #
    # ---------------------------------------------------------------- #
    # Fernet key used by ``saebooks.services.crypto`` to encrypt secret
    # fields at rest (first user: per-company SISS credentials). Empty
    # means "encryption disabled" — callers that try to encrypt/decrypt
    # raise ``FieldEncryptionNotConfiguredError`` so we never silently
    # persist a plaintext secret into a column the schema promised was
    # ciphertext. Generate with ``cryptography.fernet.Fernet.generate_key()``
    # and store as URL-safe base64 in the env.
    field_encryption_key: str = Field(default="", alias="SAEBOOKS_FIELD_ENCRYPTION_KEY")

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

    # ---------------------------------------------------------------- #
    # Outbound email (Batch Q)                                         #
    # ---------------------------------------------------------------- #
    # When SMTP_HOST is empty, ``saebooks.services.mailer`` drops
    # messages into ``mail_outbox_dir`` (default /app/mail-outbox) as
    # .eml files — same pattern as Mailpit for local dev. Production
    # sets SMTP_HOST + creds.
    smtp_host: str = Field(default="", alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_user: str = Field(default="", alias="SMTP_USER")
    smtp_password: str = Field(default="", alias="SMTP_PASSWORD")
    smtp_from: str = Field(default="books@sauer.com.au", alias="SMTP_FROM")
    smtp_tls: bool = Field(default=True, alias="SMTP_TLS")
    mail_outbox_dir: str = Field(
        default="/app/mail-outbox", alias="SAEBOOKS_MAIL_OUTBOX_DIR"
    )

    # ---------------------------------------------------------------- #
    # Observability (Batch Z)                                          #
    # ---------------------------------------------------------------- #
    sentry_dsn: str = Field(default="", alias="SENTRY_DSN")
    log_json: bool = Field(default=False, alias="SAEBOOKS_LOG_JSON")

    # ---------------------------------------------------------------- #
    # Integrations (Batch DD)                                          #
    # ---------------------------------------------------------------- #
    # Paperless — document-store integration. PAPERLESS_URL is the
    # browser-facing base URL (used to build preview links on the
    # attachment UI); PAPERLESS_API_URL is what the server itself
    # calls (may be the same, may be an internal hostname like
    # http://paperless:8000 behind Caddy). If PAPERLESS_API_TOKEN is
    # empty the module raises PaperlessNotConfiguredError on use.
    paperless_url: str = Field(default="", alias="PAPERLESS_URL")
    paperless_api_url: str = Field(default="", alias="PAPERLESS_API_URL")
    paperless_api_token: str = Field(default="", alias="PAPERLESS_API_TOKEN")

    # LEI / GLEIF — same shape as ABR. Enterprise-only feature gate
    # (see FLAG_LEI_LOOKUP). No API key needed — GLEIF is public.
    lei_api_base: str = Field(
        default="https://api.gleif.org/api/v1",
        alias="LEI_API_BASE",
    )

    # Companies House (UK) — Enterprise-only (see FLAG_COMPANIES_HOUSE).
    # Needs a free API key from https://developer.company-information.service.gov.uk/.
    # Key is sent as HTTP Basic-auth username with an empty password —
    # quirk of the CH API. When CH_API_KEY is empty the module raises
    # ``CompaniesHouseNotConfiguredError`` on use.
    ch_api_key: str = Field(default="", alias="CH_API_KEY")
    ch_api_base: str = Field(
        default="https://api.company-information.service.gov.uk",
        alias="CH_API_BASE",
    )

    # ---------------------------------------------------------------- #
    # Frontend theme (Batch QQ)                                        #
    # ---------------------------------------------------------------- #
    # Which Jinja theme layer is active. ``default`` is the stock flat
    # ``saebooks/templates/`` tree; ``classic`` loads the MYOB Classic
    # (AccountRight-style) overrides under ``templates/themes/classic/``.
    # Validated by ``services.theme.validate_startup_theme`` at app boot
    # so a typo fails loudly rather than silently falling back.
    # Per-company override is persisted as a Setting row (key ``theme``)
    # from /admin/theme; per-user override is the ``preferred_theme``
    # column on users.
    # Empty string means "unset" — the resolver treats it as a fall-through
    # so a per-company DB setting can win over the env when env is absent.
    # ``validate_startup_theme`` coerces "" back to ``DEFAULT_THEME`` at boot.
    frontend: str = Field(default="", alias="SAEBOOKS_FRONTEND")

    # Stripe webhook — public /webhooks/stripe endpoint. When
    # STRIPE_WEBHOOK_SECRET is empty the webhook handler returns 503
    # so an unconfigured instance doesn't silently accept forged
    # events. STRIPE_DEFAULT_BANK_ACCOUNT_ID pins the ledger account
    # that incoming Payment rows are created against on
    # payment_intent.succeeded (optional — skipped if empty).
    stripe_webhook_secret: str = Field(default="", alias="STRIPE_WEBHOOK_SECRET")
    stripe_default_bank_account_id: str = Field(
        default="", alias="STRIPE_DEFAULT_BANK_ACCOUNT_ID"
    )


settings = Settings()
