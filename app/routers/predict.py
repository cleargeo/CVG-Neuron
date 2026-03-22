"""
CVG Neuron AI Orchestration System — /api/predict Router
Version: 2.0.0 | Clearview Geographic LLC

Direct AI inference endpoint (faster than /process, no agent routing).
"""

from __future__ import annotations

import time
from typing import Any, Dict

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.core.config import settings
from app.core.logger import get_logger
from app.core.security import optional_auth
from app.models.response import NeuronResponse
from app.models.task import PredictRequest, PredictResult

router = APIRouter(prefix="/api/predict", tags=["Predict"])
log = get_logger("router.predict")


@router.post(
    "",
    response_model=NeuronResponse,
    summary="Direct AI inference",
    description=(
        "Direct inference call to the configured AI provider. "
        "Bypasses agent routing for maximum speed. "
        "Use /api/process for full orchestration."
    ),
)
async def predict(
    request_body: PredictRequest,
    request: Request,
    auth: Dict[str, Any] = Depends(optional_auth),
) -> NeuronResponse:
    """
    Fast direct inference using the default or specified AI provider.

    Supports:
    - `completion`: Text generation / chat completion
    - `classification`: Classify input into categories
    - `embedding`: Get vector embeddings
    - `extraction`: Extract structured data from text

    **Example:**
    ```json
    {
      "input": "Summarize the flood risk factors for coastal areas",
      "task_type": "completion",
      "provider": "ollama",
      "model": "llama3.2"
    }
    ```
    """
    t0 = time.monotonic()
    provider = request_body.provider or settings.default_ai_provider
    model = request_body.model or _get_default_model(provider)

    try:
        output = await _call_provider(provider, model, request_body)
        latency_ms = int((time.monotonic() - t0) * 1000)

        result = PredictResult(
            output=output,
            model=model,
            provider=provider,
            tokens_used=len(str(output).split()) * 2,
            latency_ms=latency_ms,
        )

        return NeuronResponse.ok(
            data=result.model_dump(),
            message="Prediction complete",
            duration_ms=latency_ms,
        )

    except Exception as exc:
        log.exception("Predict failed", provider=provider, model=model)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Inference failed: {exc}",
        )


@router.get(
    "/models",
    response_model=NeuronResponse,
    summary="List available models",
)
async def list_models() -> NeuronResponse:
    """Return available models across all configured AI providers."""
    models: Dict[str, Any] = {
        "ollama": {"base_url": settings.ollama_base_url, "default": settings.ollama_default_model},
        "openai": {"available": bool(settings.openai_api_key), "default": settings.openai_default_model},
        "anthropic": {"available": bool(settings.anthropic_api_key), "default": settings.anthropic_default_model},
        "default_provider": settings.default_ai_provider,
    }

    # Fetch Ollama model list if available
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.ollama_base_url}/api/tags")
            if resp.status_code == 200:
                models["ollama"]["models"] = [
                    m["name"] for m in resp.json().get("models", [])
                ]
    except Exception:
        models["ollama"]["models"] = [settings.ollama_default_model]

    return NeuronResponse.ok(data=models, message="Available models")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_default_model(provider: str) -> str:
    if provider == "openai":
        return settings.openai_default_model
    elif provider == "anthropic":
        return settings.anthropic_default_model
    return settings.ollama_default_model


async def _call_provider(provider: str, model: str, request_body: PredictRequest) -> Any:
    """Route to the correct AI provider."""
    messages = [{"role": "user", "content": request_body.input}]
    params = request_body.parameters

    async with httpx.AsyncClient(timeout=60) as client:
        if provider == "ollama":
            resp = await client.post(
                f"{settings.ollama_base_url}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "options": {
                        "temperature": params.get("temperature", 0.7),
                        "num_predict": params.get("max_tokens", 2048),
                    },
                },
            )
            resp.raise_for_status()
            return resp.json().get("message", {}).get("content", "")

        elif provider == "openai":
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json={
                    "model": model,
                    "messages": messages,
                    "max_tokens": params.get("max_tokens", 2048),
                    "temperature": params.get("temperature", 0.7),
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

        elif provider == "anthropic":
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": model,
                    "max_tokens": params.get("max_tokens", 2048),
                    "messages": messages,
                },
            )
            resp.raise_for_status()
            return resp.json()["content"][0]["text"]

        else:
            raise ValueError(f"Unknown provider: {provider}")
