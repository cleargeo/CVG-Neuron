"""
CVG Neuron — Live Service Integrations
Polls every CVG microservice and aggregates real-time infrastructure state
to feed into Neuron's intelligence engine.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

from cvg_neuron import knowledge, memory

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

TIMEOUT         = float(os.environ.get("CVG_POLL_TIMEOUT", "5"))
# Support both old env var names (CONTAINERIZATION_ENGINE_URL) and new (SUPPORT_ENGINE_URL)
SUPPORT_URL     = (os.environ.get("SUPPORT_ENGINE_URL")
                   or os.environ.get("CONTAINERIZATION_ENGINE_URL")
                   or "http://cvg-support-engine:8091")
GIT_ENGINE_URL  = os.environ.get("GIT_ENGINE_URL",      "http://cvg-git-engine:8092")
DNS_ENGINE_URL  = os.environ.get("DNS_ENGINE_URL",      "http://cvg-dns-engine:8094")
# Support both old env var name (AUDIT_VM_URL) and new (AUDIT_ENGINE_URL)
AUDIT_URL       = (os.environ.get("AUDIT_ENGINE_URL")
                   or os.environ.get("AUDIT_VM_URL")
                   or "http://cvg-audit-results-api:8001")
# Support both old API key name (CVG_INTERNAL_KEY) and new (CVG_API_KEY)
API_KEY         = (os.environ.get("CVG_API_KEY")
                   or os.environ.get("CVG_INTERNAL_KEY")
                   or "cvg-internal-2026")

_HEADERS = {"X-CVG-API-Key": API_KEY}

# ---------------------------------------------------------------------------
# Internal health poll table
# ---------------------------------------------------------------------------

_HEALTH_ENDPOINTS: list[dict] = [
    {"id": "cvg-slr",            "url": "http://10.10.10.200:8001/health",           "label": "SLR Wizard"},
    {"id": "cvg-rainfall",       "url": "http://10.10.10.200:8002/health",           "label": "Rainfall Wizard"},
    {"id": "cvg-storm-surge",    "url": "http://ssw-api:8080/health",                "label": "Storm Surge Wizard"},
    {"id": "cvg-support-engine", "url": f"{SUPPORT_URL}/health",                     "label": "Support Engine"},
    {"id": "cvg-git-engine",     "url": f"{GIT_ENGINE_URL}/health",                  "label": "Git Engine"},
    {"id": "cvg-dns-engine",     "url": f"{DNS_ENGINE_URL}/api/health",               "label": "DNS Engine"},
    {"id": "cvg-hive",           "url": "http://10.10.10.200:8081/health",           "label": "Hive"},
    {"id": "cvg-audit-engine",   "url": f"{AUDIT_URL}/api/health",                   "label": "Audit Engine"},
    {"id": "cvg-gitea",          "url": "http://10.10.10.200:3000/api/healthz",      "label": "Gitea SCM"},
    {"id": "cvg-grafana",        "url": "http://10.10.10.200:3100/api/health",       "label": "Grafana"},
    {"id": "cvg-prometheus",     "url": "http://10.10.10.200:9090/-/healthy",        "label": "Prometheus"},
    {"id": "cvg-geoserver-raster","url": "http://10.10.10.203:8080/geoserver/web/", "label": "GeoServer Raster"},
    {"id": "cvg-geoserver-vector","url": "http://10.10.10.204:8080/geoserver/web/", "label": "GeoServer Vector"},
]

# ---------------------------------------------------------------------------
# Async poll helpers
# ---------------------------------------------------------------------------

async def _poll_one(client: httpx.AsyncClient, endpoint: dict) -> dict:
    t0 = time.time()
    try:
        r = await client.get(endpoint["url"], timeout=TIMEOUT)
        elapsed_ms = round((time.time() - t0) * 1000)
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:200]}
        return {
            "id":         endpoint["id"],
            "label":      endpoint["label"],
            "healthy":    r.status_code < 400,
            "status_code": r.status_code,
            "response_ms": elapsed_ms,
            "body":       body,
        }
    except Exception as exc:
        return {
            "id":       endpoint["id"],
            "label":    endpoint["label"],
            "healthy":  False,
            "error":    str(exc)[:120],
            "response_ms": round((time.time() - t0) * 1000),
        }


async def poll_all_services() -> dict[str, Any]:
    """Poll every CVG service health endpoint concurrently."""
    async with httpx.AsyncClient(headers=_HEADERS, follow_redirects=True) as client:
        tasks = [_poll_one(client, ep) for ep in _HEALTH_ENDPOINTS]
        results = await asyncio.gather(*tasks, return_exceptions=False)

    output: dict[str, Any] = {}
    for r in results:
        sid = r.pop("id")
        output[sid] = r

    # Record observations for any new failures
    for sid, info in output.items():
        if not info.get("healthy"):
            memory.record_observation(
                category="infrastructure",
                subject=sid,
                detail=info.get("error", f"HTTP {info.get('status_code','?')} — service unhealthy"),
                severity="warning",
                source="neuron-poll",
            )

    return output


def poll_all_services_sync() -> dict[str, Any]:
    """Synchronous wrapper for poll_all_services."""
    return asyncio.run(poll_all_services())


# ---------------------------------------------------------------------------
# Support Engine — node telemetry
# ---------------------------------------------------------------------------

async def fetch_node_telemetry() -> dict:
    async with httpx.AsyncClient(headers=_HEADERS) as client:
        try:
            r = await client.get(f"{SUPPORT_URL}/api/telemetry", timeout=10)
            return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"error": str(e)}


async def fetch_infrastructure_nodes() -> dict:
    async with httpx.AsyncClient(headers=_HEADERS) as client:
        try:
            r = await client.get(f"{SUPPORT_URL}/api/nodes", timeout=10)
            return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"error": str(e)}


# ---------------------------------------------------------------------------
# Git Engine — version snapshot
# ---------------------------------------------------------------------------

async def fetch_service_versions() -> dict:
    async with httpx.AsyncClient(headers=_HEADERS) as client:
        try:
            r = await client.get(f"{GIT_ENGINE_URL}/api/versions", timeout=10)
            return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"error": str(e)}


# ---------------------------------------------------------------------------
# DNS Engine — zone status
# ---------------------------------------------------------------------------

async def fetch_dns_status() -> dict:
    async with httpx.AsyncClient(headers=_HEADERS) as client:
        try:
            r = await client.get(f"{DNS_ENGINE_URL}/api/status", timeout=10)
            return r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
        except Exception as e:
            return {"error": str(e)}


# ---------------------------------------------------------------------------
# Full aggregate context (used for intelligence queries)
# ---------------------------------------------------------------------------

async def build_live_context() -> dict:
    """Poll everything in parallel and return a unified live context dict."""
    health_task   = asyncio.create_task(poll_all_services())
    telemetry_task = asyncio.create_task(fetch_node_telemetry())
    versions_task = asyncio.create_task(fetch_service_versions())
    dns_task      = asyncio.create_task(fetch_dns_status())

    health, telemetry, versions, dns = await asyncio.gather(
        health_task, telemetry_task, versions_task, dns_task,
        return_exceptions=True,
    )

    def _safe(v):
        return v if not isinstance(v, Exception) else {"error": str(v)}

    return {
        "health":    _safe(health),
        "telemetry": _safe(telemetry),
        "versions":  _safe(versions),
        "dns":       _safe(dns),
        "polled_at": time.time(),
    }


def build_live_context_sync() -> dict:
    return asyncio.run(build_live_context())


# ---------------------------------------------------------------------------
# Context summary string (for LLM injection)
# ---------------------------------------------------------------------------

def summarize_live_context(ctx: dict) -> str:
    """Produce a human-readable summary of live context for LLM injection."""
    lines = ["=== LIVE CVG INFRASTRUCTURE STATE ==="]

    health = ctx.get("health", {})
    if isinstance(health, dict) and not health.get("error"):
        up   = [v["label"] for v in health.values() if v.get("healthy")]
        down = [v["label"] for v in health.values() if not v.get("healthy")]
        lines.append(f"HEALTHY ({len(up)}): {', '.join(up[:10])}")
        if down:
            lines.append(f"UNHEALTHY ({len(down)}): {', '.join(down)}")

    versions = ctx.get("versions", {})
    if isinstance(versions, dict) and not versions.get("error"):
        lines.append("DEPLOYED VERSIONS:")
        for svc, info in (versions.get("services") or versions).items():
            if isinstance(info, dict):
                v = info.get("version") or info.get("tag") or "?"
                lines.append(f"  {svc}: {v}")

    dns = ctx.get("dns", {})
    if isinstance(dns, list):
        dns_up   = [s["name"] for s in dns if s.get("up")]
        dns_down = [s["name"] for s in dns if not s.get("up")]
        if dns_down:
            lines.append(f"DNS UNREACHABLE: {', '.join(dns_down)}")
        else:
            lines.append(f"DNS OK: {', '.join(dns_up[:4])}")

    return "\n".join(lines)
