from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="I3X_", env_file=".env", extra="ignore")

    opcua_endpoint: str = Field(default="opc.tcp://opcua.umati.app:4843")
    opcua_security_mode: str = Field(default="None")
    opcua_browse_concurrency: int = Field(default=16, ge=1)
    model_refresh_interval_seconds: int = Field(default=60, ge=0)
    model_preload_on_startup: bool = Field(default=True)
    fail_startup_on_model_preload_error: bool = Field(default=False)
    log_level: str = Field(default="INFO")


settings = Settings()
