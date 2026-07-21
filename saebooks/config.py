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
    # The seed company's jurisdiction. Defaults to AU (Richard's home
    # books) — the CORE model default is the neutral "XX" sentinel, so the
    # seed company must state its jurisdiction explicitly rather than
    # inheriting a national default from the engine.
    seed_company_jurisdiction: str = Field(default="AU", alias="SEED_COMPANY_JURISDICTION")
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
    # Intercompany REMOTE relay (Phase 3c) — DEFAULT-OFF safety flag    #
    # ---------------------------------------------------------------- #
    # The single most important reversibility lever for the cross-DB
    # intercompany relay (plan D4). When False (the default and the only
    # value until Richard signs off a per-stack go-live):
    #   * the REMOTE branch of services/intercompany.post_remote_originator
    #     raises a clear "remote relay disabled" error (no outbox row,
    #     no local leg) -- REMOTE edges are inert;
    #   * the outbox dispatcher task does NOT start in lifespan;
    #   * the receiver POST /api/v1/intercompany/accept returns 503.
    # Nothing relays a cross-tenant write until this is flipped True on a
    # named stack. LOCAL (same-tenant) intercompany is unaffected either way.
    ic_remote_relay_enabled: bool = Field(
        default=False, alias="SAEBOOKS_IC_REMOTE_RELAY_ENABLED"
    )
    # The ONE outbound URL each tenant api learns: the saebooks-group broker
    # relay service (e.g. http://saebooks-group-relay:8000 on a shared docker
    # network). A tenant never learns partner /ic/accept URLs directly -- the
    # broker holds the pair registry and forwards. Empty until 3c go-live.
    ic_broker_url: str = Field(default="", alias="SAEBOOKS_IC_BROKER_URL")
    # Freshness window (seconds) for an inbound relay message: a message whose
    # issued_at is older than this (or as far in the future) is rejected at
    # /ic/accept AND at the broker /ic/relay BEFORE the nonce dedupe, bounding
    # the replay surface. Default 10 min (600s) — tight on purpose: the relay is
    # a same-LAN hop between cooperating stacks, so a legitimate message is
    # seconds old; a wide 24h window left a day-long window in which a captured
    # message could be re-injected before its nonce was first seen. Configurable
    # for clock-skew-heavy deployments (5-15 min is the sane band).
    ic_relay_freshness_seconds: int = Field(
        default=600, alias="SAEBOOKS_IC_RELAY_FRESHNESS_SECONDS"
    )
    # Dispatcher backoff / give-up policy. After max_attempts the outbox row
    # goes DEAD and surfaces in the recon view for human action -- it is NEVER
    # auto-reversed (plan D5).
    ic_relay_max_attempts: int = Field(
        default=8, alias="SAEBOOKS_IC_RELAY_MAX_ATTEMPTS"
    )
    ic_relay_poll_seconds: float = Field(
        default=5.0, alias="SAEBOOKS_IC_RELAY_POLL_SECONDS"
    )

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
    # Comms module (email transport + policy) — #32 extraction         #
    # ---------------------------------------------------------------- #
    # Outbound email transport (SMTP/Resend), the customer-email
    # two-key kill switch (SAEBOOKS_EMAIL_SEND_ENABLED /
    # SAEBOOKS_EMAIL_DRAFT_MODE), the FROM allowlist, the draft-mode
    # Outlook/Graph handoff, and the Jinja email templates ALL moved out
    # of the engine into the app "comms" module (saebooks-web) — same
    # pattern as the #31 render service. The engine now POSTs a message to
    # ``{comms_service_url}/internal/comms/send`` and the module decides
    # sent/drafted/blocked + performs the transport. Those SMTP_* / GRAPH_*
    # / RESEND_API_* / SAEBOOKS_EMAIL_* env vars now belong on the MODULE
    # container; any left on the engine deploy are harmless (extra="ignore").
    #
    # Defaults to the in-compose ``web`` service hostname so a sibling
    # docker-compose on a shared network Just Works. The optional
    # ``comms_service_token`` is sent as the ``X-Comms-Token`` header when
    # non-empty (mirrors RENDER_SERVICE_TOKEN); when empty no header is
    # sent (the module accepts unauthenticated calls on a private network).
    comms_service_url: str = Field(
        default="http://web:8080", alias="COMMS_SERVICE_URL"
    )
    comms_service_token: str = Field(default="", alias="COMMS_SERVICE_TOKEN")

    # Local audit outbox — send_customer_email still writes a copy of every
    # customer-facing message as an ``.eml`` here (engine-side audit
    # artifact alongside the email_send_log row). Not a transport path.
    mail_outbox_dir: str = Field(
        default="/app/mail-outbox", alias="SAEBOOKS_MAIL_OUTBOX_DIR"
    )

    # Svix-format webhook signing secret from Resend Dashboard → Webhooks.
    # Format: ``whsec_<base64-encoded-bytes>``. Empty = webhook receiver
    # refuses everything (fail closed). STAYS engine-side: the inbound
    # delivery-event webhook (webhooks_resend.py) updates the engine-owned
    # email_send_log audit rows, independent of the comms module.
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
    # Auto-bill kill switch. When False (DEFAULT) the inbound Paperless
    # webhook will NOT create DRAFT supplier bills for _BILL_TYPES
    # documents — it logs and returns 200 (same as an unrouted doctype).
    # Disabled by default because unattended auto-draft bills (one per
    # Paperless doc, incl. own outbound invoices/statements) created
    # “AUTO-INGESTED FROM PAPERLESS” junk that had to be purged from
    # primary_books (DB-rebuild handover Gap 4). Email→Paperless archiving
    # is unaffected (separate IMAP path). Statement reconciliation also
    # unaffected (routes via _STATEMENT_TYPES, not this gate). Set
    # PAPERLESS_AUTO_BILL_ENABLED=true to deliberately re-enable.
    paperless_auto_bill_enabled: bool = Field(
        default=False, alias="PAPERLESS_AUTO_BILL_ENABLED"
    )

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
    # Document Inbox email-in (issue #33 phase 3)                      #
    # ---------------------------------------------------------------- #
    # Per-tenant ingestion addresses <token>@<SAEBOOKS_INBOX_MAIL_DOMAIN>
    # drained from ONE catch-all mailbox by the ``inbox-poll-mail`` CLI
    # (a cron-kicked poller — never an inbound SMTP daemon). Everything
    # is config-driven so standing up the live mailbox is pure env
    # wiring: pick a provider, point the credentials at the catch-all,
    # set the domain. Empty provider = email-in disabled (the CLI exits
    # with a clear message; the API address endpoints still work so
    # addresses can be minted ahead of the mailbox).
    #
    # SAEBOOKS_INBOX_MAIL_PROVIDER: "" (disabled) | "imap" | "graph".
    inbox_mail_provider: str = Field(
        default="", alias="SAEBOOKS_INBOX_MAIL_PROVIDER"
    )
    # The dedicated ingestion subdomain (e.g. in.saebooks.com.au).
    # Recipient addresses outside this domain are ignored when routing;
    # empty = accept any recipient domain (dev/tests only).
    inbox_mail_domain: str = Field(
        default="", alias="SAEBOOKS_INBOX_MAIL_DOMAIN"
    )
    # Mailbox folders the poller files into (created if missing).
    inbox_mail_processed_folder: str = Field(
        default="Processed", alias="SAEBOOKS_INBOX_MAIL_PROCESSED_FOLDER"
    )
    inbox_mail_quarantine_folder: str = Field(
        default="Quarantine", alias="SAEBOOKS_INBOX_MAIL_QUARANTINE_FOLDER"
    )
    # Per-tenant abuse quota: EMAIL-sourced documents per UTC day;
    # excess messages are quarantined, never bounced.
    inbox_email_daily_quota: int = Field(
        default=200, alias="SAEBOOKS_INBOX_EMAIL_DAILY_QUOTA"
    )
    # --- IMAP adapter (self-hosted operators bring their own IMAP) --- #
    inbox_imap_host: str = Field(default="", alias="SAEBOOKS_INBOX_IMAP_HOST")
    inbox_imap_port: int = Field(default=993, alias="SAEBOOKS_INBOX_IMAP_PORT")
    inbox_imap_username: str = Field(
        default="", alias="SAEBOOKS_INBOX_IMAP_USERNAME"
    )
    inbox_imap_password: str = Field(
        default="", alias="SAEBOOKS_INBOX_IMAP_PASSWORD"
    )
    inbox_imap_use_ssl: bool = Field(
        default=True, alias="SAEBOOKS_INBOX_IMAP_USE_SSL"
    )
    inbox_imap_folder: str = Field(
        default="INBOX", alias="SAEBOOKS_INBOX_IMAP_FOLDER"
    )
    # --- Microsoft Graph adapter (client-credentials app-only flow) -- #
    # App registration: Mail.ReadWrite (application) on the catch-all
    # mailbox, admin-consented + scoped by an application access policy.
    inbox_graph_tenant_id: str = Field(
        default="", alias="SAEBOOKS_INBOX_GRAPH_TENANT_ID"
    )
    inbox_graph_client_id: str = Field(
        default="", alias="SAEBOOKS_INBOX_GRAPH_CLIENT_ID"
    )
    inbox_graph_client_secret: str = Field(
        default="", alias="SAEBOOKS_INBOX_GRAPH_CLIENT_SECRET"
    )
    # UPN / id of the catch-all mailbox the app drains.
    inbox_graph_mailbox: str = Field(
        default="", alias="SAEBOOKS_INBOX_GRAPH_MAILBOX"
    )
    inbox_graph_base_url: str = Field(
        default="https://graph.microsoft.com/v1.0",
        alias="SAEBOOKS_INBOX_GRAPH_BASE_URL",
    )

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

    # ---------------------------------------------------------------- #
    # PDF render service (presentation lives in the app now — #31/#32) #
    # ---------------------------------------------------------------- #
    # The LaTeX templates, latex_escape filter and XeLaTeX client moved
    # out of the engine into the app render service. The engine now POSTs
    # a document's fact context to RENDER_SERVICE_URL and streams back the
    # compiled PDF. Defaults to the in-compose ``web`` service hostname so
    # a sibling docker-compose on a shared network Just Works; the app
    # exposes POST /internal/render/{template}.
    render_service_url: str = Field(
        default="http://web:8080", alias="RENDER_SERVICE_URL"
    )
    # Optional shared secret for the internal render endpoint. When set,
    # render_latex sends it as the ``X-Render-Token`` header; when empty,
    # no header is sent (the app render service accepts unauthenticated
    # calls on a private network).
    render_service_token: str = Field(
        default="", alias="RENDER_SERVICE_TOKEN"
    )

    # ---------------------------------------------------------------- #
    # Internal module-to-engine API token (#32 pre-accounting)         #
    # ---------------------------------------------------------------- #
    # Shared secret gating the ``/internal/*`` module hand-off endpoints
    # that a sibling module container calls over the private docker
    # network (first user: POST /internal/numbering/next). Presented as
    # the ``X-Internal-Token`` header and compared constant-time.
    #
    # UNLIKE the dev-open demo guard, an EMPTY value here HARD-DISABLES the
    # endpoint (503) in every environment — these endpoints have real side
    # effects (numbering burns a sequence value), so an unconfigured
    # instance must fail closed rather than run ungated.
    internal_api_token: str = Field(default="", alias="INTERNAL_API_TOKEN")

    # ---------------------------------------------------------------- #
    # Pre-accounting module facade (#32 step 4)                        #
    # ---------------------------------------------------------------- #
    # The pre-accounting surface (quotes / purchase-orders / time-entries
    # CRUD + the three conversion hand-offs) can be hosted either
    # in-process in the engine OR in a sibling ``preaccounting`` container
    # running the SAME service code with this flag UNSET. The split is the
    # render/comms facade pattern applied to a stateful module: both
    # containers share ONE Postgres database (schema ``preaccounting``,
    # moved in step 3), so delegation is a *process* boundary, not a data
    # boundary — every read/write hits the same rows under the same RLS.
    #
    # ``preaccounting_base_url`` — when NON-EMPTY, the engine's public
    #   quotes/purchase_orders/time_entries service functions delegate
    #   their work to the module over HTTP instead of running in-process.
    #   Default "" = in-process (zero behaviour change). Flipping the flag
    #   is fully reversible: unset it and the in-process path returns.
    #   The MODULE container MUST leave this empty (else it would recurse
    #   into itself) — it is the in-process executor.
    preaccounting_base_url: str = Field(
        default="", alias="PREACCOUNTING_BASE_URL"
    )
    # ``preaccounting_service_token`` — the shared secret the ENGINE sends
    #   as the ``X-PreAccounting-Token`` header when delegating. It is
    #   compared (constant-time) by the module against its own
    #   ``PREACCOUNTING_TOKEN``; the two must match across the deploy.
    preaccounting_service_token: str = Field(
        default="", alias="PREACCOUNTING_SERVICE_TOKEN"
    )
    # ``preaccounting_token`` — the MODULE's inbound gate. Presented as
    #   ``X-PreAccounting-Token`` and compared constant-time. EMPTY
    #   HARD-DISABLES the module surface (503 fail-closed) in every
    #   environment: the module fronts tenant data, so an unconfigured
    #   instance must refuse rather than serve ungated.
    preaccounting_token: str = Field(default="", alias="PREACCOUNTING_TOKEN")

    # ---------------------------------------------------------------- #
    # Capture module facade (#32 step 5)                              #
    # ---------------------------------------------------------------- #
    # The capture surface (imports wizard + bank-feeds REST + AI
    # document extraction, plus the sync/reconcile background jobs) can
    # be hosted either in-process in the engine OR in sibling
    # ``capture-web`` / ``capture-worker`` containers running the SAME
    # code with these flags UNSET. Same shape as the pre-accounting
    # split (step 4): both containers share ONE Postgres database (the
    # capture-owned tables move to schema ``capture`` in a sibling
    # step; ``search_path`` keeps the ORM resolving them transparently),
    # so delegation is a *process* boundary, not a data boundary.
    #
    # ``capture_base_url`` — when NON-EMPTY, the engine's imports /
    #   bank-feeds / ai-extraction route handlers delegate their work to
    #   the module over HTTP instead of running in-process. Default ""
    #   = in-process (zero behaviour change). Flipping the flag is fully
    #   reversible: unset it and the in-process path returns. The MODULE
    #   container MUST leave this empty (else it would recurse into
    #   itself) — it is the in-process executor.
    capture_base_url: str = Field(default="", alias="CAPTURE_BASE_URL")
    # ``capture_service_token`` — the shared secret the ENGINE sends as
    #   the ``X-Capture-Token`` header when delegating. Compared
    #   (constant-time) by the module against its own ``CAPTURE_TOKEN``;
    #   the two must match across the deploy.
    capture_service_token: str = Field(
        default="", alias="CAPTURE_SERVICE_TOKEN"
    )
    # ``capture_token`` — the MODULE's inbound gate. Presented as
    #   ``X-Capture-Token`` and compared constant-time. EMPTY
    #   HARD-DISABLES the module surface (503 fail-closed) in every
    #   environment: the module fronts tenant data, so an unconfigured
    #   instance must refuse rather than serve ungated.
    capture_token: str = Field(default="", alias="CAPTURE_TOKEN")
    # ``capture_sync_interval_minutes`` — the ``capture-worker`` loop
    #   period. 0 (or unset) = the worker idles and only logs a
    #   heartbeat; it does NOT run sync/reconcile. This is the DEFAULT:
    #   no scheduler currently invokes ``sync-feeds`` anywhere on the
    #   host, so the worker is forward-provisioning, not a live
    #   migration. Set a positive integer to activate periodic runs.
    capture_sync_interval_minutes: int = Field(
        default=0, alias="CAPTURE_SYNC_INTERVAL_MINUTES"
    )

    # ---------------------------------------------------------------- #
    # Platform / identity module facade (#32 wave 1)                  #
    # ---------------------------------------------------------------- #
    # The platform surface (Stripe billing webhook + public signup /
    # email-verification / password-reset / magic-link + launch-promo
    # MINT) can be hosted either in-process in the engine OR in a sibling
    # ``platform-web`` container running the SAME code with these flags
    # UNSET. Same shape as the pre-accounting (step 4) and capture
    # (step 5) splits: both containers share ONE Postgres database (the
    # moved flows write ``tenants`` / ``users`` via the OWNER-role session
    # factory ``LoginSessionLocal`` — no schema move, no RLS change), so
    # delegation is a *process* boundary, not a data boundary. Identity
    # VERIFICATION (require_bearer / jwt decode / interceptors) stays in
    # the engine unchanged — only the unauthenticated / signature-authed
    # WRITE surfaces move.
    #
    # ``platform_base_url`` — when NON-EMPTY, the engine's billing-webhook
    #   and signup/magic-link route handlers delegate their work to the
    #   module over HTTP instead of running in-process. Default "" =
    #   in-process (zero behaviour change / instant rollback). The MODULE
    #   container MUST leave this empty (else it would recurse into
    #   itself) — it is the in-process executor.
    platform_base_url: str = Field(default="", alias="PLATFORM_BASE_URL")
    # ``platform_service_token`` — the shared secret the ENGINE sends as
    #   the ``X-Platform-Token`` header when delegating. Compared
    #   (constant-time) by the module against its own ``PLATFORM_TOKEN``;
    #   the two must match across the deploy.
    platform_service_token: str = Field(
        default="", alias="PLATFORM_SERVICE_TOKEN"
    )
    # ``platform_token`` — the MODULE's inbound gate. Presented as
    #   ``X-Platform-Token`` and compared constant-time. EMPTY
    #   HARD-DISABLES the module surface (503 fail-closed) in every
    #   environment: the module fronts tenant/identity data, so an
    #   unconfigured instance must refuse rather than serve ungated.
    platform_token: str = Field(default="", alias="PLATFORM_TOKEN")


    # ---------------------------------------------------------------- #
    # Supplier statement reconciliation (Phase 1, #28)               #
    # ---------------------------------------------------------------- #
    # LLM gateway for supplier-statement OCR extraction. Uses the same
    # litellm proxy as AI extraction but on separate config keys so the
    # statement pipeline can target a different model / key / base URL
    # without conflicting with receipt extraction settings.
    #
    # statement_llm_api_key: bearer token for the litellm gateway.
    #   When empty, extract_statement raises on use (fail-closed).
    # statement_llm_base: base URL of the OpenAI-compatible endpoint.
    #   Defaults to the same litellm gateway used for receipts.
    # statement_llm_model: primary extraction model.
    # statement_llm_model_escalation: model used for a second extraction
    #   attempt when the balance-reconciliation gate trips. Typically a
    #   more capable (and slower/costlier) model.
    statement_llm_api_key: str = Field(default='', alias='STATEMENT_LLM_API_KEY')
    statement_llm_base: str = Field(
        default='http://litellm:4000/v1',
        alias='STATEMENT_LLM_BASE',
    )
    statement_llm_model: str = Field(
        default='claude-sonnet-4-6',
        alias='STATEMENT_LLM_MODEL',
    )
    statement_llm_model_escalation: str = Field(
        default='claude-opus-4-7',
        alias='STATEMENT_LLM_MODEL_ESCALATION',
    )
    # Vision model for image-only / scanned statements (P4 #28).
    # When OCR text is absent or too short, the ingest layer downloads
    # the document binary and routes it through this model.  Defaults to
    # claude-haiku-4-5 (fast + multimodal); override to a more capable
    # vision model if accuracy on complex layouts requires it.
    statement_llm_vision_model: str = Field(
        default='claude-haiku-4-5',
        alias='STATEMENT_LLM_VISION_MODEL',
    )

    # ---------------------------------------------------------------- #
    # Ephemeral per-visit demo tenants (public preview)                #
    # ---------------------------------------------------------------- #
    # See migration 0170 + saebooks/services/ephemeral_demo.py. The web
    # container calls POST /internal/demo/provision over the docker
    # network on a fresh root visit; the engine mints a brand-new
    # company (its own RLS tenant) + demo user + JWT, then a 60s reaper
    # hard-deletes idle / aged demos. All knobs below carry the design
    # defaults; production overrides via env.
    #
    # demo_ephemeral_enabled — master switch. When false the provision
    #   endpoint 503s ("demo_disabled") and the reaper loop does not run.
    demo_ephemeral_enabled: bool = Field(
        default=False, alias="DEMO_EPHEMERAL_ENABLED"
    )
    # Idle TTL in seconds — reap when now - last_seen_at exceeds this.
    demo_idle_ttl: int = Field(default=1800, alias="DEMO_IDLE_TTL")
    # Absolute max age in seconds — reap when now - created_at exceeds
    # this, regardless of activity.
    demo_max_age: int = Field(default=7200, alias="DEMO_MAX_AGE")
    # Hard cap on concurrent live demo tenants. At cap, provision reaps
    # the oldest-idle demo first; if still at cap it returns 503.
    demo_max_tenants: int = Field(default=50, alias="DEMO_MAX_TENANTS")
    # Per-source-IP provision rate-limit (provisions per rolling minute).
    demo_provision_per_ip_per_min: int = Field(
        default=6, alias="DEMO_PROVISION_PER_IP_PER_MIN"
    )
    # Reaper sweep interval in seconds.
    demo_reaper_interval: int = Field(
        default=60, alias="DEMO_REAPER_INTERVAL"
    )
    # Internal shared secret gating POST /internal/demo/provision. The web
    # container sends it as the X-Internal-Secret header. Empty default:
    # in dev/test the guard is skipped (the endpoint is already
    # unreachable from the public edge, which only routes to the web
    # container); in any non-dev/test env an empty secret hard-disables
    # the endpoint (503) so a misconfigured prod can never expose it
    # ungated.
    demo_internal_secret: str = Field(
        default="", alias="DEMO_INTERNAL_SECRET"
    )
    # demo_seed_flavour — which dataset a provisioned demo gets:
    #   "saebooks" (default): full AU CoA + tax codes + draft invoices
    #   "cashbook": AU CoA + tax codes, company flipped to bookkeeping_mode=
    #     cashbook, + ~30 sole-trader cashbook entries (for the cashbook demo).
    # Set per-api-instance (the cashbook-demo-api sets DEMO_SEED_FLAVOUR=cashbook).
    demo_seed_flavour: str = Field(
        default="saebooks", alias="DEMO_SEED_FLAVOUR"
    )
    # demo_template_max_age — the clone template freezes its dates at seed time,
    # so cloned demos drift stale (a "yesterday" transaction becomes "8 days
    # ago"). The reaper purges + re-seeds the template once it is older than
    # this, regenerating dates relative to today. Default 7 days; 0 disables.
    demo_template_max_age: int = Field(
        default=604800, alias="DEMO_TEMPLATE_MAX_AGE"
    )
    # demo_template_company_id — Option A escape hatch. When set to a company
    # UUID, the clone points at that operator-seeded company as its template
    # instead of the engine building/owning one per flavour. The engine then
    # NEVER purges/re-seeds it (_refresh_stale_template is a no-op) — its
    # lifecycle belongs to the operator. Empty (default) = engine-owned
    # template per DEMO_SEED_FLAVOUR.
    demo_template_company_id: str = Field(
        default="", alias="DEMO_TEMPLATE_COMPANY_ID"
    )

    # ---------------------------------------------------------------- #
    # Scheduled backups (planned-modules Wave E, FLAG_SCHEDULED_BACKUPS) #
    # ---------------------------------------------------------------- #
    # Per-tenant LOGICAL export (never the whole-DB pg_dump — see
    # services/backups.py, which stays read-only status-view-only).
    # Encrypted artifacts (ciphertext ONLY — the passphrase is never
    # persisted, see services/backup_crypto.py) stage here pending
    # download / a future real remote push. Deliberately a SEPARATE
    # directory from SAEBOOKS_BACKUP_DIR_IN_CONTAINER (the infra
    # pg_dump timer's output) — different writer, different retention
    # policy, different (tenant-partitioned) layout.
    scheduled_backup_export_dir: str = Field(
        default="/app/scheduled-backups", alias="SAEBOOKS_SCHEDULED_BACKUP_EXPORT_DIR"
    )
    # Local-path destination pushes are only permitted under this root —
    # a config-supplied absolute path outside it is rejected. Prevents a
    # tenant admin's destination config from writing anywhere on the
    # container filesystem. Rclone-remote destinations are a stubbed
    # extension point (services/backup_destinations.py) and don't use
    # this setting.
    scheduled_backup_local_dest_root: str = Field(
        default="/app/scheduled-backups-local-dest",
        alias="SAEBOOKS_SCHEDULED_BACKUP_LOCAL_DEST_ROOT",
    )

    @property
    def oauth_allowed_emails_set(self) -> set[str]:
        return {e.strip().lower() for e in self.oauth_allowed_emails.split(",") if e.strip()}

    # ---------------------------------------------------------------- #
    # Jurisdiction-module bootstrap (Job C registration inversion)      #
    # ---------------------------------------------------------------- #
    # Boot-time selection of which jurisdiction bolt-on modules
    # self-register (``saebooks.bootstrap.jurisdictions.ensure_loaded``).
    # This is the LOADABLE SET (a restart-time config choice) — distinct
    # from the PER-COMPANY ``Company.jurisdiction`` runtime choice, which
    # picks from whatever this set actually loaded. Comma-separated,
    # whitespace and case-insensitive, same pattern as
    # ``oauth_allowed_emails`` above.
    #
    # ``ee`` is included: its tax engine is core-native
    # (``services/tax_engine/ee.py``), but the ``jurisdictions/ee``
    # package self-registers its ``ee/default`` chart-template applier and
    # control-account codes on import, so ``bootstrap.jurisdictions``
    # loads it from this set (import-light — see that package's docstring).
    enabled_jurisdictions: str = Field(
        default="au,nz,uk,lt,lv,ee", alias="SAEBOOKS_ENABLED_JURISDICTIONS"
    )

    @property
    def enabled_jurisdictions_set(self) -> set[str]:
        return {
            c.strip().lower() for c in self.enabled_jurisdictions.split(",") if c.strip()
        }

settings = Settings()
