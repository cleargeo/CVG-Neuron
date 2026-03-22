"""
CVG Neuron AI Orchestration System — CVG Hive Service
Version: 2.0.0 | Clearview Geographic LLC

Client for the CVG Hive distributed compute cluster.
Manages job submission, node health, and task coordination.

CVG Hive Nodes:
  HIVE-0 (primary): 192.168.100.38 — 16 cores, 64GB RAM, GPU
  HIVE-1 (worker):  192.168.100.39 — 8 cores, 32GB RAM
  HIVE-2 (worker):  192.168.100.40 — 8 cores, 32GB RAM
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.logger import get_logger

log = get_logger("hive-service")


class HiveService:
    """
    CVG Hive distributed compute integration.

    Provides:
    - Job submission and monitoring
    - Node health checking
    - Load balancing hints
    - Task coordination with NeuronOrchestrator
    """

    HIVE_NODES = [
        {"id": "hive-0", "ip": settings.hive_node_0, "role": "primary", "gpu": True},
        {"id": "hive-1", "ip": settings.hive_node_1, "role": "worker",  "gpu": False},
        {"id": "hive-2", "ip": settings.hive_node_2, "role": "worker",  "gpu": False},
    ]

    def __init__(self) -> None:
        self._base_url = settings.hive_endpoint.rsplit("/api/", 1)[0]
        self._timeout = settings.hive_timeout
        self._online_nodes: List[str] = []

    # ── Health ────────────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Ping the primary Hive node. Returns True if reachable."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"http://{settings.hive_node_0}:8808/api/hive/health")
                return resp.status_code < 500
        except Exception as exc:
            log.debug("Hive health check failed", error=str(exc))
            return False

    async def get_online_nodes(self) -> List[str]:
        """Return IDs of currently reachable Hive nodes."""
        results = await asyncio.gather(
            *[self._ping_node(node) for node in self.HIVE_NODES],
            return_exceptions=True,
        )
        online = [
            self.HIVE_NODES[i]["id"]
            for i, r in enumerate(results)
            if r is True
        ]
        self._online_nodes = online
        return online

    async def _ping_node(self, node: Dict[str, Any]) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(f"http://{node['ip']}:8808/api/health")
                return resp.status_code < 500
        except Exception:
            return False

    # ── Job Submission ────────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
    async def submit_job(
        self,
        task_id: str,
        task_type: str,
        payload: Dict[str, Any],
        strategy: str = "round_robin",
        preferred_node: Optional[str] = None,
        gpu_required: bool = False,
    ) -> Dict[str, Any]:
        """
        Submit a compute job to the CVG Hive.

        Args:
            task_id: Neuron task ID
            task_type: Hive task type (e.g. ai_inference, geometry_processing)
            payload: Task data
            strategy: Distribution strategy (round_robin, gpu_priority, etc.)
            preferred_node: Preferred hive node ID
            gpu_required: Whether GPU is required

        Returns:
            Hive job response dict
        """
        job = {
            "task_id": task_id,
            "task_type": task_type,
            "payload": payload,
            "strategy": strategy,
            "gpu_required": gpu_required,
            "preferred_node": preferred_node or ("hive-0" if gpu_required else None),
            "neuron_id": settings.neuron_id,
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    settings.hive_endpoint,
                    json=job,
                    headers={"X-Neuron-ID": settings.neuron_id},
                )
                resp.raise_for_status()
                result = resp.json()
                log.info(
                    "Hive job submitted",
                    task_id=task_id,
                    task_type=task_type,
                    job_id=result.get("job_id"),
                )
                return result
        except httpx.HTTPError as exc:
            log.error("Hive job submission failed", task_id=task_id, error=str(exc))
            # Return a degraded response so Neuron can continue locally
            return {
                "job_id": f"local-{task_id}",
                "status": "local_fallback",
                "node": "local",
                "message": f"Hive unavailable: {exc}",
            }

    async def get_job_status(self, job_id: str) -> Dict[str, Any]:
        """Check the status of a submitted Hive job."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    f"http://{settings.hive_node_0}:8808/api/hive/jobs/{job_id}",
                    headers={"X-Neuron-ID": settings.neuron_id},
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            log.warning("Hive job status check failed", job_id=job_id, error=str(exc))
            return {"job_id": job_id, "status": "unknown", "error": str(exc)}

    async def get_node_metrics(self) -> List[Dict[str, Any]]:
        """Fetch resource metrics from all Hive nodes."""
        results = await asyncio.gather(
            *[self._get_node_metrics(node) for node in self.HIVE_NODES],
            return_exceptions=True,
        )
        return [r for r in results if isinstance(r, dict)]

    async def _get_node_metrics(self, node: Dict[str, Any]) -> Dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"http://{node['ip']}:8808/api/hive/metrics")
                if resp.status_code == 200:
                    data = resp.json()
                    data["node_id"] = node["id"]
                    return data
        except Exception:
            pass
        return {"node_id": node["id"], "status": "offline"}
