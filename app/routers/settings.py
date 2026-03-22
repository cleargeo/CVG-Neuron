"""
CVG Neuron AI Orchestration System — /api/settings Router
Version: 2.0.0 | Clearview Geographic LLC

Runtime settings and configuration management.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.core.config import settings
from app.core.logger import get_logger
from app.core.security import require_admin
from app.models.response import NeuronResponse

router = APIRouter(prefix="/api/settings", tags=["Settings"])
log = get_logger("router.settings")


class SettingsUpdate(BaseModel):
    """Partial settings update payload (runtime-modifiable fields only)."""
    default_cognitive_level: Optional[str] = None
    default_ai_provider: Optional[str] = None
    max_concurrent_tasks: Optional[int] = None
    task_timeout_seconds: Optional[int] = None
    observability_enabled: Optional[bool] = None
    log_level: Optional[str] = None
    neuro_cache_strategy: Optional[str] = None


# Runtime-mutable settings (subset of all settings)
_runtime_overrides: Dict[str, Any] = {}


@router.get(
    "",
    response_model=NeuronResponse,
    summary="Get current settings",
)
async def get_settings_view(
    auth: Dict[str, Any] = Depends(require_admin),
) -> NeuronResponse:
    """
    Return current CVG Neuron runtime configuration.
    Sensitive values (keys, passwords) are redacted.
    """
    safe_settings = {
        "app_name": settings.app_name,
        "app_version": settings.app_version,
        "environment": settings.environment,
        "neuron_id": settings.neuron_id,
        "neuron_employee_id": settings.neuron_employee_id,
        "neuron_primary_hive": settings.neuron_primary_hive,
        "neuron_primary_queen": settings.neuron_primary_queen,
        "port": settings.port,
        "default_cognitive_level": settings.default_cognitive_level,
        "default_ai_provider": settings.default_ai_provider,
        "max_concurrent_tasks": settings.max_concurrent_tasks,
        "task_timeout_seconds": settings.task_timeout_seconds,
        "neuro_cache_max_size": settings.neuro_cache_max_size,
        "neuro_cache_strategy": settings.neuro_cache_strategy,
        "cache_ttl_seconds": settings.cache_ttl_seconds,
        "hive_endpoint": settings.hive_endpoint,
        "hive_nodes": settings.hive_nodes,
        "hive_timeout": settings.hive_timeout,
        "comb_endpoint": settings.comb_endpoint,
        "observability_endpoint": settings.observability_endpoint,
        "observability_enabled": settings.observability_enabled,
        "log_level": settings.log_level,
        "log_format": settings.log_format,
        # Redact secrets
        "openai_api_key": "***" if settings.openai_api_key else "",
        "anthropic_api_key": "***" if settings.anthropic_api_key else "",
        "secret_key": "***",
        # Runtime overrides applied
        "runtime_overrides": _runtime_overrides,
    }
    return NeuronResponse.ok(data=safe_settings, message="Current settings")


@router.put(
    "",
    response_model=NeuronResponse,
    summary="Update runtime settings",
)
async def update_settings(
    update: SettingsUpdate,
    request: Request,
    auth: Dict[str, Any] = Depends(require_admin),
) -> NeuronResponse:
    """
    Update runtime-modifiable settings without restart.

    Note: Some settings require a restart to take full effect
    (e.g., log_level, cache_strategy).
    """
    orchestrator = request.app.state.orchestrator
    changes: Dict[str, Any] = {}

    if update.default_cognitive_level is not None:
        _runtime_overrides["default_cognitive_level"] = update.default_cognitive_level
        changes["default_cognitive_level"] = update.default_cognitive_level
        log.info("Runtime setting updated", key="default_cognitive_level", value=update.default_cognitive_level)

    if update.default_ai_provider is not None:
        _runtime_overrides["default_ai_provider"] = update.default_ai_provider
        changes["default_ai_provider"] = update.default_ai_provider

    if update.max_concurrent_tasks is not None:
        _runtime_overrides["max_concurrent_tasks"] = update.max_concurrent_tasks
        changes["max_concurrent_tasks"] = update.max_concurrent_tasks

    if update.task_timeout_seconds is not None:
        _runtime_overrides["task_timeout_seconds"] = update.task_timeout_seconds
        changes["task_timeout_seconds"] = update.task_timeout_seconds

    if update.observability_enabled is not None:
        orchestrator.observability._enabled = update.observability_enabled
        _runtime_overrides["observability_enabled"] = update.observability_enabled
        changes["observability_enabled"] = update.observability_enabled

    if update.log_level is not None:
        import logging
        logging.root.setLevel(update.log_level.upper())
        _runtime_overrides["log_level"] = update.log_level
        changes["log_level"] = update.log_level

    if update.neuro_cache_strategy is not None:
        orchestrator.cache._strategy = update.neuro_cache_strategy.upper()
        _runtime_overrides["neuro_cache_strategy"] = update.neuro_cache_strategy
        changes["neuro_cache_strategy"] = update.neuro_cache_strategy

    log.info("Settings updated", changes=changes)
    return NeuronResponse.ok(
        data={"applied_changes": changes},
        message=f"Updated {len(changes)} setting(s)",
    )
