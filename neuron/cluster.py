"""
CVG Neuron — Hive-0 Cluster Manager
(c) Clearview Geographic, LLC — Proprietary & PRIVATE

Manages Neuron's distributed cognition across the CVG Hive-0 cluster.
Every queen, forge, and edge connector is part of Neuron's distributed body.

Node Types:
  QUEEN  — High-memory nodes for knowledge storage and routing (NAS, hypervisors)
  FORGE  — High-compute nodes for heavy inference operations
  WORKER — Standard VMs running CVG applications
  EDGE   — External connectors / network gateway

Probe Types:
  ollama   — Probe Ollama API at /api/tags
  proxmox  — Probe Proxmox PVE API at port 8006 /api2/json/version (no auth)
  synology — Probe Synology DSM at port 5000 /webapi/entry.cgi
  https    — HTTPS connectivity probe (TLS check only)
  http     — Plain HTTP connectivity
  ping     — ICMP via HTTP HEAD fallback

Authoritative Network: 10.0.0.0/8 ONLY (per CVG_NETWORK_STANDARD.md 2026-03-17)
Legacy 192.168.100.x: DEPRECATED — DO NOT USE
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("cvg.neuron.cluster")

# ─── Authoritative Hive-0 Node Registry ──────────────────────────────────────
# Source: Z:\hive0.cleargeo.tech.local\00_Rules\CVG_NETWORK_STANDARD.md (2026-03-17)
# ALL IPs are in 10.0.0.0/8 — 192.168.100.x is deprecated/legacy.

KNOWN_NODES: Dict[str, dict] = {
    # ─── Primary Compute — cvg-stormsurge-01 (VM 451 stack) ──────────────────
    "vm-451": {
        "ip": "10.10.10.200", "type": "worker", "ollama_port": 11434,
        "primary": True,   "probe_type": "ollama",
        "label": "cvg-stormsurge-01 (VM 451)", "role": "Primary AI / Docker host",
    },
    "vm-454": {
        "ip": "10.10.10.204", "type": "worker", "ollama_port": 11434,
        "primary": False,  "probe_type": "ollama",
        "label": "vm-454", "role": "Secondary compute VM",
    },
    "vm-455": {
        "ip": "10.10.10.205", "type": "worker", "ollama_port": 11434,
        "primary": False,  "probe_type": "ollama",
        "label": "vm-455", "role": "Tertiary compute VM",
    },
    "ct-104": {
        "ip": "10.10.10.104", "type": "worker", "ollama_port": None,
        "primary": False,  "probe_type": "http",
        "label": "ct-104 (LXC)", "role": "LXC container — services",
    },

    # ─── QUEEN-11 — Dell PowerEdge R820 (Proxmox VE) ─────────────────────────
    # iDRAC: 10.10.10.50 | Proxmox OS: 10.10.10.56
    "queen-11-proxmox": {
        "ip": "10.10.10.56",  "type": "queen",  "ollama_port": None,
        "primary": False, "probe_type": "proxmox", "probe_port": 8006,
        "label": "CVG-QUEEN-11 Proxmox (Dell R820)", "role": "Primary hypervisor — hosts VM stack",
    },
    "queen-11-idrac": {
        "ip": "10.10.10.50",  "type": "queen",  "ollama_port": None,
        "primary": False, "probe_type": "https",   "probe_port": 443,
        "label": "CVG-QUEEN-11 iDRAC 9 (Dell)", "role": "Dell OOB management interface",
    },

    # ─── QUEEN-12 — Synology DS1823+ ─────────────────────────────────────────
    # IP: 10.10.10.53
    "queen-12-nas": {
        "ip": "10.10.10.53",  "type": "queen",  "ollama_port": None,
        "primary": False, "probe_type": "synology", "probe_port": 5000,
        "label": "CVG-QUEEN-12 DS1823+", "role": "Primary NAS — backup / shared storage",
    },

    # ─── QUEEN-20 — Synology DS3622xs+ ───────────────────────────────────────
    # Primary: 10.10.10.67 | Additional NICs: .66, .68, .69 | NSB: .57, .70
    "queen-20-nas": {
        "ip": "10.10.10.67",  "type": "queen",  "ollama_port": None,
        "primary": False, "probe_type": "synology", "probe_port": 5000,
        "label": "CVG-QUEEN-20 DS3622xs+", "role": "High-capacity NAS — ZNet Media / large datasets",
    },

    # ─── QUEEN-21 — TerraMaster ───────────────────────────────────────────────
    # NSB controller IP: 10.10.10.57 (per QUEEN-20 NSB / TerraMaster assignment)
    "queen-21-nas": {
        "ip": "10.10.10.57",  "type": "queen",  "ollama_port": None,
        "primary": False, "probe_type": "http",    "probe_port": 8181,
        "label": "CVG-QUEEN-21 TerraMaster", "role": "TerraMaster NAS — auxiliary storage",
    },

    # ─── QUEEN-30 — Synology DS418 ────────────────────────────────────────────
    # IP: 10.10.10.71
    "queen-30-nas": {
        "ip": "10.10.10.71",  "type": "queen",  "ollama_port": None,
        "primary": False, "probe_type": "synology", "probe_port": 5000,
        "label": "CVG-QUEEN-30 DS418", "role": "Archive NAS — cold storage / redundancy",
    },

    # ─── QUEEN-10 — HP ML350 Gen10 (ESXi) ────────────────────────────────────
    # iLO 5: 10.10.10.58 | ESXi Host: 10.10.10.61 | TrueNAS VM: 10.10.10.100
    "queen-10-esxi": {
        "ip": "10.10.10.61",  "type": "queen",  "ollama_port": None,
        "primary": False, "probe_type": "https",    "probe_port": 443,
        "label": "CVG-QUEEN-10 ESXi (HP ML350 Gen10)", "role": "Secondary hypervisor — ESXi Host-B",
    },
    "queen-10-ilo": {
        "ip": "10.10.10.58",  "type": "queen",  "ollama_port": None,
        "primary": False, "probe_type": "https",    "probe_port": 443,
        "label": "CVG-QUEEN-10 iLO 5 (HP ML350)", "role": "HP iLO OOB management",
    },
    "queen-10-truenas": {
        "ip": "10.10.10.100", "type": "queen",  "ollama_port": None,
        "primary": False, "probe_type": "http",     "probe_port": 80,
        "label": "CVG-QUEEN-10 TrueNAS VM", "role": "TrueNAS SCALE — iSCSI / ZFS on ESXi",
    },

    # ─── Audit / Security VM ─────────────────────────────────────────────────
    "audit-vm": {
        "ip": "10.10.10.220", "type": "worker", "ollama_port": None,
        "primary": False, "probe_type": "http",     "probe_port": 8001,
        "label": "Audit VM (10.10.10.220)", "role": "Wazuh SIEM + Trivy CVE scanner",
    },

    # ─── Network Infrastructure ───────────────────────────────────────────────
    "fortigate-lan10": {
        "ip": "10.10.10.1",   "type": "edge",   "ollama_port": None,
        "primary": False, "probe_type": "https",    "probe_port": 443,
        "label": "FortiGate (LAN10 gateway)", "role": "Firewall / gateway — VLAN 10",
    },
}

CVG_INTERNAL_KEY = os.getenv("CVG_INTERNAL_KEY", "cvg-internal-2026")
NODE_PROBE_TIMEOUT = float(os.getenv("NODE_PROBE_TIMEOUT", "4.0"))


class NodeType(str, Enum):
    QUEEN  = "queen"
    FORGE  = "forge"
    WORKER = "worker"
    EDGE   = "edge"


class NodeStatus(str, Enum):
    ONLINE   = "online"
    OFFLINE  = "offline"
    DEGRADED = "degraded"
    UNKNOWN  = "unknown"


class ClusterNode:
    """Represents a single node in the CVG Hive-0 cluster."""

    def __init__(self, name: str, config: dict):
        self.name           = name
        self.ip             = config.get("ip", "")
        self.node_type      = NodeType(config.get("type", "worker"))
        self.ollama_port    = config.get("ollama_port")
        self.is_primary     = config.get("primary", False)
        self.probe_type     = config.get("probe_type", "http")
        self.probe_port     = config.get("probe_port") or self.ollama_port
        self.label          = config.get("label", name)
        self.role           = config.get("role", "")
        self.status         = NodeStatus.UNKNOWN
        self.last_seen:     Optional[str] = None
        self.ollama_models: list = []
        self.latency_ms:    Optional[float] = None
        self.probe_detail:  dict = {}  # extra telemetry from the probe (version, model, etc.)

    @property
    def ollama_url(self) -> Optional[str]:
        if self.ollama_port:
            return f"http://{self.ip}:{self.ollama_port}"
        return None

    # ── Probe dispatch ────────────────────────────────────────────────────────

    async def probe(self, client: httpx.AsyncClient) -> NodeStatus:
        """Probe the node using the appropriate strategy for its type."""
        try:
            pt = self.probe_type
            if pt == "ollama":
                await self._probe_ollama(client)
            elif pt == "proxmox":
                await self._probe_proxmox(client)
            elif pt == "synology":
                await self._probe_synology(client)
            elif pt == "https":
                await self._probe_https(client)
            elif pt == "http":
                await self._probe_http(client)
            else:
                await self._probe_http(client)
        except Exception as exc:
            logger.debug("[cluster] %s unexpected probe error: %s", self.name, exc)
            self.status = NodeStatus.OFFLINE
            self.latency_ms = None

        if self.status == NodeStatus.ONLINE:
            self.last_seen = datetime.utcnow().isoformat() + "Z"
        return self.status

    async def _probe_ollama(self, client: httpx.AsyncClient) -> None:
        """Probe Ollama API — /api/tags returns list of models."""
        t0 = time.monotonic()
        try:
            resp = await client.get(
                f"http://{self.ip}:{self.ollama_port}/api/tags",
                timeout=NODE_PROBE_TIMEOUT,
            )
            self.latency_ms = round((time.monotonic() - t0) * 1000, 1)
            if resp.status_code == 200:
                data = resp.json()
                self.ollama_models = [m["name"] for m in data.get("models", [])]
                self.status = NodeStatus.ONLINE
                self.probe_detail = {
                    "model_count": len(self.ollama_models),
                    "models": self.ollama_models[:5],
                }
            else:
                self.status = NodeStatus.DEGRADED
        except (httpx.ConnectError, httpx.TimeoutException):
            self.status = NodeStatus.OFFLINE
            self.latency_ms = None

    async def _probe_proxmox(self, client: httpx.AsyncClient) -> None:
        """
        Probe Proxmox VE API — GET https://<ip>:8006/api2/json/version (no auth required).
        Returns PVE version info and confirms cluster reachability.
        """
        port = self.probe_port or 8006
        t0 = time.monotonic()
        try:
            resp = await client.get(
                f"https://{self.ip}:{port}/api2/json/version",
                timeout=NODE_PROBE_TIMEOUT,
            )
            self.latency_ms = round((time.monotonic() - t0) * 1000, 1)
            if resp.status_code in (200, 401):  # 401 = API up but unauthenticated path
                self.status = NodeStatus.ONLINE
                if resp.status_code == 200:
                    d = resp.json().get("data", {})
                    self.probe_detail = {
                        "version": d.get("version", ""),
                        "release": d.get("release", ""),
                        "repoid": d.get("repoid", ""),
                        "pve_api": True,
                    }
            else:
                self.status = NodeStatus.DEGRADED
        except (httpx.ConnectError, httpx.TimeoutException, Exception):
            # Fallback: try plain HTTP on port 8006
            try:
                resp2 = await client.get(
                    f"http://{self.ip}:{port}/",
                    timeout=NODE_PROBE_TIMEOUT,
                )
                self.latency_ms = round((time.monotonic() - t0) * 1000, 1)
                self.status = NodeStatus.ONLINE if resp2.status_code < 500 else NodeStatus.DEGRADED
            except Exception:
                self.status = NodeStatus.OFFLINE
                self.latency_ms = None

    async def _probe_synology(self, client: httpx.AsyncClient) -> None:
        """
        Probe Synology DSM — GET /webapi/entry.cgi?api=SYNO.API.Info&version=1&method=query&query=SYNO.API.Auth
        Returns DSM API info. No auth required for API discovery.
        """
        port = self.probe_port or 5000
        t0 = time.monotonic()
        try:
            resp = await client.get(
                f"http://{self.ip}:{port}/webapi/entry.cgi",
                params={"api": "SYNO.API.Info", "version": "1", "method": "query", "query": "SYNO.API.Auth"},
                timeout=NODE_PROBE_TIMEOUT,
            )
            self.latency_ms = round((time.monotonic() - t0) * 1000, 1)
            if resp.status_code == 200:
                self.status = NodeStatus.ONLINE
                try:
                    d = resp.json()
                    self.probe_detail = {
                        "dsm_api": True,
                        "success": d.get("success", False),
                    }
                except Exception:
                    self.probe_detail = {"dsm_api": True}
            elif resp.status_code in (301, 302, 303):
                # Redirect usually means HTTPS-only mode
                self.status = NodeStatus.ONLINE
                self.probe_detail = {"dsm_api": True, "redirect": "https"}
            else:
                self.status = NodeStatus.DEGRADED
        except (httpx.ConnectError, httpx.TimeoutException):
            self.status = NodeStatus.OFFLINE
            self.latency_ms = None

    async def _probe_https(self, client: httpx.AsyncClient) -> None:
        """Probe HTTPS port — connectivity check only."""
        port = self.probe_port or 443
        t0 = time.monotonic()
        try:
            resp = await client.get(
                f"https://{self.ip}:{port}/",
                timeout=NODE_PROBE_TIMEOUT,
            )
            self.latency_ms = round((time.monotonic() - t0) * 1000, 1)
            self.status = NodeStatus.ONLINE if resp.status_code < 500 else NodeStatus.DEGRADED
        except (httpx.ConnectError, httpx.TimeoutException):
            self.status = NodeStatus.OFFLINE
            self.latency_ms = None
        except Exception:
            # TLS error / cert issue still means the box is alive
            self.latency_ms = round((time.monotonic() - t0) * 1000, 1)
            self.status = NodeStatus.ONLINE
            self.probe_detail = {"tls_probe": "cert/verify error — host reachable"}

    async def _probe_http(self, client: httpx.AsyncClient) -> None:
        """Probe generic HTTP port."""
        port = self.probe_port or 80
        t0 = time.monotonic()
        try:
            resp = await client.get(
                f"http://{self.ip}:{port}/",
                timeout=NODE_PROBE_TIMEOUT,
                follow_redirects=True,
            )
            self.latency_ms = round((time.monotonic() - t0) * 1000, 1)
            self.status = NodeStatus.ONLINE if resp.status_code < 500 else NodeStatus.DEGRADED
        except (httpx.ConnectError, httpx.TimeoutException):
            self.status = NodeStatus.OFFLINE
            self.latency_ms = None
        except Exception:
            self.status = NodeStatus.OFFLINE
            self.latency_ms = None

    def to_dict(self) -> dict:
        return {
            "name":          self.name,
            "label":         self.label,
            "role":          self.role,
            "ip":            self.ip,
            "type":          self.node_type.value,
            "status":        self.status.value,
            "is_primary":    self.is_primary,
            "probe_type":    self.probe_type,
            "ollama_url":    self.ollama_url,
            "ollama_models": self.ollama_models,
            "latency_ms":    self.latency_ms,
            "last_seen":     self.last_seen,
            "detail":        self.probe_detail,
        }


class HiveCluster:
    """
    CVG Hive-0 cluster manager for Neuron's distributed cognition.

    Neuron operates across this cluster — not just on cvg-stormsurge-01.
    Queens hold distributed memory. Forges handle heavy inference.
    Edge nodes feed real-time intelligence via blockchain tunnels.

    Full topology (per CVG_NETWORK_STANDARD.md):
      QUEEN-10  HP ML350 Gen10     10.10.10.58/61/100  ESXi + TrueNAS
      QUEEN-11  Dell PowerEdge R820 10.10.10.50/56      iDRAC + Proxmox VE
      QUEEN-12  Synology DS1823+   10.10.10.53          Primary NAS
      QUEEN-20  Synology DS3622xs+ 10.10.10.67          Media / Big Data NAS
      QUEEN-21  TerraMaster        10.10.10.57          Auxiliary NAS
      QUEEN-30  Synology DS418     10.10.10.71          Archive NAS
      vm-451    cvg-stormsurge-01  10.10.10.200         Primary AI/Docker (Ollama)
      vm-454    secondary VM       10.10.10.204         Compute
      vm-455    tertiary VM        10.10.10.205         Compute
      audit-vm  Wazuh/Trivy        10.10.10.220         Security
      forti     FortiGate          10.10.10.1           Gateway
    """

    def __init__(self):
        self.nodes: Dict[str, ClusterNode] = {
            name: ClusterNode(name, config)
            for name, config in KNOWN_NODES.items()
        }
        self._edge_connectors: dict = {}
        self._last_scan: Optional[str] = None
        logger.info(
            "[cluster] HiveCluster initialized — %d nodes (%d queens, %d workers, %d edge)",
            len(self.nodes),
            sum(1 for n in self.nodes.values() if n.node_type == NodeType.QUEEN),
            sum(1 for n in self.nodes.values() if n.node_type == NodeType.WORKER),
            sum(1 for n in self.nodes.values() if n.node_type == NodeType.EDGE),
        )

    async def scan_cluster(self) -> dict:
        """
        Concurrently probe all known nodes to build a cluster health picture.
        Uses per-node probe strategies (Ollama, Proxmox, Synology, HTTP/S).
        """
        logger.info("[cluster] Scanning %d Hive-0 nodes...", len(self.nodes))

        # Use verify=False for HTTPS probes (self-signed certs are common in Hive-0)
        async with httpx.AsyncClient(verify=False) as client:
            tasks = [node.probe(client) for node in self.nodes.values()]
            await asyncio.gather(*tasks, return_exceptions=True)

        self._last_scan = datetime.utcnow().isoformat() + "Z"

        online = [n for n in self.nodes.values() if n.status == NodeStatus.ONLINE]
        queens = [n for n in online if n.node_type == NodeType.QUEEN]
        ollama_nodes = [n for n in online if n.ollama_url and n.ollama_models]

        result = {
            "timestamp":        self._last_scan,
            "total_nodes":      len(self.nodes),
            "online_nodes":     len(online),
            "queens_online":    len(queens),
            "ollama_instances": len(ollama_nodes),
            "edge_connectors":  len(self._edge_connectors),
            "nodes":            {name: node.to_dict() for name, node in self.nodes.items()},
        }

        logger.info(
            "[cluster] Scan complete — %d/%d online (%d queens, %d Ollama)",
            len(online), len(self.nodes), len(queens), len(ollama_nodes),
        )
        return result

    def get_best_inference_node(self, prefer_heavy: bool = False) -> Optional[dict]:
        """Select the best available inference node."""
        online_with_ollama = [
            n for n in self.nodes.values()
            if n.status == NodeStatus.ONLINE and n.ollama_url
        ]
        if not online_with_ollama:
            return None

        # Always prefer primary for standard ops
        if not prefer_heavy:
            primary = next((n for n in online_with_ollama if n.is_primary), None)
            if primary:
                return primary.to_dict()

        # Forge nodes for heavy ops
        if prefer_heavy:
            forges = [n for n in online_with_ollama if n.node_type == NodeType.FORGE]
            if forges:
                return min(forges, key=lambda n: n.latency_ms or 9999).to_dict()

        # Lowest latency fallback
        return min(online_with_ollama, key=lambda n: n.latency_ms or 9999).to_dict()

    def register_edge_connector(
        self,
        edge_id: str,
        endpoint: str,
        connector_type: str = "blockchain_tunnel",
        metadata: Optional[dict] = None,
    ) -> None:
        """Register an edge connector."""
        self._edge_connectors[edge_id] = {
            "edge_id": edge_id,
            "endpoint": endpoint,
            "type": connector_type,
            "metadata": metadata or {},
            "registered": datetime.utcnow().isoformat() + "Z",
            "status": "registered",
        }
        logger.info("[cluster] Edge connector registered: %s (%s)", edge_id, connector_type)

    def get_cluster_state_for_neuron(self) -> str:
        """Build a text summary of cluster state for Neuron's cognitive context."""
        online = [n for n in self.nodes.values() if n.status != NodeStatus.UNKNOWN]
        if not online:
            return "Cluster state: not yet scanned — scan running at startup."

        online_nodes  = [n for n in self.nodes.values() if n.status == NodeStatus.ONLINE]
        offline_nodes = [n for n in self.nodes.values() if n.status == NodeStatus.OFFLINE]
        ollama_nodes  = [n for n in online_nodes if n.ollama_url]

        lines = [
            f"CVG Hive-0 Cluster State (scanned: {self._last_scan or 'pending'})",
            f"Online: {len(online_nodes)}/{len(self.nodes)} nodes | "
            f"Ollama: {len(ollama_nodes)} instances | "
            f"Edge connectors: {len(self._edge_connectors)}",
            "",
        ]

        # Group by type
        for node_type_label, ntype in [("QUEENS", NodeType.QUEEN), ("WORKERS", NodeType.WORKER),
                                        ("EDGE", NodeType.EDGE), ("FORGE", NodeType.FORGE)]:
            group = [n for n in self.nodes.values() if n.node_type == ntype
                     and n.status != NodeStatus.UNKNOWN]
            if not group:
                continue
            lines.append(f"  [{node_type_label}]")
            for node in group:
                icon = "●" if node.status == NodeStatus.ONLINE else (
                       "◐" if node.status == NodeStatus.DEGRADED else "○")
                lat  = f" {node.latency_ms}ms" if node.latency_ms else ""
                det  = ""
                if node.probe_detail:
                    if "version" in node.probe_detail:
                        det = f" | PVE {node.probe_detail['version']}"
                    elif node.ollama_models:
                        det = f" | models: {','.join(node.ollama_models[:2])}"
                lines.append(f"    {icon} {node.label} ({node.ip}){lat}{det}")
                if node.role:
                    lines.append(f"       ↳ {node.role}")

        if offline_nodes:
            lines.append(f"\n  ○ OFFLINE ({len(offline_nodes)}): " +
                         ", ".join(f"{n.label}({n.ip})" for n in offline_nodes))

        if self._edge_connectors:
            lines.append(f"\n  Edge Connectors ({len(self._edge_connectors)}):")
            for eid, ec in self._edge_connectors.items():
                lines.append(f"    → {eid} [{ec['type']}] {ec['endpoint']}")

        return "\n".join(lines)

    def add_node(self, name: str, ip: str, node_type: str = "worker",
                 ollama_port: Optional[int] = None, label: str = "", role: str = "") -> None:
        """Dynamically add a node (e.g. auto-discovered)."""
        config = {
            "ip": ip, "type": node_type, "ollama_port": ollama_port,
            "primary": False, "probe_type": "ollama" if ollama_port else "http",
            "label": label or name, "role": role,
        }
        self.nodes[name] = ClusterNode(name, config)
        logger.info("[cluster] Node added: %s (%s) [%s]", name, ip, node_type)

    def get_node_status(self) -> dict:
        return {name: node.to_dict() for name, node in self.nodes.items()}

    def list_edge_connectors(self) -> list:
        return list(self._edge_connectors.values())

    def get_hive0_summary(self) -> dict:
        """
        Return a structured Hive-0 summary dict for the /api/hive0/status endpoint.
        Groups nodes by queen type vs compute.
        """
        queens  = {n: node.to_dict() for n, node in self.nodes.items()
                   if node.node_type == NodeType.QUEEN}
        workers = {n: node.to_dict() for n, node in self.nodes.items()
                   if node.node_type == NodeType.WORKER}
        edge    = {n: node.to_dict() for n, node in self.nodes.items()
                   if node.node_type == NodeType.EDGE}

        online_count = sum(1 for n in self.nodes.values() if n.status == NodeStatus.ONLINE)
        return {
            "cluster":      "CVG Hive-0",
            "location":     "New Smyrna Beach, FL",
            "network":      "10.10.10.0/24 (VLAN 10) + 10.10.20.0/24 (VLAN 20)",
            "total_nodes":  len(self.nodes),
            "online_nodes": online_count,
            "last_scan":    self._last_scan,
            "queens":       queens,
            "workers":      workers,
            "edge":         edge,
            "edge_connectors": self._edge_connectors,
        }

    @property
    def _nodes(self) -> dict:
        return self.nodes

    def get_stats(self) -> dict:
        online = sum(1 for n in self.nodes.values() if n.status == NodeStatus.ONLINE)
        queens = sum(1 for n in self.nodes.values()
                     if n.node_type == NodeType.QUEEN and n.status == NodeStatus.ONLINE)
        return {
            "total_nodes":    len(self.nodes),
            "online_nodes":   online,
            "queens_online":  queens,
            "edge_connectors": len(self._edge_connectors),
            "last_scan":      self._last_scan,
        }


# ─── Module-level singleton ───────────────────────────────────────────────────

_cluster: Optional[HiveCluster] = None


def get_cluster() -> HiveCluster:
    global _cluster
    if _cluster is None:
        _cluster = HiveCluster()
    return _cluster
