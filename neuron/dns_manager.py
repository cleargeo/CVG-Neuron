# =============================================================================
# CVG Neuron — DNS Manager
# Gives Neuron visibility into DNS health, zone status, and migration state.
# Integrates with CVG DNS Support Engine (port 8810) and direct BIND9 queries.
#
# (c) Clearview Geographic, LLC — Proprietary
# =============================================================================
from __future__ import annotations

import asyncio
import logging
import re
import socket
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

log = logging.getLogger("cvg.neuron.dns")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DNS_ENGINE_URL  = "http://10.10.10.200:8810"   # CVG DNS Support Engine
PRIMARY_DOMAIN  = "cleargeo.tech"
NS1_INTERNAL_IP = "10.10.10.200"               # cvg-stormsurge-01

# All CVG domains with their vanity nameserver names (Cloudflare-backed)
CVG_NS_CONFIG: dict[str, dict] = {
    "cleargeo.tech": {
        "ns1": "ns1.cleargeo.tech",
        "ns2": "ns2.cleargeo.tech",
        "description": "CVG primary public domain",
    },
    "clearviewgeographic.com": {
        "ns1": "ns1.clearviewgeographic.com",
        "ns2": "ns2.clearviewgeographic.com",
        "description": "CVG corporate full name domain",
    },
    "cvg-nexus.com": {
        "ns1": "ns1.cvg-nexus.com",
        "ns2": "ns2.cvg-nexus.com",
        "description": "CVG Nexus platform domain",
    },
}

# Convenience aliases for primary domain
NS1_HOSTNAME = CVG_NS_CONFIG[PRIMARY_DOMAIN]["ns1"]
NS2_HOSTNAME = CVG_NS_CONFIG[PRIMARY_DOMAIN]["ns2"]

# Known CVG service records to health-check
CVG_SUBDOMAINS = [
    "cleargeo.tech",
    "www.cleargeo.tech",
    "git.cleargeo.tech",
    "neuron.cleargeo.tech",
    "mail.cleargeo.tech",
]

# Regex patterns for detecting DNS-related questions in chat
_DNS_PATTERNS = [
    r"\bdns\b",
    r"\bnameserver\b",
    r"\bns1\b", r"\bns2\b",
    r"\bhostgator\b",
    r"\bmigrat\w+\s+dns\b",
    r"\bdns\s+migrat",
    r"\bzone\s+file\b",
    r"\bbind9?\b",
    r"\bglue\s+record",
    r"\bdomain.*resolv",
    r"\bpropagat\w+\b",
    r"\bregistrar\b",
    r"\b(?:a|mx|txt|cname|soa|ns)\s+record\b",
    r"\bcleargeo\.tech\b.*(?:dns|resolv|domain)",
]
_DNS_RE = re.compile("|".join(_DNS_PATTERNS), re.IGNORECASE)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DnsRecord:
    name: str
    rtype: str
    value: str
    ttl: int = 300


@dataclass
class DnsZoneStatus:
    domain: str
    timestamp: float = field(default_factory=time.time)
    ns_records: list[str] = field(default_factory=list)
    a_record: str = ""
    mx_records: list[str] = field(default_factory=list)
    using_selfhosted: bool = False
    using_hostgator: bool = False
    migration_complete: bool = False
    bind9_engine_online: bool = False
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"DNS Status for {self.domain}:"]
        if self.ns_records:
            lines.append(f"  Nameservers: {', '.join(self.ns_records)}")
        if self.a_record:
            lines.append(f"  A record:    {self.a_record}")
        if self.mx_records:
            lines.append(f"  MX records:  {', '.join(self.mx_records)}")

        if self.using_selfhosted:
            lines.append("  [OK] Using SELF-HOSTED nameservers (migration COMPLETE)")
        elif self.using_hostgator:
            lines.append("  [PENDING] Still using HostGator nameservers — migration NOT yet complete")
        else:
            lines.append("  [UNKNOWN] Nameserver status undetermined")

        lines.append(f"  CVG DNS Engine online: {'YES' if self.bind9_engine_online else 'NO'}")

        if self.errors:
            lines.append(f"  Errors: {'; '.join(self.errors)}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# DNS query helpers (no external dependencies — uses socket + subprocess)
# ---------------------------------------------------------------------------

def _dig(hostname: str, rtype: str = "A", nameserver: str = "") -> list[str]:
    """Run dig and return answer lines. Falls back to nslookup if dig unavailable."""
    try:
        cmd = ["dig", "+short", "+time=5", "+tries=2"]
        if nameserver:
            cmd.append(f"@{nameserver}")
        cmd += [hostname, rtype]
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=10, encoding="utf-8", errors="replace"
        )
        lines = [l.strip() for l in result.stdout.strip().splitlines() if l.strip()]
        return lines
    except FileNotFoundError:
        # dig not available — try nslookup
        return _nslookup(hostname, rtype)
    except Exception as exc:
        log.debug("dig error for %s %s: %s", hostname, rtype, exc)
        return []


def _nslookup(hostname: str, rtype: str = "A") -> list[str]:
    """Fallback DNS resolver using nslookup (available on Windows)."""
    try:
        cmd = ["nslookup", f"-type={rtype}", hostname]
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=10, encoding="utf-8", errors="replace"
        )
        lines = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if rtype == "A" and "Address:" in line and not line.startswith("Server"):
                addr = line.split("Address:")[-1].strip()
                if addr:
                    lines.append(addr)
            elif rtype == "NS" and "nameserver" in line.lower():
                ns = line.split("=")[-1].strip() if "=" in line else line.split()[-1]
                lines.append(ns)
            elif rtype == "MX" and "mail exchanger" in line.lower():
                lines.append(line)
        return lines
    except Exception as exc:
        log.debug("nslookup error for %s %s: %s", hostname, rtype, exc)
        return []


def _resolve_simple(hostname: str) -> str:
    """Quick socket-based A record lookup (no subprocess needed)."""
    try:
        return socket.gethostbyname(hostname)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# DNS Engine client
# ---------------------------------------------------------------------------

async def _engine_health() -> bool:
    """Check if CVG DNS Support Engine is reachable."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{DNS_ENGINE_URL}/health")
            return resp.status_code == 200
    except Exception:
        return False


async def _engine_get_zones() -> list[str]:
    """List zones managed by the CVG DNS Support Engine."""
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(f"{DNS_ENGINE_URL}/zones")
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    return data.get("zones", [])
    except Exception as exc:
        log.debug("engine zones error: %s", exc)
    return []


async def _engine_get_records(zone: str) -> list[DnsRecord]:
    """Fetch records for a zone from the DNS Support Engine."""
    records = []
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(f"{DNS_ENGINE_URL}/zones/{zone}/records")
            if resp.status_code == 200:
                data = resp.json()
                items = data if isinstance(data, list) else data.get("records", [])
                for item in items:
                    records.append(DnsRecord(
                        name=item.get("name", ""),
                        rtype=item.get("type", "A"),
                        value=item.get("value", item.get("rdata", "")),
                        ttl=item.get("ttl", 300),
                    ))
    except Exception as exc:
        log.debug("engine records error for %s: %s", zone, exc)
    return records


# ---------------------------------------------------------------------------
# Core status check
# ---------------------------------------------------------------------------

async def get_dns_status(domain: str = PRIMARY_DOMAIN) -> DnsZoneStatus:
    """
    Comprehensive DNS status check for the given domain.
    Returns a DnsZoneStatus with current NS, A, MX records and migration state.
    """
    status = DnsZoneStatus(domain=domain)

    # Check DNS Engine
    status.bind9_engine_online = await _engine_health()

    # Get current NS records
    ns_list = _dig(domain, "NS")
    if not ns_list:
        ns_list = _nslookup(domain, "NS")
    status.ns_records = [ns.rstrip(".").lower() for ns in ns_list if ns]

    # Determine migration state
    for ns in status.ns_records:
        if "hostgator" in ns:
            status.using_hostgator = True
        if "cleargeo.tech" in ns or ns == NS1_HOSTNAME or ns == NS2_HOSTNAME:
            status.using_selfhosted = True

    status.migration_complete = status.using_selfhosted and not status.using_hostgator

    # Get A record
    a_records = _dig(domain, "A")
    status.a_record = a_records[0] if a_records else _resolve_simple(domain)

    # Get MX records
    status.mx_records = _dig(domain, "MX")

    return status


async def check_subdomain_resolution() -> dict[str, str]:
    """
    Check that all known CVG subdomains are resolving.
    Returns dict of {subdomain: resolved_ip_or_error}
    """
    results = {}
    for subdomain in CVG_SUBDOMAINS:
        ip = _resolve_simple(subdomain)
        results[subdomain] = ip if ip else "UNRESOLVABLE"
    return results


async def get_zone_records(zone: str = PRIMARY_DOMAIN) -> list[DnsRecord]:
    """Fetch all records for a zone from DNS Engine (if available)."""
    return await _engine_get_records(zone)


# ---------------------------------------------------------------------------
# Context builder — provides DNS context for Neuron prompts
# ---------------------------------------------------------------------------

async def build_dns_context() -> str:
    """Build a rich DNS status context string for injection into Neuron prompts."""
    try:
        status = await get_dns_status()
        lines = [
            "=== CVG DNS Status ===",
            status.summary(),
        ]

        if not status.migration_complete:
            lines.append("")
            lines.append("MIGRATION STATUS: PENDING — Domain still uses HostGator DNS")
            lines.append("Action required: See docs/DNS_MIGRATION_PLAYBOOK.md")
            lines.append(f"Next steps: Deploy BIND9 on ns1({NS1_INTERNAL_IP}), reduce TTLs, register glue records")

        if status.bind9_engine_online:
            zones = await _engine_get_zones()
            lines.append(f"\nCVG DNS Engine zones managed: {', '.join(zones) if zones else 'none yet'}")

        return "\n".join(lines)

    except Exception as exc:
        log.warning("DNS context build failed: %s", exc)
        return "DNS Status: unavailable (DNS manager error)"


# ---------------------------------------------------------------------------
# Natural language query detection
# ---------------------------------------------------------------------------

def is_dns_query(message: str) -> bool:
    """Return True if the user message is asking about DNS / nameservers / migration."""
    return bool(_DNS_RE.search(message))


def extract_dns_intent(message: str) -> str:
    """
    Classify the DNS intent from a message.
    Returns one of: 'status', 'migrate', 'records', 'zone', 'help', 'unknown'
    """
    msg_lower = message.lower()
    if any(w in msg_lower for w in ["status", "health", "working", "resolv", "check"]):
        return "status"
    if any(w in msg_lower for w in ["migrat", "switch", "move", "cutover", "hostgator", "away from"]):
        return "migrate"
    if any(w in msg_lower for w in ["record", "a record", "mx", "txt", "cname", "soa"]):
        return "records"
    if any(w in msg_lower for w in ["zone", "zone file", "bind", "named"]):
        return "zone"
    if any(w in msg_lower for w in ["how", "help", "playbook", "steps", "plan"]):
        return "help"
    return "status"  # default to status check


# ---------------------------------------------------------------------------
# Neuron command dispatch
# ---------------------------------------------------------------------------

async def handle_dns_command(message: str) -> str:
    """
    Handle a DNS-related command from the user.
    Called from mind.py REASON step when is_dns_query() is True.
    """
    intent = extract_dns_intent(message)
    log.info("DNS command: intent=%s, message=%s", intent, message[:80])

    if intent == "status":
        status = await get_dns_status()
        subdomain_checks = await check_subdomain_resolution()

        lines = [status.summary(), ""]
        lines.append("Subdomain resolution:")
        for sub, ip in subdomain_checks.items():
            icon = "OK" if ip and ip != "UNRESOLVABLE" else "FAIL"
            lines.append(f"  [{icon}] {sub:<35} {ip}")

        return "\n".join(lines)

    elif intent == "migrate":
        status = await get_dns_status()
        if status.migration_complete:
            return (
                f"DNS migration for {status.domain} is COMPLETE.\n"
                f"Nameservers: {', '.join(status.ns_records)}\n"
                f"Your domain is now 100% self-hosted on CVG infrastructure."
            )
        else:
            return (
                f"DNS migration for {status.domain} is PENDING.\n"
                f"Currently using: {', '.join(status.ns_records)}\n\n"
                f"Full playbook: docs/DNS_MIGRATION_PLAYBOOK.md\n\n"
                f"Quick steps:\n"
                f"  T-24h:  Reduce HostGator TTLs to 300s\n"
                f"  T-12h:  Deploy BIND9 on ns1 ({NS1_INTERNAL_IP}) + ns2\n"
                f"  T-6h:   Test: dig @NS1_IP {status.domain} SOA\n"
                f"  T-1h:   Register GLUE RECORDS at HostGator registrar\n"
                f"  T=0:    Update registrar NS to ns1/ns2.cleargeo.tech\n"
                f"  T+1h:   Verify all CVG services resolve correctly\n"
            )

    elif intent == "records":
        records = await get_zone_records()
        if records:
            lines = [f"Zone records for {PRIMARY_DOMAIN} (from DNS Engine):"]
            for r in records[:50]:  # cap display
                lines.append(f"  {r.rtype:<8} {r.name:<35} {r.value}")
            if len(records) > 50:
                lines.append(f"  ... and {len(records) - 50} more records")
            return "\n".join(lines)
        else:
            status = await get_dns_status()
            return (
                f"DNS records from Engine not available (engine online: {status.bind9_engine_online}).\n"
                f"Current nameservers: {', '.join(status.ns_records)}\n"
                f"A record: {status.a_record}\n"
                f"MX: {', '.join(status.mx_records) if status.mx_records else 'none'}\n"
                f"To get full records, ensure CVG DNS Engine is running at {DNS_ENGINE_URL}"
            )

    elif intent == "zone":
        engine_up = await _engine_health()
        zones = await _engine_get_zones() if engine_up else []
        return (
            f"BIND9 Zone Management:\n"
            f"  CVG DNS Engine: {'ONLINE' if engine_up else 'OFFLINE'} at {DNS_ENGINE_URL}\n"
            f"  Managed zones: {', '.join(zones) if zones else 'none (not yet migrated)'}\n"
            f"  Zone template: config/bind9/cleargeo.tech.zone.template\n"
            f"  Primary conf:  config/bind9/named.conf.primary\n"
            f"  Secondary conf: config/bind9/named.conf.secondary\n"
            f"  Deploy zone files to: /opt/cvg/bind/zones/ on ns1 ({NS1_INTERNAL_IP})\n"
        )

    elif intent == "help":
        return (
            "CVG DNS Migration Playbook Summary:\n"
            "  Full guide: docs/DNS_MIGRATION_PLAYBOOK.md\n\n"
            "  Key phases:\n"
            "    Phase 1: Audit — export all HostGator records\n"
            "    Phase 2: Build — deploy BIND9 on ns1 + ns2\n"
            "    Phase 3: Stage — reduce TTLs to 300s, test BIND9 directly\n"
            "    Phase 4: Cutover — register glue records, update registrar\n"
            "    Phase 5: Verify — check all records + raise TTLs\n\n"
            "  Quick audit commands:\n"
            "    Linux: bash scripts/dns_audit.sh cleargeo.tech\n"
            "    Windows: .\\scripts\\dns_audit.ps1 -Domain cleargeo.tech\n\n"
            "  Config files:\n"
            "    config/bind9/cleargeo.tech.zone.template\n"
            "    config/bind9/named.conf.primary\n"
            "    config/bind9/named.conf.secondary\n"
        )

    # Fallback
    return await handle_dns_command("status")


# ---------------------------------------------------------------------------
# Standalone health check
# ---------------------------------------------------------------------------

async def dns_health_check() -> dict:
    """
    Lightweight health check for use by web_api.py lifespan / scheduler.
    Returns a dict with migration_complete, ns_records, engine_online.
    """
    try:
        status = await get_dns_status()
        return {
            "domain": status.domain,
            "ns_records": status.ns_records,
            "migration_complete": status.migration_complete,
            "using_hostgator": status.using_hostgator,
            "using_selfhosted": status.using_selfhosted,
            "a_record": status.a_record,
            "engine_online": status.bind9_engine_online,
            "errors": status.errors,
        }
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    async def _main():
        if len(sys.argv) > 1:
            cmd = " ".join(sys.argv[1:])
        else:
            cmd = "status"
        result = await handle_dns_command(cmd)
        print(result)
        print()
        print("--- Raw health check ---")
        health = await dns_health_check()
        for k, v in health.items():
            print(f"  {k}: {v}")

    asyncio.run(_main())
