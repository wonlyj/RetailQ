from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True,
    )
    openai_base_url: str = Field(default="", repr=False)
    openai_api_key: SecretStr = Field(default=SecretStr(""), repr=False)
    openai_model: str = ""
    context_window_tokens: int = Field(default=8192, gt=0)
    max_output_tokens: int = Field(default=1024, gt=0)
    token_safety_margin: int = Field(default=512, ge=0)
    structured_output_method: Literal["json_mode", "json_schema"] = "json_mode"
    model_timeout_seconds: float = Field(default=60, gt=0)

    @field_validator("openai_base_url", "openai_model")
    @classmethod
    def strip_value(cls, value: str) -> str:
        return value.strip()

    @field_validator("openai_base_url")
    @classmethod
    def check_url(cls, value: str) -> str:
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError("base URL must use HTTP(S)")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("base URL must not contain credentials, query or fragment")
        return value

    @model_validator(mode="after")
    def check_budget(self):
        if self.input_budget <= 0:
            raise ValueError("context window must exceed output reservation and safety margin")
        return self

    @property
    def input_budget(self) -> int:
        return self.context_window_tokens - self.max_output_tokens - self.token_safety_margin

    @property
    def is_ready(self) -> bool:
        return bool(self.openai_base_url and self.openai_model and self.openai_api_key.get_secret_value().strip())
