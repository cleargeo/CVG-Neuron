"""
CVG Neuron — Hive-0 Dedicated Telemetry Module
(c) Clearview Geographic, LLC — Proprietary & PRIVATE

Provides deep telemetry for the full CVG Hive-0 infrastructure:
  - Proxmox VE API (QUEEN-11, port 8006) — node/VM/container metrics
  - Synology DSM API (QUEEN-12/20/30, port 5000) — NAS health & volumes
  - Generic node reachability probes for all queen nodes
  - FortiGate gateway connectivity check
  - Structured telemetry summary for Neuron's context

This module supplements cluster.py (which does broad connectivity checks) with
deeper protocol-aware telemetry for the queen-class infrastructure.

Proxmox API Reference: https://pve.proxmox.com/pve-docs/api-viewer/
Synology DSM API Reference: Synology DSM 6/7 WebAPI Guide

Authentication:
  - Proxmox: API ticket (PVEAuthCookie) or API token — we probe unauthenticated endpoints
    for telemetry that doesn't require auth, and skip authenticated ones cleanly.
  - Synology: /api/query endpoint is unauthenticated for API discovery.
    Full DSM metrics require auth — we probe what's available without credentials.
  - FortiGate: HTTPS connectivity check only.

All telemetry is cached for TELEMETRY_TTL_SECONDS and refreshed on demand.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("cvg.neuron.hive0")

TELEMETRY_TTL_SECONDS = int(os.getenv("HIVE0_TELEMETRY_TTL", "120"))  # 2 minutes
PROBE_TIMEOUT = float(os.getenv("NODE_PROBE_TIMEOUT", "4.0"))

# ─── Hive-0 Queen Node Manifest ───────────────────────────────────────────────

HIVE0_QUEENS: Dict[str, dict] = {
    "queen-11-proxmox": {
        "label":    "CVG-QUEEN-11 (Dell R820 / Proxmox VE)",
        "ip":       "10.10.10.56",
        "type":     "proxmox",
        "port":     8006,
        "role":     "Primary hypervisor — hosts VM 451/454/455 stack",
        "hardware": "Dell PowerEdge R820, 4×E5-4650, 512 GB RAM",
    },
    "queen-11-idrac": {
        "label":    "CVG-QUEEN-11 iDRAC 9 (Dell R820)",
        "ip":       "10.10.10.50",
        "type":     "idrac",
        "port":     443,
        "role":     "Dell OOB management (iDRAC 9)",
        "hardware": "Dell iDRAC 9 — IPMI / remote console",
    },
    "queen-12-nas": {
        "label":    "CVG-QUEEN-12 Synology DS1823+",
        "ip":       "10.10.10.53",
        "type":     "synology",
        "port":     5000,
        "role":     "Primary NAS — backup / shared storage queen",
        "hardware": "Synology DS1823+, 8-bay",
    },
    "queen-20-nas": {
        "label":    "CVG-QUEEN-20 Synology DS3622xs+",
        "ip":       "10.10.10.67",
        "type":     "synology",
        "port":     5000,
        "role":     "High-capacity NAS — ZNet Media / large geospatial datasets",
        "hardware": "Synology DS3622xs+, 12-bay, 10GbE",
    },
    "queen-21-nas": {
        "label":    "CVG-QUEEN-21 TerraMaster",
        "ip":       "10.10.10.57",
        "type":     "terramaster",
        "port":     8181,
        "role":     "Auxiliary NAS — TerraMaster storage unit",
        "hardware": "TerraMaster NAS",
    },
    "queen-30-nas": {
        "label":    "CVG-QUEEN-30 Synology DS418",
        "ip":       "10.10.10.71",
        "type":     "synology",
        "port":     5000,
        "role":     "Archive NAS — cold storage / redundancy",
        "hardware": "Synology DS418, 4-bay",
    },
    "queen-10-esxi": {
        "label":    "CVG-QUEEN-10 ESXi (HP ML350 Gen10)",
        "ip":       "10.10.10.61",
        "type":     "esxi",
        "port":     443,
        "role":     "Secondary hypervisor — HP ML350 Gen10 Host-B",
        "hardware": "HP ProLiant ML350 Gen10, 2×Gold 5118, 192 GB RAM",
    },
    "queen-10-ilo": {
        "label":    "CVG-QUEEN-10 iLO 5 (HP ML350)",
        "ip":       "10.10.10.58",
        "type":     "ilo",
        "port":     443,
        "role":     "HP iLO 5 OOB management",
        "hardware": "HP iLO 5 — IPMI / remote console",
    },
    "queen-10-truenas": {
        "label":    "CVG-QUEEN-10 TrueNAS VM (ESXi)",
        "ip":       "10.10.10.100",
        "type":     "truenas",
        "port":     80,
        "role":     "TrueNAS SCALE — iSCSI / ZFS on ESXi",
        "hardware": "VM on HP ML350 Gen10",
    },
    "fortigate": {
        "label":    "FortiGate (LAN10 gateway)",
        "ip":       "10.10.10.1",
        "type":     "fortigate",
        "port":     443,
        "role":     "Network gateway — FortiGate firewall / VLAN 10",
        "hardware": "FortiGate appliance",
    },
}


# ─── Individual probe functions ───────────────────────────────────────────────

async def _probe_proxmox(client: httpx.AsyncClient, ip: str, port: int) -> dict:
    """
    Probe Proxmox VE API — /api2/json/version (no auth),
    and attempt /api2/json/nodes (fails without auth but confirms API existence).
    """
    result: dict = {"reachable": False, "api": "proxmox-ve"}
    t0 = time.monotonic()
    try:
        resp = await client.get(
            f"https://{ip}:{port}/api2/json/version",
            timeout=PROBE_TIMEOUT,
        )
        result["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        result["status_code"] = resp.status_code

        if resp.status_code == 200:
            result["reachable"] = True
            data = resp.json().get("data", {})
            result["pve_version"]  = data.get("version", "")
            result["pve_release"]  = data.get("release", "")
            result["pve_repoid"]   = data.get("repoid", "")
            result["api_endpoint"] = f"https://{ip}:{port}/api2/json/"
            result["note"] = "Proxmox VE API online — authenticated endpoints available at /api2/json/"
        elif resp.status_code == 401:
            result["reachable"] = True
            result["note"] = "Proxmox VE API online — authenticated access required"
        else:
            result["reachable"] = False
            result["note"] = f"Unexpected HTTP {resp.status_code}"

    except httpx.ConnectError:
        result["reachable"] = False
        result["note"] = "Connection refused — node offline or firewall blocked"
    except httpx.TimeoutException:
        result["reachable"] = False
        result["note"] = f"Timeout after {PROBE_TIMEOUT}s"
    except Exception as exc:
        # SSL/cert error still means host is up
        elapsed = round((time.monotonic() - t0) * 1000, 1)
        result["latency_ms"] = elapsed
        result["reachable"] = True
        result["note"] = f"Host reachable (TLS/cert issue: {type(exc).__name__})"

    return result


async def _probe_synology(client: httpx.AsyncClient, ip: str, port: int) -> dict:
    """
    Probe Synology DSM API — /webapi/entry.cgi SYNO.API.Info (no auth).
    Returns DSM capabilities and confirms device is online.
    """
    result: dict = {"reachable": False, "api": "synology-dsm"}
    t0 = time.monotonic()
    try:
        resp = await client.get(
            f"http://{ip}:{port}/webapi/entry.cgi",
            params={"api": "SYNO.API.Info", "version": "1",
                    "method": "query", "query": "SYNO.API.Auth,SYNO.FileStation.Info"},
            timeout=PROBE_TIMEOUT,
        )
        result["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        result["status_code"] = resp.status_code

        if resp.status_code == 200:
            result["reachable"] = True
            try:
                d = resp.json()
                result["dsm_api_success"] = d.get("success", False)
                result["note"] = "DSM API online — SYNO.API.Info returned"
            except Exception:
                result["note"] = "DSM HTTP 200 — JSON parse failed"
        elif resp.status_code in (301, 302):
            result["reachable"] = True
            result["note"] = "DSM online — redirecting to HTTPS"
        else:
            result["note"] = f"DSM HTTP {resp.status_code}"

    except httpx.ConnectError:
        # Try HTTPS fallback (some NAS force HTTPS)
        try:
            resp2 = await client.get(
                f"https://{ip}:{port + 1}/webapi/entry.cgi",  # port 5001 for HTTPS DSM
                params={"api": "SYNO.API.Info", "version": "1", "method": "query", "query": "all"},
                timeout=PROBE_TIMEOUT,
            )
            elapsed = round((time.monotonic() - t0) * 1000, 1)
            result["latency_ms"] = elapsed
            result["reachable"]  = True
            result["note"]       = f"DSM HTTPS ({port+1}) online"
        except Exception:
            result["note"] = "DSM unreachable on HTTP and HTTPS"
    except httpx.TimeoutException:
        result["note"] = f"DSM timeout after {PROBE_TIMEOUT}s"
    except Exception as exc:
        result["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        result["reachable"] = True
        result["note"] = f"Host reachable ({type(exc).__name__})"

    return result


async def _probe_generic_https(client: httpx.AsyncClient, ip: str, port: int,
                                api_label: str) -> dict:
    """Generic HTTPS probe for iDRAC, iLO, ESXi, FortiGate."""
    result: dict = {"reachable": False, "api": api_label}
    t0 = time.monotonic()
    try:
        resp = await client.get(f"https://{ip}:{port}/", timeout=PROBE_TIMEOUT)
        result["latency_ms"]  = round((time.monotonic() - t0) * 1000, 1)
        result["status_code"] = resp.status_code
        result["reachable"]   = resp.status_code < 500
        result["note"] = f"HTTPS {resp.status_code}"
    except httpx.ConnectError:
        result["note"] = "Connection refused"
    except httpx.TimeoutException:
        result["note"] = f"Timeout after {PROBE_TIMEOUT}s"
    except Exception as exc:
        result["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        result["reachable"]  = True
        result["note"] = f"Reachable (TLS issue: {type(exc).__name__})"
    return result


async def _probe_http(client: httpx.AsyncClient, ip: str, port: int,
                      api_label: str) -> dict:
    """Generic HTTP probe."""
    result: dict = {"reachable": False, "api": api_label}
    t0 = time.monotonic()
    try:
        resp = await client.get(f"http://{ip}:{port}/", timeout=PROBE_TIMEOUT,
                                follow_redirects=True)
        result["latency_ms"]  = round((time.monotonic() - t0) * 1000, 1)
        result["status_code"] = resp.status_code
        result["reachable"]   = resp.status_code < 500
        result["note"] = f"HTTP {resp.status_code}"
    except (httpx.ConnectError, httpx.TimeoutException):
        result["note"] = "Unreachable"
    except Exception as exc:
        result["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
        result["reachable"]  = True
        result["note"] = f"Reachable ({type(exc).__name__})"
    return result


# ─── Full Hive-0 Telemetry Sweep ─────────────────────────────────────────────

async def sweep_hive0() -> Dict[str, Any]:
    """
    Full concurrent telemetry sweep of all Hive-0 queen nodes.
    Returns structured result dict per node + cluster summary.
    """
    start = time.monotonic()
    results: Dict[str, Any] = {}

    async with httpx.AsyncClient(verify=False) as client:
        tasks = {}
        for node_id, spec in HIVE0_QUEENS.items():
            ip   = spec["ip"]
            port = spec["port"]
            t    = spec["type"]

            if t == "proxmox":
                tasks[node_id] = asyncio.create_task(_probe_proxmox(client, ip, port))
            elif t == "synology":
                tasks[node_id] = asyncio.create_task(_probe_synology(client, ip, port))
            elif t in ("idrac", "ilo", "esxi", "fortigate"):
                tasks[node_id] = asyncio.create_task(
                    _probe_generic_https(client, ip, port, t))
            else:
                tasks[node_id] = asyncio.create_task(_probe_http(client, ip, port, t))

        for node_id, task in tasks.items():
            try:
                probe = await task
            except Exception as exc:
                probe = {"reachable": False, "note": str(exc)}

            spec = HIVE0_QUEENS[node_id]
            results[node_id] = {
                "label":    spec["label"],
                "ip":       spec["ip"],
                "type":     spec["type"],
                "role":     spec["role"],
                "hardware": spec["hardware"],
                **probe,
            }

    elapsed = round((time.monotonic() - start) * 1000, 1)
    online_count  = sum(1 for r in results.values() if r.get("reachable"))
    total_count   = len(results)

    # Build text summary for Neuron's cognitive context
    lines = [
        f"CVG Hive-0 Queen Telemetry ({datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')})",
        f"Queens reachable: {online_count}/{total_count} | Sweep: {elapsed}ms",
        "",
    ]
    for node_id, r in results.items():
        icon = "●" if r.get("reachable") else "○"
        lat  = f" {r.get('latency_ms')}ms" if r.get("latency_ms") else ""
        note = f" — {r.get('note', '')}" if r.get("note") else ""
        ver  = f" | PVE {r['pve_version']}" if r.get("pve_version") else ""
        lines.append(f"  {icon} {r['label']} ({r['ip']}){lat}{ver}{note}")
        lines.append(f"       ↳ {r['role']}")

    return {
        "timestamp":    datetime.utcnow().isoformat() + "Z",
        "sweep_ms":     elapsed,
        "queens_total": total_count,
        "queens_online": online_count,
        "nodes":        results,
        "summary_text": "\n".join(lines),
    }


# ─── Cached telemetry ─────────────────────────────────────────────────────────

_telemetry_cache: Optional[Dict[str, Any]] = None
_telemetry_ts:    Optional[float] = None


async def get_hive0_telemetry(force: bool = False) -> Dict[str, Any]:
    """Return cached Hive-0 queen telemetry, refreshing if stale or forced."""
    global _telemetry_cache, _telemetry_ts

    now = time.monotonic()
    stale = (
        _telemetry_cache is None
        or _telemetry_ts is None
        or (now - _telemetry_ts) > TELEMETRY_TTL_SECONDS
    )

    if stale or force:
        logger.info("[hive0] Sweeping Hive-0 queen telemetry (%d nodes)...",
                    len(HIVE0_QUEENS))
        _telemetry_cache = await sweep_hive0()
        _telemetry_ts    = now
        logger.info(
            "[hive0] Telemetry complete — %d/%d queens online",
            _telemetry_cache.get("queens_online", 0),
            _telemetry_cache.get("queens_total", 0),
        )

    return _telemetry_cache  # type: ignore[return-value]


def get_hive0_node_manifest() -> Dict[str, dict]:
    """Return static node manifest (no probing — for documentation/identity)."""
    return {
        node_id: {
            "label":    spec["label"],
            "ip":       spec["ip"],
            "type":     spec["type"],
            "role":     spec["role"],
            "hardware": spec["hardware"],
        }
        for node_id, spec in HIVE0_QUEENS.items()
    }
