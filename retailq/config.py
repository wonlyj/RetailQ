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
    mysql_host: str = "127.0.0.1"
    mysql_port: int = Field(default=3306, gt=0, le=65535)
    mysql_database: str = "retailq"
    mysql_user: str = "retailq"
    mysql_password: SecretStr = Field(default=SecretStr(""), repr=False)
    mysql_root_password: SecretStr = Field(default=SecretStr(""), repr=False)
    tool_timeout_seconds: float = Field(default=5, gt=0)
    tool_read_retries: int = Field(default=1, ge=0, le=1)
    knowledge_tokenizer_path: str = 'artifacts/knowledge/assets/tokenizer.json'
    embedding_base_url: str = 'https://api.siliconflow.cn/v1'
    embedding_api_key: SecretStr = Field(default=SecretStr(''),repr=False)
    embedding_proxy: SecretStr = Field(default=SecretStr(''),repr=False)
    embedding_model: Literal['BAAI/bge-m3'] = 'BAAI/bge-m3'
    embedding_batch_size: int = Field(default=16,gt=0,le=128)
    embedding_timeout_seconds: float = Field(default=10,gt=0,le=120)
    embedding_retries: int = Field(default=1,ge=0,le=1)
    knowledge_enabled: bool = False
    knowledge_schedule_enabled: bool = True
    knowledge_data_dir: str = 'artifacts/knowledge'
    knowledge_top_k: int = Field(default=3,ge=1,le=20)
    knowledge_min_score: float = Field(default=0.0,ge=-1,le=1)
    knowledge_chunk_target: int = Field(default=600,gt=0)
    knowledge_chunk_overlap: int = Field(default=100,ge=0)
    knowledge_schedule_hour: int = Field(default=3,ge=0,le=23)
    knowledge_schedule_minute: int = Field(default=0,ge=0,le=59)
    knowledge_timezone: Literal['Asia/Shanghai'] = 'Asia/Shanghai'
    knowledge_dedup_min_score: float = Field(default=0.85,ge=-1,le=1)
    knowledge_dedup_candidate_k: int = Field(default=8,ge=1,le=32)

    @field_validator("openai_base_url", "openai_model", "embedding_base_url")
    @classmethod
    def strip_value(cls, value: str) -> str:
        return value.strip()

    @field_validator("openai_base_url", "embedding_base_url")
    @classmethod
    def check_url(cls, value: str) -> str:
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError("base URL must use HTTP(S)")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("base URL must not contain credentials, query or fragment")
        return value

    @field_validator('embedding_proxy')
    @classmethod
    def check_embedding_proxy(cls,value: SecretStr) -> SecretStr:
        raw=value.get_secret_value().strip()
        if raw:
            parsed=urlsplit(raw)
            if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.query or parsed.fragment:
                raise ValueError('embedding proxy must be an HTTP(S) proxy URL')
            if parsed.port is not None and not 1<=parsed.port<=65535:
                raise ValueError('invalid embedding proxy port')
        return SecretStr(raw)

    @model_validator(mode="after")
    def check_budget(self):
        if self.input_budget <= 0:
            raise ValueError("context window must exceed output reservation and safety margin")
        if self.knowledge_chunk_overlap>=self.knowledge_chunk_target:
            raise ValueError('knowledge overlap must be smaller than target')
        return self

    @property
    def input_budget(self) -> int:
        return self.context_window_tokens - self.max_output_tokens - self.token_safety_margin

    @property
    def is_ready(self) -> bool:
        return bool(self.openai_base_url and self.openai_model and self.openai_api_key.get_secret_value().strip())

    @property
    def is_database_configured(self) -> bool:
        return bool(self.mysql_host and self.mysql_database and self.mysql_user and self.mysql_password.get_secret_value())

    @property
    def is_embedding_configured(self) -> bool:
        return bool(self.embedding_base_url and self.embedding_api_key.get_secret_value().strip())

    @property
    def is_knowledge_configured(self) -> bool:
        return self.knowledge_enabled and self.is_embedding_configured and self.is_database_configured

    @property
    def database_url(self):
        from sqlalchemy import URL
        return URL.create("mysql+pymysql", username=self.mysql_user,
                          password=self.mysql_password.get_secret_value(), host=self.mysql_host,
                          port=self.mysql_port, database=self.mysql_database, query={"charset": "utf8mb4"})
