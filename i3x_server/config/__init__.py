from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="I3X_", env_file=".env", extra="ignore")

    opcua_endpoint: str = Field(default="opc.tcp://opcua.umati.app:4843")
    opcua_username: str | None = Field(default=None)
    opcua_password: str | None = Field(default=None)
    opcua_security_mode: str = Field(default="None")
    opcua_security_policy: str | None = Field(default=None)
    opcua_client_cert_path: str | None = Field(default=None)
    opcua_client_key_path: str | None = Field(default=None)
    opcua_client_key_password: str | None = Field(default=None)
    opcua_server_cert_path: str | None = Field(default=None)
    opcua_browse_concurrency: int = Field(default=128, ge=1)
    opcua_metadata_cache_ttl_seconds: int = Field(default=300, ge=0)
    opcua_connection_monitor_interval_seconds: int = Field(default=5, ge=0)
    model_refresh_interval_seconds: int = Field(default=60, ge=0)
    model_preload_on_startup: bool = Field(default=True)
    model_preload_blocking: bool = Field(default=False)
    fail_startup_on_model_preload_error: bool = Field(default=False)
    subscriptions_initial_values: bool = Field(default=True)
    subscription_interval_seconds: float = Field(default=5.0, gt=0)
    subscription_max_updates: int = Field(default=10000, ge=1)
    subscription_ttl_seconds: int = Field(default=300, ge=1)
    log_level: str = Field(default="INFO")
    enable_writes: bool = Field(default=False)
    mcp_include_opcua_metadata: bool = Field(default=True)

    @field_validator("*", mode="before")
    @classmethod
    def _strip_env_whitespace(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip()
        return value


settings = Settings()
