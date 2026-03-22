"""
CVG Neuron — Hive Cluster Manager
(c) Clearview Geographic LLC — Proprietary

Discovers, monitors, and intelligently routes inference requests across
all Hive-0 nodes:
  - Queens:   Proxmox hypervisors + NAS nodes (QUEEN-11, 12, 13, Q10)
  - Forges:   Developer workstations with Docker/Ollama (DFORGE-100)
  - Compute:  Production VMs with Ollama available (VM-451, 454, 455, CT-104)
  - Edge:     Dynamically registered via blockchain tunnel

This is what makes CVG Neuron use the ENTIRE HIVE as a cluster computing
network — not just one Ollama instance. Any node in Hive-0 that has Ollama
running can be enlisted as compute for CVG Neuron.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import httpx

logger = logging.getLogger("cvg.neuron.hive")

# ─── Node Types ───────────────────────────────────────────────────────────────

class NodeType(str, Enum):
    QUEEN   = "queen"    # Proxmox / NAS / core infrastructure host
    FORGE   = "forge"    # Developer workstation / build node
    COMPUTE = "compute"  # Production VM or LXC with Ollama
    EDGE    = "edge"     # Remote node registered via blockchain tunnel


# ─── HiveNode ─────────────────────────────────────────────────────────────────

@dataclass
class HiveNode:
    """Represents a single node in the CVG Hive-0 cluster."""
    node_id:      str
    node_type:    NodeType
    hostname:     str
    ip:           str
    description:  str
    ollama_port:  int  = 11434
    api_port:     Optional[int] = None
    ollama_url:   str  = ""

    # Runtime state (updated by probe)
    online:         bool  = False
    ollama_online:  bool  = False
    latency_ms:     float = 0.0
    models:         list  = field(default_factory=list)
    last_seen:      float = 0.0
    last_probed:    float = 0.0
    capabilities:   dict  = field(default_factory=dict)
    error:          str   = ""

    def __post_init__(self):
        if not self.ollama_url:
            self.ollama_url = f"http://{self.ip}:{self.ollama_port}"

    def to_dict(self) -> dict:
        return {
            "node_id":      self.node_id,
            "node_type":    self.node_type.value,
            "hostname":     self.hostname,
            "ip":           self.ip,
            "description":  self.description,
            "ollama_url":   self.ollama_url,
            "online":       self.online,
            "ollama_online": self.ollama_online,
            "latency_ms":   self.latency_ms,
            "models":       self.models,
            "last_seen":    self.last_seen,
            "last_probed":  self.last_probed,
            "capabilities": self.capabilities,
            "error":        self.error,
        }


# ─── Static Hive-0 Registry ───────────────────────────────────────────────────
# These are the known persistent nodes of Hive-0.
# Edge nodes register dynamically at runtime via the blockchain tunnel.

HIVE_REGISTRY: list[HiveNode] = [
    # ── Queens ──────────────────────────────────────────────────────────────
    HiveNode(
        node_id="queen-11",
        node_type=NodeType.QUEEN,
        hostname="CVG-QUEEN-11",
        ip="10.10.10.56",
        description="Proxmox VE 8.3 hypervisor — Dell PowerEdge R820 — 136+ cores, 1.75 TB RAM. "
                    "Primary Hive-0 hypervisor. Hosts all CVG VMs and LXC containers.",
        ollama_port=11434,
        capabilities={"proxmox": True, "vcpus": 136, "ram_tb": 1.75, "role": "hypervisor"},
    ),
    HiveNode(
        node_id="queen-12",
        node_type=NodeType.QUEEN,
        hostname="CVG-QUEEN-12",
        ip="10.10.10.53",
        description="Synology DSM NAS — primary G: drive (CGDP). Stores all active project data and source code.",
        ollama_port=11434,
        capabilities={"nas": True, "storage": "CGDP", "smb": True},
    ),
    HiveNode(
        node_id="q10-truenas",
        node_type=NodeType.QUEEN,
        hostname="CVG-Q10-TrueNAS",
        ip="10.10.10.100",
        description="TrueNAS NAS — archive Z:/T: drives (CGPS). Archives 2018–2024 CVG project data.",
        ollama_port=11434,
        capabilities={"nas": True, "storage": "CGPS"},
    ),
    HiveNode(
        node_id="queen-13",
        node_type=NodeType.QUEEN,
        hostname="CVG-QUEEN-13",
        ip="192.168.50.187",
        description="Legacy TerraStation NAS — secondary network segment (192.168.50.x).",
        ollama_port=11434,
        capabilities={"nas": True, "legacy": True},
    ),
    # ── Compute VMs / LXC ────────────────────────────────────────────────────
    HiveNode(
        node_id="vm-451",
        node_type=NodeType.COMPUTE,
        hostname="cvg-stormsurge-01",
        ip="10.10.10.200",
        description="Primary Docker production host — 8 vCPUs, 16 GB RAM. "
                    "Runs all CVG microservices including CVG Neuron itself.",
        ollama_port=11434,
        api_port=8091,
        capabilities={"docker": True, "primary": True, "all_services": True},
    ),
    HiveNode(
        node_id="vm-454",
        node_type=NodeType.COMPUTE,
        hostname="cvg-geoserver-raster-01",
        ip="10.10.10.203",
        description="GeoServer Raster VM — WMS/WCS raster layers (DEM, imagery, inundation).",
        ollama_port=11434,
        capabilities={"geoserver": True, "raster": True},
    ),
    HiveNode(
        node_id="vm-455",
        node_type=NodeType.COMPUTE,
        hostname="cvg-geoserver-vector-01",
        ip="10.10.10.204",
        description="GeoServer Vector VM — WMS/WFS vector layers (parcels, flood zones, infrastructure).",
        ollama_port=11434,
        capabilities={"geoserver": True, "vector": True},
    ),
    HiveNode(
        node_id="ct-104",
        node_type=NodeType.COMPUTE,
        hostname="hive0-web-ct",
        ip="10.10.10.75",
        description="LXC container — nginx, PHP, MariaDB, BIND9 (ns1.cvg-nexus.com). "
                    "Internal DNS resolver and web container.",
        ollama_port=11434,
        capabilities={"dns": True, "web": True, "bind9": True},
    ),
    # ── Forges ───────────────────────────────────────────────────────────────
    HiveNode(
        node_id="dforge-100",
        node_type=NodeType.FORGE,
        hostname="DFORGE-100",
        ip="10.10.10.59",
        description="Primary developer workstation — Docker Desktop + WSL2 + Windows + Ollama. "
                    "Cline/Claude development hub. Primary Neuron development forge.",
        ollama_port=11434,
        capabilities={"docker_desktop": True, "wsl2": True, "forge": True, "cline": True},
    ),
    HiveNode(
        node_id="znet-media",
        node_type=NodeType.QUEEN,
        hostname="ZNET-MEDIA",
        ip="192.168.50.186",
        description="Legacy server — Apache :8808, SMB shares. Secondary network segment.",
        ollama_port=11434,
        capabilities={"legacy": True, "apache": True},
    ),
]

# ─── Dynamic Edge Nodes (registered via blockchain tunnel at runtime) ─────────
_edge_nodes: list[HiveNode] = []

# ─── Probe Configuration ─────────────────────────────────────────────────────
PROBE_TIMEOUT   = httpx.Timeout(connect=2.5, read=4.0, write=2.5, pool=2.5)
CACHE_TTL_SEC   = 120  # Re-probe after 2 minutes of staleness
_last_full_probe: float = 0.0


# ─── Node Probing ─────────────────────────────────────────────────────────────

async def _probe_node(client: httpx.AsyncClient, node: HiveNode) -> HiveNode:
    """Probe a single hive node: check Ollama /api/tags and collect model list."""
    t0 = time.monotonic()
    node.last_probed = time.time()
    try:
        resp = await client.get(f"{node.ollama_url}/api/tags", timeout=PROBE_TIMEOUT)
        elapsed_ms = (time.monotonic() - t0) * 1000
        if resp.status_code == 200:
            data = resp.json()
            node.models       = [m["name"] for m in data.get("models", [])]
            node.online       = True
            node.ollama_online = True
            node.latency_ms   = round(elapsed_ms, 1)
            node.last_seen    = time.time()
            node.error        = ""
            logger.debug(
                "[hive] %-20s (%s) ONLINE — %d models, %.0fms",
                node.node_id, node.ip, len(node.models), elapsed_ms,
            )
        else:
            node.online       = False
            node.ollama_online = False
            node.error        = f"HTTP {resp.status_code}"
    except httpx.ConnectError:
        node.online       = False
        node.ollama_online = False
        node.error        = "connection refused"
    except httpx.TimeoutException:
        node.online       = False
        node.ollama_online = False
        node.error        = "timeout"
    except Exception as exc:
        node.online       = False
        node.ollama_online = False
        node.error        = str(exc)[:80]
    return node


async def probe_all_nodes(force: bool = False) -> list[HiveNode]:
    """
    Probe all hive nodes concurrently.
    Uses cached results unless force=True or cache is stale.
    """
    global _last_full_probe
    now = time.time()
    if not force and (now - _last_full_probe) < CACHE_TTL_SEC:
        # Return current state without re-probing
        return HIVE_REGISTRY + _edge_nodes

    logger.info("[hive] Probing %d hive nodes...", len(HIVE_REGISTRY) + len(_edge_nodes))
    all_nodes = HIVE_REGISTRY + _edge_nodes
    async with httpx.AsyncClient() as client:
        tasks   = [_probe_node(client, node) for node in all_nodes]
        results = await asyncio.gather(*tasks, return_exceptions=False)

    online_count = sum(1 for n in results if n.ollama_online)
    _last_full_probe = now
    logger.info(
        "[hive] Probe complete — %d/%d nodes have Ollama",
        online_count, len(results),
    )
    return results


# ─── Compute Routing ──────────────────────────────────────────────────────────

async def get_compute_nodes(model_hint: Optional[str] = None) -> list[HiveNode]:
    """
    Return all online Ollama nodes sorted by latency.
    If model_hint is given, prefer nodes that already have that model loaded.
    """
    nodes  = await probe_all_nodes()
    online = [n for n in nodes if n.ollama_online]

    if model_hint:
        # Split off tag: "llama3.1:8b" → base "llama3.1"
        base = model_hint.split(":")[0].lower()
        with_model = [n for n in online if any(base in m.lower() for m in n.models)]
        if with_model:
            return sorted(with_model, key=lambda n: (n.latency_ms or 9999))

    return sorted(online, key=lambda n: (n.latency_ms or 9999))


async def get_best_ollama_url(model_hint: Optional[str] = None) -> str:
    """
    Return the Ollama URL of the best available hive node for inference.
    Falls back to the configured OLLAMA_URL env var if no nodes are reachable.
    """
    fallback = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434")
    try:
        nodes = await get_compute_nodes(model_hint)
        if nodes:
            best = nodes[0]
            logger.info(
                "[hive] Routing to %s (%s) — %.0fms latency",
                best.node_id, best.ollama_url, best.latency_ms,
            )
            return best.ollama_url
    except Exception as exc:
        logger.warning("[hive] Node routing failed, using fallback: %s", exc)
    return fallback


# ─── Edge Node Registration ───────────────────────────────────────────────────

def register_edge_node(
    node_id:      str,
    ip:           str,
    description:  str = "Edge node",
    ollama_port:  int = 11434,
    capabilities: Optional[dict] = None,
) -> HiveNode:
    """
    Register a dynamic edge node with the hive.
    Called automatically by the blockchain tunnel when an edge connector registers.
    """
    global _edge_nodes
    node = HiveNode(
        node_id     = node_id,
        node_type   = NodeType.EDGE,
        hostname    = node_id,
        ip          = ip,
        description = description,
        ollama_port = ollama_port,
        capabilities = capabilities or {},
    )
    # Deregister old entry if present
    _edge_nodes = [n for n in _edge_nodes if n.node_id != node_id]
    _edge_nodes.append(node)
    logger.info("[hive] Edge node registered: %s @ %s:%d", node_id, ip, ollama_port)
    return node


def deregister_edge_node(node_id: str) -> bool:
    global _edge_nodes
    before = len(_edge_nodes)
    _edge_nodes = [n for n in _edge_nodes if n.node_id != node_id]
    return len(_edge_nodes) < before


# ─── Topology Export ──────────────────────────────────────────────────────────

def get_hive_topology() -> dict:
    """Return the full hive topology as a serializable dict (uses last-known state)."""
    all_nodes = HIVE_REGISTRY + _edge_nodes
    by_type: dict[str, list] = {}
    for node in all_nodes:
        t = node.node_type.value
        by_type.setdefault(t, []).append(node.to_dict())

    online     = sum(1 for n in all_nodes if n.online)
    ollama_cnt = sum(1 for n in all_nodes if n.ollama_online)
    all_models = set()
    for n in all_nodes:
        all_models.update(n.models)

    return {
        "total_nodes":     len(all_nodes),
        "online_nodes":    online,
        "ollama_nodes":    ollama_cnt,
        "available_models": sorted(all_models),
        "last_probed":     _last_full_probe,
        "by_type":         by_type,
    }


async def get_hive_topology_live() -> dict:
    """Force-probe all nodes and return fresh topology."""
    await probe_all_nodes(force=True)
    return get_hive_topology()
