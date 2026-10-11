from pathlib import Path
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .account_vault import apply_accounts


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JARVIS_", env_file="secrets/runtime.env", extra="ignore")
    database_url: str = ""
    api_token: str = ""
    mobile_url: str = ""

    @field_validator("mobile_url")
    @classmethod
    def private_mobile_url(cls, value):
        value = value.rstrip("/")
        if not value:
            return value
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or not url.hostname.endswith(".ts.net")
            or url.netloc != url.hostname
            or value != "https://" + url.hostname
            or url.path
            or url.query
            or url.fragment
        ):
            raise ValueError("Mobile URL must be an exact private Tailscale HTTPS origin")
        return value

    primary_url: str = "http://127.0.0.1:8181"
    background_url: str = "http://127.0.0.1:8184"
    embedding_url: str = "http://127.0.0.1:8182"
    reranker_url: str = "http://127.0.0.1:8183"
    ms_client_id: str = ""
    ms_tenant_id: str = ""
    outlook_browser_account: str = "demo@example.invalid"
    telegram_token: str = ""
    telegram_user_id: int = 0
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from: str = ""
    demo_phone: str = ""
    public_webhook_url: str = ""
    timezone: str = "America/Denver"
    data_dir: Path = Path("data")
    secret_dir: Path = Path("secrets")


settings = apply_accounts(Settings())
