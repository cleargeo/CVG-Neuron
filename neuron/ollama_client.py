# CVG Neuron -- Ollama Client v2
# (c) Clearview Geographic, LLC -- Proprietary and PRIVATE
#
# Async Ollama client for CVG Neuron cognitive substrate.
# Supports: health, chat, streaming, model listing, health_detail.
# All Ollama calls use the host configured via OLLAMA_HOST or OLLAMA_URL env vars.

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx

logger = logging.getLogger("cvg.neuron.ollama")

# Environment-driven configuration
OLLAMA_HOST    = os.getenv("OLLAMA_HOST", os.getenv("OLLAMA_URL", "http://10.10.10.200:11434"))
DEFAULT_MODEL  = os.getenv("OLLAMA_MODEL", "cvg-neuron")
_read_timeout  = float(os.getenv("OLLAMA_TIMEOUT", "300"))


class OllamaClient:
    """
    Async Ollama API client for CVG Neuron.

    This is the cognitive SUBSTRATE — not CVG Neuron's identity.
    CVG Neuron IS the intelligence; Ollama IS the inference engine underneath.

    Methods:
        health()        — Quick ping (returns True/False)
        health_detail() — Full health with model listing
        chat()          — Single-turn or multi-turn inference
        stream_chat()   — Streaming inference (async generator)
        list_models()   — List available Ollama models
        pull_model()    — Pull a model from Ollama registry
        create_model()  — Create a custom model from a Modelfile
    """

    def __init__(self, host: str = OLLAMA_HOST, model: str = DEFAULT_MODEL) -> None:
        self.host  = host.rstrip("/")
        self.model = model
        self.default_model = model
        self._timeout = httpx.Timeout(connect=10.0, read=_read_timeout, write=30.0, pool=5.0)
        logger.info("[ollama] Client initialized: host=%s model=%s", self.host, self.model)

    # ── Health ────────────────────────────────────────────────────────────────

    async def health(self) -> bool:
        """Quick liveness check. Returns True if Ollama is reachable."""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                resp = await client.get(f"{self.host}/api/tags")
                return resp.status_code == 200
        except Exception as exc:
            logger.debug("[ollama] Health check failed: %s", exc)
            return False

    async def health_detail(self) -> Dict[str, Any]:
        """
        Detailed health check. Returns status, model list, and latency.
        Used by /api/health/deep endpoint.
        """
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(8.0)) as client:
                resp = await client.get(f"{self.host}/api/tags")
                if resp.status_code != 200:
                    return {
                        "status":  "error",
                        "host":    self.host,
                        "http":    resp.status_code,
                        "latency_ms": int((time.monotonic() - t0) * 1000),
                    }
                data   = resp.json()
                models = [m.get("name", "") for m in data.get("models", [])]
                return {
                    "status":        "online",
                    "host":          self.host,
                    "default_model": self.default_model,
                    "model_count":   len(models),
                    "models":        models,
                    "latency_ms":    int((time.monotonic() - t0) * 1000),
                }
        except httpx.ConnectError:
            return {
                "status":     "offline",
                "host":       self.host,
                "error":      "Connection refused",
                "latency_ms": int((time.monotonic() - t0) * 1000),
            }
        except Exception as exc:
            return {
                "status":     "error",
                "host":       self.host,
                "error":      str(exc),
                "latency_ms": int((time.monotonic() - t0) * 1000),
            }

    # ── Inference ─────────────────────────────────────────────────────────────

    async def chat(
        self,
        messages: List[Dict[str, str]],
        system: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.3,
        num_predict: int = 2048,
        stream: bool = False,
    ) -> str:
        """
        Single-turn or multi-turn inference via Ollama /api/chat.

        Args:
            messages:    List of {"role": "user"|"assistant", "content": "..."}
            system:      Optional system prompt (prepended as system message)
            model:       Model to use (defaults to self.default_model)
            temperature: Sampling temperature (0.0–2.0)
            num_predict: Max tokens to generate
            stream:      If True, returns concatenated stream (use stream_chat() for async iter)

        Returns:
            Generated text string.
        """
        use_model = model or self.default_model
        payload: Dict[str, Any] = {
            "model":   use_model,
            "messages": messages,
            "stream":  False,
            "options": {
                "temperature":   temperature,
                "num_predict":   num_predict,
                "repeat_penalty": 1.1,
            },
        }
        if system:
            # Prepend system message if provided
            payload["messages"] = [{"role": "system", "content": system}] + messages

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self.host}/api/chat",
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("message", {}).get("content", "")
        except httpx.ReadTimeout:
            logger.error("[ollama] chat() timed out after %.0fs (model=%s)", _read_timeout, use_model)
            raise
        except httpx.HTTPStatusError as exc:
            logger.error("[ollama] chat() HTTP %d: %s", exc.response.status_code, exc.response.text[:200])
            raise
        except Exception as exc:
            logger.error("[ollama] chat() failed: %s", exc)
            raise

    async def stream_chat(
        self,
        messages: List[Dict[str, str]],
        system: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.3,
    ) -> AsyncGenerator[str, None]:
        """
        Streaming inference via Ollama /api/chat with stream=True.
        Yields token chunks as they arrive.

        Usage:
            async for chunk in client.stream_chat(messages, system=system_prompt):
                print(chunk, end='', flush=True)
        """
        use_model = model or self.default_model
        payload: Dict[str, Any] = {
            "model":   use_model,
            "messages": messages,
            "stream":  True,
            "options": {"temperature": temperature},
        }
        if system:
            payload["messages"] = [{"role": "system", "content": system}] + messages

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream("POST", f"{self.host}/api/chat", json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            data  = _json.loads(line)
                            chunk = data.get("message", {}).get("content", "")
                            if chunk:
                                yield chunk
                            if data.get("done"):
                                break
                        except _json.JSONDecodeError:
                            continue
        except Exception as exc:
            logger.error("[ollama] stream_chat() failed: %s", exc)
            raise

    # ── Model management ──────────────────────────────────────────────────────

    async def list_models(self) -> List[str]:
        """Return names of all models available in this Ollama instance."""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                resp = await client.get(f"{self.host}/api/tags")
                resp.raise_for_status()
                data = resp.json()
                return [m.get("name", "") for m in data.get("models", [])]
        except Exception as exc:
            logger.warning("[ollama] list_models() failed: %s", exc)
            return []

    async def pull_model(self, model_name: str) -> Dict[str, Any]:
        """
        Pull a model from the Ollama registry.
        Returns the final status response. This may take minutes for large models.
        """
        logger.info("[ollama] Pulling model: %s", model_name)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=1800.0, write=30.0, pool=5.0)) as client:
                resp = await client.post(
                    f"{self.host}/api/pull",
                    json={"name": model_name, "stream": False},
                )
                resp.raise_for_status()
                result = resp.json()
                logger.info("[ollama] Pull complete: %s -> %s", model_name, result.get("status"))
                return result
        except Exception as exc:
            logger.error("[ollama] pull_model(%s) failed: %s", model_name, exc)
            raise

    async def create_model(self, model_name: str, modelfile_content: str) -> Dict[str, Any]:
        """
        Create a custom Ollama model from a Modelfile string.
        Used by entrypoint.sh to register the cvg-neuron identity model.
        """
        logger.info("[ollama] Creating model: %s", model_name)
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=5.0)) as client:
                resp = await client.post(
                    f"{self.host}/api/create",
                    json={"name": model_name, "modelfile": modelfile_content, "stream": False},
                )
                resp.raise_for_status()
                result = resp.json()
                logger.info("[ollama] Model created: %s -> %s", model_name, result.get("status"))
                return result
        except Exception as exc:
            logger.error("[ollama] create_model(%s) failed: %s", model_name, exc)
            raise

    async def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        system: Optional[str] = None,
        temperature: float = 0.3,
        num_predict: int = 1024,
    ) -> str:
        """
        Single-shot generation via Ollama /api/generate (non-chat format).
        Use chat() for multi-turn conversations.
        """
        use_model = model or self.default_model
        payload: Dict[str, Any] = {
            "model":  use_model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": num_predict},
        }
        if system:
            payload["system"] = system

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(f"{self.host}/api/generate", json=payload)
                resp.raise_for_status()
                return resp.json().get("response", "")
        except Exception as exc:
            logger.error("[ollama] generate() failed: %s", exc)
            raise


# ── Module-level singleton ────────────────────────────────────────────────────

_ollama_client: Optional[OllamaClient] = None


def get_ollama_client(host: str = OLLAMA_HOST, model: str = DEFAULT_MODEL) -> OllamaClient:
    """Return the global OllamaClient singleton (lazy-initialized)."""
    global _ollama_client
    if _ollama_client is None:
        _ollama_client = OllamaClient(host=host, model=model)
    return _ollama_client
