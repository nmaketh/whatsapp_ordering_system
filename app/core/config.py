from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Secrets belong in the environment, never the database."""

    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    database_url: str = "sqlite+aiosqlite:///./kaziflow.db"
    redis_url: str = "redis://localhost:6379/0"
    meta_verify_token: SecretStr = SecretStr("development-verify-token")
    meta_app_secret: SecretStr = SecretStr("development-app-secret")
    meta_access_token: SecretStr | None = None
    meta_graph_version: str = "v22.0"
    outbound_messages_per_second: int = Field(default=70, ge=1, le=80)
    session_ttl_seconds: int = Field(default=3600, ge=300)
    default_delivery_fee: int = Field(default=0, ge=0)
    pii_encryption_key: SecretStr | None = None
    admin_api_key: SecretStr | None = None
    payment_callback_secret: SecretStr | None = None
    default_stk_provider: Literal["MTN_MOMO", "AIRTEL_MONEY", "MPESA"] = "MTN_MOMO"
    mtn_momo_request_to_pay_url: str | None = None
    mtn_momo_subscription_key: SecretStr | None = None
    mtn_momo_bearer_token: SecretStr | None = None
    mtn_momo_target_environment: str = "sandbox"
    airtel_money_payment_url: str | None = None
    airtel_money_bearer_token: SecretStr | None = None
    mpesa_stk_push_url: str | None = None
    mpesa_bearer_token: SecretStr | None = None
    worker_consumer_name: str = "worker-1"
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    @model_validator(mode="after")
    def production_secrets_are_configured(self) -> "Settings":
        if self.app_env == "production" and not self.pii_encryption_key:
            raise ValueError("PII_ENCRYPTION_KEY is required in production")
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
