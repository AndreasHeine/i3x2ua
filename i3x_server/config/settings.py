from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="I3X_", env_file=".env", extra="ignore")

    opcua_endpoint: str = Field(default="opc.tcp://localhost:4840")
    opcua_security_mode: str = Field(default="None")
    model_refresh_interval_seconds: int = Field(default=60, ge=0)
    log_level: str = Field(default="INFO")


settings = Settings()
