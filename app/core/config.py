"""
CVG Neuron AI Orchestration System — Configuration
Version: 2.0.0 | Clearview Geographic LLC

Pulls from environment variables / .env file.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal, List

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All CVG Neuron configuration, sourced from environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # ── Application ───────────────────────────────────────────────────────────
    app_name: str = "CVG Neuron"
    app_version: str = "2.0.0"
    environment: Literal["development", "staging", "production"] = "development"
    app_debug: bool = Field(default=False, alias="APP_DEBUG")

    # ── Neuron Identity ───────────────────────────────────────────────────────
    neuron_id: str = "CVG-NEURON-001"
    neuron_employee_id: str = "CVG-AI-001"
    neuron_primary_hive: str = "HIVE-0"
    neuron_primary_queen: str = "CVG-QUEEN-13"

    # ── Network ───────────────────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8808
    workers: int = 4

    # ── Security / JWT ────────────────────────────────────────────────────────
    secret_key: str = Field(
        default="changeme-please-generate-with-openssl-rand-hex-32",
        description="JWT signing key — MUST be overridden in production",
    )
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440  # 24 hours

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = "sqlite+aiosqlite:///./cvg_neuron.db"
    database_echo: bool = False

    # ── Redis / NeuroCache ────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    redis_password: str = ""
    cache_ttl_seconds: int = 3600
    neuro_cache_max_size: int = 512
    neuro_cache_strategy: Literal["LRU", "LFU", "FIFO"] = "LRU"

    # ── CVG Hive ──────────────────────────────────────────────────────────────
    hive_endpoint: str = "http://192.168.100.38:8808/api/hive/coordinate"
    hive_node_0: str = "192.168.100.38"
    hive_node_1: str = "192.168.100.39"
    hive_node_2: str = "192.168.100.40"
    hive_timeout: int = 30
    hive_retries: int = 3

    # ── CVG COMB ──────────────────────────────────────────────────────────────
    comb_endpoint: str = "http://192.168.100.38:8808/api/comb/store"
    comb_pollen_store: str = "http://192.168.100.38:8808/api/comb/pollen"
    comb_bit_hive: str = "http://192.168.100.38:8808/api/comb/bithive"
    comb_wax_cell: str = "http://192.168.100.38:8808/api/comb/waxcell"

    # ── CVG Observability ─────────────────────────────────────────────────────
    observability_endpoint: str = "http://192.168.100.38:9090/api/metrics"
    observability_enabled: bool = True
    metrics_port: int = 9091

    # ── CVG Network ───────────────────────────────────────────────────────────
    network_endpoint: str = "http://192.168.100.38:8808/api/network"

    # ── CVG StratoVault ───────────────────────────────────────────────────────
    stratovault_endpoint: str = "http://192.168.100.38:8443/api/auth"
    stratovault_enabled: bool = False

    # ── CVG Engine URLs ───────────────────────────────────────────────────────
    # Each engine has a canonical env var (e.g. GIT_ENGINE_URL) matching .env
    git_engine_url: str = Field(
        default="http://cvg-git-engine:8092",
        validation_alias=AliasChoices("git_engine_url", "GIT_ENGINE_URL"),
    )
    dns_engine_url: str = Field(
        default="http://cvg-dns-engine:8094",
        validation_alias=AliasChoices("dns_engine_url", "DNS_ENGINE_URL"),
    )
    containerization_engine_url: str = Field(
        default="http://cvg-support-engine:8091",
        validation_alias=AliasChoices(
            "containerization_engine_url", "CONTAINERIZATION_ENGINE_URL"
        ),
    )
    audit_engine_url: str = Field(
        default="http://10.10.10.220:8001",
        validation_alias=AliasChoices("audit_engine_url", "AUDIT_ENGINE_URL"),
    )
    # Internal API key shared across all CVG engines
    cvg_internal_key: str = Field(
        default="cvg-internal-2026",
        validation_alias=AliasChoices("cvg_internal_key", "CVG_INTERNAL_KEY"),
    )

    # ── AI Providers ──────────────────────────────────────────────────────────
    openai_api_key: str = ""
    openai_default_model: str = "gpt-4o"

    anthropic_api_key: str = ""
    anthropic_default_model: str = "claude-3-5-sonnet-20241022"

    # Accepts OLLAMA_BASE_URL (canonical), OLLAMA_URL, or OLLAMA_HOST from .env
    ollama_base_url: str = Field(
        default="http://192.168.100.38:11434",
        validation_alias=AliasChoices(
            "ollama_base_url", "OLLAMA_BASE_URL", "OLLAMA_URL", "OLLAMA_HOST"
        ),
    )
    # Accepts OLLAMA_DEFAULT_MODEL (canonical) or OLLAMA_MODEL from .env
    ollama_default_model: str = Field(
        default="llama3.2",
        validation_alias=AliasChoices(
            "ollama_default_model", "OLLAMA_DEFAULT_MODEL", "OLLAMA_MODEL"
        ),
    )

    default_ai_provider: Literal["openai", "anthropic", "ollama"] = "ollama"

    # ── Cognitive Processing ──────────────────────────────────────────────────
    default_cognitive_level: Literal["basic", "advanced", "neural", "autonomous"] = "advanced"
    max_concurrent_tasks: int = 20
    task_timeout_seconds: int = 300

    # ── Logging ───────────────────────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "text"] = "json"
    log_file: str = "logs/neuron.log"

    # ── CORS ──────────────────────────────────────────────────────────────────
    cors_origins: str = "*"
    cors_allow_credentials: bool = True

    # ── Computed properties ───────────────────────────────────────────────────
    @property
    def cors_origins_list(self) -> List[str]:
        if self.cors_origins == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",")]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        return self.environment == "development"

    @property
    def hive_nodes(self) -> List[str]:
        return [self.hive_node_0, self.hive_node_1, self.hive_node_2]

    @field_validator("secret_key")
    @classmethod
    def warn_default_secret(cls, v: str) -> str:
        if v.startswith("changeme") and os.getenv("ENVIRONMENT", "development") == "production":
            raise ValueError("SECRET_KEY must be changed in production!")
        return v


@lru_cache()
def get_settings() -> Settings:
    """Cached settings instance — call this everywhere."""
    return Settings()


# Module-level convenience alias
settings: Settings = get_settings()
