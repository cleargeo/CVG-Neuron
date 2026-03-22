"""neuron/config.py -- CVG Neuron Centralized Configuration
(c) Clearview Geographic, LLC -- Proprietary

Single source of truth for all configuration.
Reads from env vars with sane defaults.

Usage:
    from neuron.config import cfg, get_config
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field, asdict
from typing import Optional

NEURON_VERSION = "1.1.0"
NEURON_SERVICE = "cvg-neuron"
NEURON_CODENAME = "Hive Edition"


@dataclass
class NeuronConfig:
    """CVG Neuron runtime configuration (all from env vars)."""

    OLLAMA_BASE_URL: str = field(default_factory=lambda: os.getenv(
        "OLLAMA_HOST", os.getenv("OLLAMA_URL", "http://10.10.10.200:11434")))
    OLLAMA_MODEL: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", "cvg-neuron"))
    OLLAMA_ALT_MODEL: str = field(default_factory=lambda: os.getenv("OLLAMA_ALT_MODEL", "llama3.1:8b"))
    OLLAMA_TIMEOUT: float = field(default_factory=lambda: float(os.getenv("OLLAMA_TIMEOUT", "300")))
    NEURON_HOST: str = field(default_factory=lambda: os.getenv("NEURON_HOST", "0.0.0.0"))
    NEURON_PORT: int = field(default_factory=lambda: int(os.getenv("NEURON_PORT", "8095")))
    DATA_DIR: str = field(default_factory=lambda: os.getenv("NEURON_DATA_DIR", "/app/data"))
    CVG_INTERNAL_KEY: str = field(default_factory=lambda: os.getenv("CVG_INTERNAL_KEY", "cvg-internal-2026"))
    EDGE_SECRET: str = field(default_factory=lambda: os.getenv("CVG_TUNNEL_SECRET", os.getenv("EDGE_SECRET", "cvg-neuron-tunnel-2026")))
    CONTAINERIZATION_ENGINE_URL: str = field(default_factory=lambda: os.getenv("CONTAINERIZATION_ENGINE_URL", "http://cvg-support-engine:8091"))
    GIT_ENGINE_URL: str = field(default_factory=lambda: os.getenv("GIT_ENGINE_URL", "http://cvg-git-engine:8092"))
    DNS_ENGINE_URL: str = field(default_factory=lambda: os.getenv("DNS_ENGINE_URL", "http://cvg-dns-engine:8094"))
    AUDIT_ENGINE_URL: str = field(default_factory=lambda: os.getenv("AUDIT_ENGINE_URL", "http://10.10.10.220:8001"))
    CONTEXT_REFRESH_INTERVAL: int = field(default_factory=lambda: int(os.getenv("CONTEXT_REFRESH_INTERVAL", "300")))
    MAX_CONVERSATION_HISTORY: int = field(default_factory=lambda: int(os.getenv("NEURON_MAX_CTX_MSGS", os.getenv("MAX_CONVERSATION_HISTORY", "20"))))
    NODE_PROBE_TIMEOUT: float = field(default_factory=lambda: float(os.getenv("NODE_PROBE_TIMEOUT", "3.0")))
    CVG_POLL_TIMEOUT: float = field(default_factory=lambda: float(os.getenv("CVG_POLL_TIMEOUT", "5.0")))
    LOG_LEVEL: str = field(default_factory=lambda: os.getenv("NEURON_LOG_LEVEL", os.getenv("LOG_LEVEL", "INFO")).upper())
    LOG_FORMAT: str = field(default_factory=lambda: os.getenv("LOG_FORMAT", "json"))
    ENABLE_DOCS: bool = field(default_factory=lambda: os.getenv("ENABLE_DOCS", "true").lower() == "true")

    def public_dict(self) -> dict:
        """Return a safe, secret-free copy of config for API responses."""
        d = asdict(self)
        for key in ("CVG_INTERNAL_KEY", "EDGE_SECRET"):
            d.pop(key, None)
        d["CVG_INTERNAL_KEY_set"] = bool(self.CVG_INTERNAL_KEY)
        d["EDGE_SECRET_set"]      = bool(self.EDGE_SECRET)
        return d

_config: Optional[NeuronConfig] = None


def get_cfg() -> NeuronConfig:
    """Return the global NeuronConfig singleton (lazy-initialised)."""
    global _config
    if _config is None:
        _config = NeuronConfig()
    return _config


def __getattr__(name: str):
    """Module-level lazy attr so `from neuron.config import cfg` works."""
    if name == "cfg":
        return get_cfg()
    raise AttributeError(f"module neuron.config has no attribute {name!r}")


def get_config() -> dict:
    """Return public-safe config dict for API responses. Secrets excluded."""
    c = get_cfg()
    return {
        "service":  NEURON_SERVICE,
        "version":  NEURON_VERSION,
        "codename": NEURON_CODENAME,
        "ollama_url":   c.OLLAMA_BASE_URL,
        "ollama_model": c.OLLAMA_MODEL,
        "ollama_alt":   c.OLLAMA_ALT_MODEL,
        "host":         c.NEURON_HOST,
        "port":         c.NEURON_PORT,
        "data_dir":     c.DATA_DIR,
        "log_level":    c.LOG_LEVEL,
        "log_format":   c.LOG_FORMAT,
        "context_refresh_interval": c.CONTEXT_REFRESH_INTERVAL,
        "max_conversation_history": c.MAX_CONVERSATION_HISTORY,
        "node_probe_timeout":       c.NODE_PROBE_TIMEOUT,
        "engines": {
            "container": c.CONTAINERIZATION_ENGINE_URL,
            "git":       c.GIT_ENGINE_URL,
            "dns":       c.DNS_ENGINE_URL,
            "audit":     c.AUDIT_ENGINE_URL,
        },
        "secrets_configured": {
            "cvg_internal_key": bool(c.CVG_INTERNAL_KEY),
            "edge_secret":      bool(c.EDGE_SECRET),
        },
    }
