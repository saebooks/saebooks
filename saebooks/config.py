from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Five-edition model (CHARTER v1.1 §6). Strict superset:
    # community ⊂ offline ⊂ business ⊂ pro ⊂ enterprise. The licence
    # resolver in ``services/licence/`` sets this at boot from the USB
    # Ed25519 licence (offline) or portal JWT (business/pro/enterprise);
    # community is the fall-through when no licence is present.
    edition: Literal[
        "community", "offline", "business", "pro", "enterprise", "developer"
    ] = Field(default="community", alias="SAEBOOKS_EDITION")
    log_level: str = Field(default="INFO", alias="SAEBOOKS_LOG_LEVEL")
    bind_host: str = Field(default="127.0.0.1", alias="SAEBOOKS_BIND_HOST")
    bind_port: int = Field(default=8000, alias="SAEBOOKS_BIND_PORT")
    debug: bool = Field(default=False, alias="SAEBOOKS_DEBUG")

    database_url: str = Field(
        default="postgresql+asyncpg://saebooks:change-me-local-only@db:5432/saebooks",
        alias="DATABASE_URL",
    )

    # ---------------------------------------------------------------- #
    # Multi-tenant — runtime DB role (P0 cross-tenant leak fix)        #
    # ---------------------------------------------------------------- #
    # See migration 0056_split_db_role.py. The schema-owner role
    # (``saebooks``) is a superuser and bypasses RLS, so the API
    # container connects as the non-superuser ``saebooks_app`` for all
    # request-time queries. ``SAEBOOKS_APP_DATABASE_URL`` overrides
    # ``DATABASE_URL`` for the runtime engine; if unset, the engine
    # falls back to ``DATABASE_URL`` (development convenience —
    # production MUST set this).
    #
    # The migration entrypoint deliberately does NOT use this URL so
    # alembic can keep using the owner role for DDL.
    app_database_url: str = Field(default="", alias="SAEBOOKS_APP_DATABASE_URL")
    # Convenience: just the password, lets ops set the URL once and
    # rotate the password without rebuilding the URL string.
    app_db_password: str = Field(default="", alias="SAEBOOKS_APP_DB_PASSWORD")

    # ---------------------------------------------------------------- #
    # SQL tool — sandboxed read-only role (Cat-C admin)                #
    # ---------------------------------------------------------------- #
    # ``saebooks_sql_ro`` is created by migration 0087 with
    # ``pg_read_all_data`` and explicit REVOKEs on dangerous functions
    # (pg_read_server_files, lo_export, etc). The /api/v1/admin/sql
    # endpoint connects as this role for every plain SELECT. Empty
    # default — the migration refuses to run without it.
    sql_ro_password: str = Field(default="", alias="SAEBOOKS_SQL_RO_PASSWORD")

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
    # credential set. ``SISS_SANDBOX`` is kept as a separate flag so
    # the sandbox vs production switch is explicit, never auto-derived.
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

    # Sandbox-specific overrides. When SISS_SANDBOX=true, siss_client()
    # uses siss_sandbox_key (APIM primary key from the sandbox portal) and
    # siss_base_url (the sandbox API host) instead of the production pair.
    # Leave both empty in production deployments.
    siss_sandbox_key: str = Field(default="", alias="SISS_SANDBOX_PRIMARY_KEY")
    siss_base_url: str = Field(
        default="https://sandboxapi.sissdata.com.au/cdr-au/v1/",
        alias="SISS_BASE_URL",
    )

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
    smtp_from: str = Field(default="books@example.com", alias="SMTP_FROM")
    smtp_tls: bool = Field(default=True, alias="SMTP_TLS")
    mail_outbox_dir: str = Field(
        default="/app/mail-outbox", alias="SAEBOOKS_MAIL_OUTBOX_DIR"
    )

    # ---------------------------------------------------------------- #
    # Customer-facing email pipeline (Resend) — KILL SWITCH            #
    # ---------------------------------------------------------------- #
    # Two-key gate (BOTH must be true for a real Resend network call):
    #   1. customer_email_send_enabled = true   (this env var)
    #   2. tenants.outbound_email_enabled = true (per-tenant DB column)
    # Default for both is false; flipping either alone does nothing.
    # When blocked, customer_email writes the email + attachment to the
    # outbox dir and logs to email_send_log with resend_status='blocked'.
    customer_email_send_enabled: bool = Field(
        default=False, alias="SAEBOOKS_EMAIL_SEND_ENABLED"
    )
    resend_api_key: str = Field(default="", alias="RESEND_API_KEY")
    resend_api_url: str = Field(
        default="https://api.resend.com", alias="RESEND_API_URL"
    )
    # Svix-format webhook signing secret from Resend Dashboard → Webhooks.
    # Format: ``whsec_<base64-encoded-bytes>``. Empty = webhook receiver
    # refuses everything (fail closed).
    resend_webhook_secret: str = Field(default="", alias="RESEND_WEBHOOK_SECRET")

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

    # ---------------------------------------------------------------- #
    # JWT auth (B/43)                                                  #
    # ---------------------------------------------------------------- #
    # Secret key used to sign /auth/login JWT tokens (HMAC-SHA256).
    # If unset, a per-process random key is generated at startup — safe
    # for single-process dev/test; production must set this to a stable
    # value so tokens survive restarts.
    secret_key: str = Field(default="", alias="SAEBOOKS_SECRET_KEY")

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
    # Stripe API keys (B/48 — outbound Checkout Session creation).
    # When stripe_secret_key is empty, create_payment_link raises
    # StripeNotConfiguredError so an unconfigured install can't silently
    # produce broken links. The publishable key is returned to the
    # frontend for client-side Stripe.js use (optional but surfaced in
    # the integrations status page).
    stripe_secret_key: str = Field(default="", alias="STRIPE_SECRET_KEY")
    stripe_publishable_key: str = Field(default="", alias="STRIPE_PUBLISHABLE_KEY")

    # ---------------------------------------------------------------- #
    # AI document extraction                                           #
    # ---------------------------------------------------------------- #
    # OpenAI-compatible API endpoint for vision-capable LLM extraction
    # of receipts/invoices. When ``litellm_api_key`` is empty the
    # ai_extraction service raises ``AiExtractionNotConfiguredError`` on
    # use so a misconfigured install can't silently fail. Only reached
    # on Business+ editions (FLAG_AI_EXTRACTION gate). Point
    # ``LITELLM_BASE_URL`` at any OpenAI-compatible endpoint
    # (LiteLLM proxy, vLLM, OpenAI, etc.).
    litellm_api_key: str = Field(default="", alias="LITELLM_API_KEY")
    litellm_base_url: str = Field(
        default="https://api.openai.com/v1",
        alias="LITELLM_BASE_URL",
    )

    # ---------------------------------------------------------------- #
    # ATO SBR (Batch II.5)                                             #
    # ---------------------------------------------------------------- #
    # AUSkey is retired — STP / BAS e-lodgement uses a Machine
    # Credential issued via RAM (Relationship Authorisation Manager)
    # linked to the admin's myGovID, plus a Software Service ID (SSID)
    # from ATO Software Developer onboarding.
    #
    # We default to the External Vendor Test Environment (EVTE) for
    # every ping so a misconfigured install can't accidentally lodge
    # against production. The onboarding UI flips a per-company
    # ``environment`` toggle to production once a real credential is
    # verified; these base URLs stay global.
    ato_sbr_evte_base: str = Field(
        default="https://softwareauthorisations.acc.ato.gov.au",
        alias="ATO_SBR_EVTE_BASE",
    )
    ato_sbr_prod_base: str = Field(
        default="https://softwareauthorisations.ato.gov.au",
        alias="ATO_SBR_PROD_BASE",
    )



    # ---------------------------------------------------------------- #
    # External identity (DiscourseConnect)                             #
    # ---------------------------------------------------------------- #
    # Identity is consolidated on discourse.saebooks.com.au.
    # The DiscourseConnect handshake itself runs in saebooks-web; the
    # API only sees the post-handshake handoff via /api/v1/auth/oauth-handoff.
    #
    # Portal lockdown — app.saebooks.com.au is Richard's internal portal,
    # NOT a public SaaS. Only emails in this CSV allowlist may complete
    # SSO login (handoff returns 403 otherwise). Empty = open (dev).
    # Comma-separated; whitespace and case-insensitive.
    oauth_allowed_emails: str = Field(default="", alias="SAEBOOKS_OAUTH_ALLOWED_EMAILS")

    # Public base URL the API uses when composing user-facing links
    # (magic-link emails, signup confirmations, etc.). Should point at
    # whichever frontend the user lands on — typically app.saebooks.com.au.
    public_base_url: str = Field(
        default="http://localhost:8000", alias="SAEBOOKS_PUBLIC_BASE_URL"
    )

    # ---------------------------------------------------------------- #
    # Vault — saebooks-vault REST integration (Phase 1)                #
    # ---------------------------------------------------------------- #
    # File attachments (receipts, supporting docs) are stored in the
    # closed-source ``saebooks-vault`` service, never in the accounting
    # DB. SAE Books owns only the linkage (vault file_id + entity ref).
    #
    # ``VAULT_URL`` defaults to the in-compose service hostname so a
    # sibling docker-compose setup (with a shared network) Just Works.
    # Production deployments where the vault runs on a different host
    # set this to the LAN/VPN bind for that host.
    #
    # ``VAULT_SHARED_SECRET`` is the bearer token the vault expects in
    # the ``Authorization`` header. Empty by default so an unconfigured
    # install can't silently send unauthenticated requests.
    #
    # ``VAULT_ENABLED`` is the kill-switch. When false, the attachments
    # router returns 503 — the rest of saebooks is unaffected. Lets a
    # community/offline edition opt out cleanly.
    vault_url: str = Field(
        default="http://saebooks-vault-api:18820", alias="VAULT_URL"
    )
    vault_shared_secret: str = Field(default="", alias="VAULT_SHARED_SECRET")
    vault_enabled: bool = Field(default=False, alias="VAULT_ENABLED")
    # Per-call timeout (seconds) for upstream vault HTTP calls. Upload
    # paths get the longer ``vault_upload_timeout``; metadata/list/delete
    # use ``vault_timeout``.
    vault_timeout: float = Field(default=10.0, alias="VAULT_TIMEOUT")
    vault_upload_timeout: float = Field(default=60.0, alias="VAULT_UPLOAD_TIMEOUT")

    # ---------------------------------------------------------------- #
    # Launch promo — first-1000-customers free Pro for 12 months.     #
    # ---------------------------------------------------------------- #
    # LAUNCH_PROMO_ENABLED: master switch. Default false.
    # LAUNCH_PROMO_LIMIT: cap (default 1000). Must match license-server.
    # LICENSE_SERVER_URL: base URL for license.saebooks.com.au.
    #   The signup flow calls /api/v1/license/issue-launch-promo on
    #   success when the promo is active.
    # LICENSE_SERVER_SHARED_SECRET: bearer token for the internal
    #   admin endpoint (not used by issue-launch-promo which is public,
    #   but reserved for future admin calls). Leave empty to disable.
    launch_promo_enabled: bool = Field(
        default=False, alias="LAUNCH_PROMO_ENABLED"
    )
    launch_promo_limit: int = Field(
        default=1000, alias="LAUNCH_PROMO_LIMIT"
    )
    license_server_url: str = Field(
        default="https://license.saebooks.com.au",
        alias="LICENSE_SERVER_URL",
    )
    license_server_timeout: float = Field(
        default=5.0, alias="LICENSE_SERVER_TIMEOUT"
    )

    # ---------------------------------------------------------------- #
    # Multi-jurisdiction reference DB (v0.1.4)                          #
    # ---------------------------------------------------------------- #
    # The reference DB carries jurisdiction master data (rates, codes,
    # form definitions, brackets, calendars). It lives on the SAME
    # Postgres cluster as the company DB but in a separate database so
    # it can be packaged, versioned, and signed independently of any
    # one customers ledger.
    #
    # Two roles are expected:
    #
    #   reference_app  — the role the API uses at request time.
    #                    Connects with default_transaction_read_only=on
    #                    via connect_args. The app NEVER writes to the
    #                    reference DB; rate corrections ship as
    #                    point-release seed updates.
    #
    #   reference_owner — the role the seed loader and alembic_reference
    #                     use. Has DDL + write privileges. Set
    #                     ``REFERENCE_MIGRATION_DATABASE_URL`` for it.
    #
    # If reference_database_url is empty the API still boots and the
    # ReferenceSession factory returns None — code paths that need
    # reference data raise ReferenceNotConfiguredError so the absence
    # is loud, not silent.
    reference_database_url: str = Field(
        default="", alias="REFERENCE_DATABASE_URL"
    )
    reference_migration_database_url: str = Field(
        default="", alias="REFERENCE_MIGRATION_DATABASE_URL"
    )

    @property
    def oauth_allowed_emails_set(self) -> set[str]:
        return {e.strip().lower() for e in self.oauth_allowed_emails.split(",") if e.strip()}

settings = Settings()
