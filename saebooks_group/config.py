"""Broker configuration — money-free relay service settings."""
from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BrokerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # The broker's OWN database (saebooks_group). Its role has NO credentials to
    # any tenant DB — the broker reaches tenants only over HTTP.
    database_url: str = Field(
        default="postgresql+asyncpg://saebooks_group:change-me@db:5432/saebooks_group",
        alias="SAEBOOKS_GROUP_DATABASE_URL",
    )
    bind_host: str = Field(default="0.0.0.0", alias="SAEBOOKS_GROUP_BIND_HOST")
    bind_port: int = Field(default=8000, alias="SAEBOOKS_GROUP_BIND_PORT")
    log_level: str = Field(default="INFO", alias="SAEBOOKS_GROUP_LOG_LEVEL")
    # Total timeout for the forward POST to a partner /ic/accept.
    forward_timeout_seconds: float = Field(
        default=30.0, alias="SAEBOOKS_GROUP_FORWARD_TIMEOUT_SECONDS"
    )
    # Phase 3b ships /ic/relay as 501; 3c flips this on to forward live.
    relay_forwarding_enabled: bool = Field(
        default=False, alias="SAEBOOKS_GROUP_RELAY_FORWARDING_ENABLED"
    )
    # Freshness window (seconds) the broker enforces on an inbound relay BEFORE it
    # forwards, mirroring the receiver /ic/accept guard. A message whose issued_at
    # is older than this (or as far in the future) is rejected at the first hop,
    # so a captured envelope cannot be re-injected through the broker outside a
    # tight window. Default 10 min; keep it equal to the tenant
    # SAEBOOKS_IC_RELAY_FRESHNESS_SECONDS so both hops agree.
    relay_freshness_seconds: int = Field(
        default=600, alias="SAEBOOKS_GROUP_RELAY_FRESHNESS_SECONDS"
    )


settings = BrokerSettings()
