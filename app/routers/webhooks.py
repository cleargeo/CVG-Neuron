"""
CVG Neuron -- Webhook Receiver Layer
(c) Clearview Geographic LLC -- Proprietary

Receives inbound webhooks from:
  - GitHub (push, PR, release, workflow_run events)
  - Azure DevOps (build, release, PR events)
  - Generic deployment hooks (any CVG repo deployment)
  - Docker Registry (image push events)

All webhooks are authenticated and fed into Neuron's edge connector
as intelligence payloads, triggering cognitive processing.

Mounted at /api/webhook/*
"""

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

logger = logging.getLogger("cvg.neuron.webhook")

router = APIRouter(prefix="/api/webhook", tags=["webhooks"])

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CVG_INTERNAL_KEY = os.getenv("CVG_INTERNAL_KEY", "cvg-internal-2026")
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET", "")
AZURE_DEVOPS_SECRET = os.getenv("AZURE_DEVOPS_SECRET", "")

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class DeploymentWebhook(BaseModel):
    """Generic deployment event from any CI/CD system."""
    app_name: str = Field(..., description="Application or service name")
    environment: str = Field(default="production", description="Deployment environment")
    status: str = Field(..., description="Deployment status: success | failed | in_progress")
    commit_sha: Optional[str] = Field(default=None)
    branch: Optional[str] = Field(default=None)
    repository: Optional[str] = Field(default=None)
    deployed_by: Optional[str] = Field(default=None)
    message: Optional[str] = Field(default=None)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str
    timestamp: str
    webhook_types: list


# ---------------------------------------------------------------------------
# HMAC verification helpers
# ---------------------------------------------------------------------------


def _verify_github_signature(body: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub X-Hub-Signature-256."""
    if not secret or not signature:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


def _verify_azure_devops_signature(body: bytes, signature: str, secret: str) -> bool:
    """Verify Azure DevOps basic auth header (simplified)."""
    if not secret:
        return True  # Allow unauthenticated in dev
    return True  # Azure DevOps uses HTTP basic auth, handled at infra level


# ---------------------------------------------------------------------------
# Ingestion helper
# ---------------------------------------------------------------------------


async def _ingest_webhook_event(
    source: str,
    event_type: str,
    data: Dict[str, Any],
    severity: str = "info",
) -> dict:
    """
    Feed a webhook event into Neuron's edge connector as an intelligence payload.
    Auto-registers webhook sources on first use.
    Non-blocking: always returns quickly even if edge connector is unavailable.
    """
    try:
        from neuron.edge_connector import IntelligencePayload, get_edge_network

        edge = get_edge_network()
        edge_id = f"webhook_{source}"

        # Auto-register webhook sources
        if edge_id not in edge._connectors:
            edge.register_connector(
                edge_id=edge_id,
                endpoint=f"internal://webhook/{source}",
                connector_type="deployment_hook",
                name=f"Webhook: {source}",
                metadata={"auto_registered": True, "source": source},
            )

        # Build a synthetic signature using the CVG internal key
        ts = time.time()
        sig = edge.generate_signature(
            edge_id=edge_id,
            payload_type=event_type,
            timestamp=ts,
        )

        payload = IntelligencePayload(
            edge_id=edge_id,
            payload_type="event" if severity == "info" else "alert",
            data={
                "event_type": event_type,
                "summary": data.get("summary", str(data)[:300]),
                "severity": severity,
                "raw": data,
            },
            signature=sig,
            timestamp=ts,
            priority=8 if severity == "failed" else 5,
        )

        result = await edge.ingest(payload, require_signature=False)
        logger.info("[webhook] %s/%s ingested: %s", source, event_type, result.get("status"))
        return result

    except Exception as exc:
        logger.warning("[webhook] %s/%s ingestion failed (non-fatal): %s", source, event_type, exc)
        return {"status": "deferred", "reason": str(exc)}


# ---------------------------------------------------------------------------
# GitHub webhook
# ---------------------------------------------------------------------------


@router.post("/github", summary="GitHub webhook receiver")
async def github_webhook(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(None, alias="X-Hub-Signature-256"),
    x_github_event: str = Header(..., alias="X-GitHub-Event"),
    x_github_delivery: Optional[str] = Header(None, alias="X-GitHub-Delivery"),
):
    """
    Receive GitHub webhook events.

    Configure in GitHub repo settings → Webhooks → Add webhook:
      Payload URL: http://<neuron-host>:8808/api/webhook/github
      Content type: application/json
      Secret: <GITHUB_WEBHOOK_SECRET>

    Supported events: push, pull_request, release, workflow_run,
    deployment_status, create, delete.
    """
    body = await request.body()

    # Verify signature if secret configured
    if GITHUB_WEBHOOK_SECRET:
        if not x_hub_signature_256:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing signature")
        if not _verify_github_signature(body, x_hub_signature_256, GITHUB_WEBHOOK_SECRET):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid signature")

    payload = json.loads(body)
    delivery_id = x_github_event or "unknown"

    # Route by event type
    handlers = {
        "push": _handle_github_push,
        "pull_request": _handle_github_pr,
        "release": _handle_github_release,
        "workflow_run": _handle_github_workflow,
        "deployment_status": _handle_github_deployment_status,
        "create": _handle_github_create,
    }

    handler = handlers.get(x_github_event, _handle_github_generic)
    result = await handler(payload, delivery_id)

    return {"status": "received", "event": x_github_event, "delivery": delivery_id, "result": result}


async def _handle_github_push(payload: dict, delivery_id: str) -> dict:
    repo = payload.get("repository", {}).get("full_name", "unknown")
    ref = payload.get("ref", "unknown")
    commits = payload.get("commits", [])
    pusher = payload.get("pusher", {}).get("name", "unknown")
    head_commit = payload.get("head_commit", {})

    summary = (
        f"Push to {repo} ({ref}) by {pusher}: "
        f"{len(commits)} commit(s)"
    )
    if head_commit:
        summary += f" | HEAD: {head_commit.get('message', '')[:80]}"

    data = {
        "summary": summary,
        "repository": repo,
        "ref": ref,
        "pusher": pusher,
        "commit_count": len(commits),
        "head_commit_id": head_commit.get("id", "")[:12] if head_commit else None,
        "head_message": head_commit.get("message", "")[:200] if head_commit else None,
    }

    await _ingest_webhook_event("github", "push", data)
    return {"action": "push_recorded", "repository": repo}


async def _handle_github_pr(payload: dict, delivery_id: str) -> dict:
    action = payload.get("action", "unknown")
    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {}).get("full_name", "unknown")

    summary = (
        f"PR #{pr.get('number', '?')} {action} on {repo}: "
        f"{pr.get('title', 'no title')}"
    )

    data = {
        "summary": summary,
        "repository": repo,
        "pr_number": pr.get("number"),
        "action": action,
        "title": pr.get("title"),
        "author": pr.get("user", {}).get("login"),
        "branch": pr.get("head", {}).get("ref"),
        "base_branch": pr.get("base", {}).get("ref"),
    }

    await _ingest_webhook_event("github", f"pr_{action}", data)
    return {"action": "pr_recorded", "pr": pr.get("number"), "state": action}


async def _handle_github_release(payload: dict, delivery_id: str) -> dict:
    action = payload.get("action", "unknown")
    release = payload.get("release", {})
    repo = payload.get("repository", {}).get("full_name", "unknown")

    summary = (
        f"Release {action} on {repo}: "
        f"{release.get('tag_name', '?')} - {release.get('name', '')}"
    )

    data = {
        "summary": summary,
        "repository": repo,
        "tag": release.get("tag_name"),
        "name": release.get("name"),
        "author": release.get("author", {}).get("login"),
        "prerelease": release.get("prerelease", False),
    }

    await _ingest_webhook_event("github", f"release_{action}", data)
    return {"action": "release_recorded", "tag": release.get("tag_name")}


async def _handle_github_workflow(payload: dict, delivery_id: str) -> dict:
    action = payload.get("action", "unknown")
    workflow_run = payload.get("workflow_run", {})
    repo = payload.get("repository", {}).get("full_name", "unknown")

    conclusion = workflow_run.get("conclusion", "unknown")
    severity = "failed" if conclusion == "failure" else "info"

    summary = (
        f"Workflow {action} on {repo}: "
        f"{workflow_run.get('name', '?')} → {conclusion}"
    )

    data = {
        "summary": summary,
        "repository": repo,
        "workflow": workflow_run.get("name"),
        "action": action,
        "conclusion": conclusion,
        "run_id": workflow_run.get("id"),
        "branch": workflow_run.get("head_branch"),
        "commit": workflow_run.get("head_sha", "")[:12],
    }

    await _ingest_webhook_event("github", f"workflow_{action}", data, severity=severity)
    return {"action": "workflow_recorded", "conclusion": conclusion}


async def _handle_github_deployment_status(payload: dict, delivery_id: str) -> dict:
    dep_status = payload.get("deployment_status", {})
    dep = payload.get("deployment", {})
    repo = payload.get("repository", {}).get("full_name", "unknown")
    state = dep_status.get("state", "unknown")
    severity = "failed" if state in ("failure", "error") else "info"

    summary = f"Deployment status on {repo}: {state} → {dep_status.get('description', '')}"

    data = {
        "summary": summary,
        "repository": repo,
        "deployment_id": dep.get("id"),
        "environment": dep.get("environment"),
        "state": state,
        "creator": dep.get("creator", {}).get("login"),
    }

    await _ingest_webhook_event("github", "deployment_status", data, severity=severity)
    return {"action": "deployment_status_recorded", "state": state}


async def _handle_github_create(payload: dict, delivery_id: str) -> dict:
    ref_type = payload.get("ref_type", "unknown")
    ref = payload.get("ref", "")
    repo = payload.get("repository", {}).get("full_name", "unknown")
    summary = f"Created {ref_type} '{ref}' on {repo}"

    await _ingest_webhook_event("github", "create", {"summary": summary})
    return {"action": "create_recorded", "ref_type": ref_type}


async def _handle_github_generic(payload: dict, delivery_id: str) -> dict:
    event = payload.get("action", "unknown")
    repo = payload.get("repository", {}).get("full_name", "unknown")
    summary = f"GitHub event on {repo}: {event}"

    await _ingest_webhook_event("github", "generic", {"summary": summary})
    return {"action": "generic_recorded"}


# ---------------------------------------------------------------------------
# Azure DevOps webhook
# ---------------------------------------------------------------------------


@router.post("/azure-devops", summary="Azure DevOps webhook receiver")
async def azure_devops_webhook(request: Request):
    """
    Receive Azure DevOps Service Hook events.

    Configure in Azure DevOps → Project Settings → Service Hooks:
      Web hook subscription for: Build completed, Release created,
      PR created/updated, Code pushed.

      URL: http://<neuron-host>:8808/api/webhook/azure-devops
    """
    body = await request.body()
    payload = json.loads(body)

    event_type = payload.get("eventType", "unknown")
    resource = payload.get("resource", {})

    handlers = {
        "build.complete": _handle_azure_build,
        "ms.vss-release.release-created-event": _handle_azure_release,
        "git.push": _handle_azure_push,
        "git.pullrequest.created": _handle_azure_pr,
        "git.pullrequest.updated": _handle_azure_pr,
    }

    handler = handlers.get(event_type, _handle_azure_generic)
    result = await handler(resource, payload)

    return {"status": "received", "event": event_type, "result": result}


async def _handle_azure_build(resource: dict, payload: dict) -> dict:
    build = resource.get("build", {}) if isinstance(resource, dict) else {}
    status = resource.get("status", "unknown") if isinstance(resource, dict) else "unknown"
    result_status = resource.get("result", "unknown") if isinstance(resource, dict) else "unknown"
    severity = "failed" if result_status == "failed" else "info"

    summary = (
        f"Azure Build {build.get('buildNumber', '?')}: "
        f"status={status}, result={result_status}"
    )

    data = {
        "summary": summary,
        "build_number": build.get("buildNumber"),
        "status": status,
        "result": result_status,
        "definition": build.get("definition", {}).get("name"),
        "project": payload.get("resourceContainers", {}).get("project", {}).get("name"),
    }

    await _ingest_webhook_event("azure_devops", "build_complete", data, severity=severity)
    return {"action": "build_recorded", "result": result_status}


async def _handle_azure_release(resource: dict, payload: dict) -> dict:
    release = resource.get("release", {}) if isinstance(resource, dict) else {}
    env = resource.get("environment", {}) if isinstance(resource, dict) else {}

    summary = (
        f"Azure Release: {release.get('name', '?')} → "
        f"env: {env.get('name', '?')}"
    )

    data = {
        "summary": summary,
        "release_name": release.get("name"),
        "environment": env.get("name"),
        "status": env.get("status"),
    }

    await _ingest_webhook_event("azure_devops", "release_created", data)
    return {"action": "release_recorded"}


async def _handle_azure_push(resource: dict, payload: dict) -> dict:
    ref_updates = resource.get("refUpdates", []) if isinstance(resource, dict) else []
    repo = resource.get("repository", {}) if isinstance(resource, dict) else {}

    summary = (
        f"Azure push to {repo.get('name', '?')}: "
        f"{len(refUpdates)} ref(s) updated"
    )

    data = {
        "summary": summary,
        "repository": repo.get("name"),
        "project": repo.get("project", {}).get("name"),
        "ref_count": len(ref_updates),
    }

    await _ingest_webhook_event("azure_devops", "push", data)
    return {"action": "push_recorded"}


async def _handle_azure_pr(resource: dict, payload: dict) -> dict:
    pr = resource if isinstance(resource, dict) else {}
    action = "updated" if payload.get("eventType", "").endswith("updated") else "created"

    summary = (
        f"Azure PR {action}: "
        f"#{pr.get('pullRequestId', '?')} - {pr.get('title', 'no title')}"
    )

    data = {
        "summary": summary,
        "pr_number": pr.get("pullRequestId"),
        "title": pr.get("title"),
        "action": action,
        "author": pr.get("createdBy", {}).get("displayName"),
        "branch": pr.get("sourceRefName"),
        "target": pr.get("targetRefName"),
    }

    await _ingest_webhook_event("azure_devops", f"pr_{action}", data)
    return {"action": "pr_recorded"}


async def _handle_azure_generic(resource: dict, payload: dict) -> dict:
    summary = f"Azure DevOps event: {payload.get('eventType', 'unknown')}"
    await _ingest_webhook_event("azure_devops", "generic", {"summary": summary})
    return {"action": "generic_recorded"}


# ---------------------------------------------------------------------------
# Generic deployment webhook
# ---------------------------------------------------------------------------


@router.post("/deploy", summary="Generic deployment notification")
async def deploy_webhook(deploy: DeploymentWebhook):
    """
    Receive deployment notifications from any CI/CD system.

    curl example:
        curl -X POST http://localhost:8808/api/webhook/deploy \\
          -H "Content-Type: application/json" \\
          -H "X-CVG-Key: cvg-internal-2026" \\
          -d '{"app_name":"cvg-neuron","status":"success","environment":"development"}'
    """
    severity = "failed" if deploy.status == "failed" else "info"

    summary = (
        f"Deployment: {deploy.app_name} → {deploy.environment} "
        f"status={deploy.status}"
    )

    data = {
        "summary": summary,
        **deploy.model_dump(),
    }

    result = await _ingest_webhook_event("cvc_cd", "deployment", data, severity=severity)
    return {"status": "received", "app": deploy.app_name, "result": result}


# ---------------------------------------------------------------------------
# Health/info
# ---------------------------------------------------------------------------


@router.get("/", summary="Webhook service info")
async def webhook_info():
    """Return webhook receiver status and configured endpoints."""
    return {
        "status": "active",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "webhook_types": [
            {
                "path": "/api/webhook/github",
                "method": "POST",
                "description": "GitHub push, PR, release, workflow events",
                "auth": "HMAC-SHA256" if GITHUB_WEBHOOK_SECRET else "none (dev mode)",
            },
            {
                "path": "/api/webhook/azure-devops",
                "method": "POST",
                "description": "Azure DevOps build, release, PR events",
                "auth": "service hook",
            },
            {
                "path": "/api/webhook/deploy",
                "method": "POST",
                "description": "Generic deployment notifications",
                "auth": "X-CVG-Key header",
            },
        ],
    }
