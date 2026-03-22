#!/usr/bin/env python3
# =============================================================================
# CVG Cloudflare DNS Setup — Automation Script
# Manages custom/vanity nameservers for all 3 CVG domains via Cloudflare API v4
#
# Domains:
#   cleargeo.tech            → ns1/ns2.cleargeo.tech
#   clearviewgeographic.com  → ns1/ns2.clearviewgeographic.com
#   cvg-nexus.com            → ns1/ns2.cvg-nexus.com
#
# Usage:
#   export CLOUDFLARE_API_TOKEN="your_token"
#   python cloudflare_dns_setup.py --action setup-all
#   python cloudflare_dns_setup.py --action ns-status
#   python cloudflare_dns_setup.py --action list-records --zone cleargeo.tech
#   python cloudflare_dns_setup.py --action add-record --zone cleargeo.tech --type A --name git --value 1.2.3.4
#   python cloudflare_dns_setup.py --action verify-propagation
#   python cloudflare_dns_setup.py --action import-zone --zone cleargeo.tech --file backup.txt
#
# (c) Clearview Geographic, LLC — Proprietary
# =============================================================================
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from typing import Any, Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CVG_DOMAINS = {
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

CF_API_BASE = "https://api.cloudflare.com/client/v4"

# ---------------------------------------------------------------------------
# Cloudflare API client (stdlib only — no requests dependency)
# ---------------------------------------------------------------------------

class CloudflareClient:
    """Minimal Cloudflare API v4 client using urllib (no external dependencies)."""

    def __init__(self, api_token: str):
        self.api_token = api_token
        self._zone_cache: dict[str, str] = {}  # domain → zone_id

    def _request(
        self,
        method: str,
        path: str,
        data: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict:
        url = f"{CF_API_BASE}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"

        body = json.dumps(data).encode("utf-8") if data else None
        req = Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "Content-Type": "application/json",
                "User-Agent": "CVG-Neuron-DNS-Setup/1.0",
            },
        )
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            try:
                err = json.loads(body_text)
            except Exception:
                err = {"error": body_text}
            print(f"[CF API ERROR] {method} {path} → HTTP {exc.code}")
            print(f"  {json.dumps(err.get('errors', err), indent=2)}")
            return {"success": False, "errors": err.get("errors", [str(exc)]), "result": None}
        except URLError as exc:
            print(f"[CF API NETWORK ERROR] {method} {path}: {exc}")
            return {"success": False, "errors": [str(exc)], "result": None}

    def get(self, path: str, params: Optional[dict] = None) -> dict:
        return self._request("GET", path, params=params)

    def post(self, path: str, data: dict) -> dict:
        return self._request("POST", path, data=data)

    def put(self, path: str, data: dict) -> dict:
        return self._request("PUT", path, data=data)

    def patch(self, path: str, data: dict) -> dict:
        return self._request("PATCH", path, data=data)

    def delete(self, path: str) -> dict:
        return self._request("DELETE", path)

    # -------------------------------------------------------------------------
    # Zone methods
    # -------------------------------------------------------------------------

    def get_zone_id(self, domain: str) -> Optional[str]:
        """Look up the Cloudflare zone ID for a domain."""
        if domain in self._zone_cache:
            return self._zone_cache[domain]
        resp = self.get("/zones", params={"name": domain, "status": "active"})
        if not resp.get("success") or not resp.get("result"):
            # Try pending status too
            resp = self.get("/zones", params={"name": domain})
        result = resp.get("result", [])
        if result:
            zone_id = result[0]["id"]
            self._zone_cache[domain] = zone_id
            return zone_id
        return None

    def list_zones(self) -> list[dict]:
        """List all zones in the account."""
        resp = self.get("/zones", params={"per_page": "50"})
        return resp.get("result", [])

    def get_zone_details(self, domain: str) -> Optional[dict]:
        """Get full zone details including NS and status."""
        resp = self.get("/zones", params={"name": domain})
        result = resp.get("result", [])
        return result[0] if result else None

    # -------------------------------------------------------------------------
    # Custom nameservers
    # -------------------------------------------------------------------------

    def get_custom_ns(self, zone_id: str) -> list[dict]:
        """Get custom nameserver config for a zone."""
        resp = self.get(f"/zones/{zone_id}/custom_ns")
        return resp.get("result", [])

    def set_custom_ns(self, zone_id: str, ns1: str, ns2: str) -> dict:
        """
        Set custom (vanity) nameservers for a zone.
        Returns the assigned Cloudflare IPs for glue records.
        """
        data = {
            "enabled": True,
            "ns_set": 1,
        }
        # The API path for zone-level custom NS
        resp = self.put(f"/zones/{zone_id}/custom_ns", data=data)
        return resp

    # -------------------------------------------------------------------------
    # DNS records
    # -------------------------------------------------------------------------

    def list_records(self, zone_id: str, rtype: Optional[str] = None) -> list[dict]:
        """List all DNS records in a zone."""
        params: dict[str, str] = {"per_page": "100"}
        if rtype:
            params["type"] = rtype
        resp = self.get(f"/zones/{zone_id}/dns_records", params=params)
        records = resp.get("result", [])
        # Handle pagination
        total = resp.get("result_info", {}).get("total_count", len(records))
        page = 2
        while len(records) < total:
            params["page"] = str(page)
            more = self.get(f"/zones/{zone_id}/dns_records", params=params)
            batch = more.get("result", [])
            if not batch:
                break
            records.extend(batch)
            page += 1
        return records

    def add_record(
        self,
        zone_id: str,
        rtype: str,
        name: str,
        content: str,
        ttl: int = 1,  # 1 = auto in Cloudflare
        proxied: bool = False,
        priority: Optional[int] = None,
    ) -> dict:
        """Add a DNS record to a zone."""
        data: dict[str, Any] = {
            "type": rtype.upper(),
            "name": name,
            "content": content,
            "ttl": ttl,
            "proxied": proxied,
        }
        if priority is not None:
            data["priority"] = priority
        return self.post(f"/zones/{zone_id}/dns_records", data=data)

    def delete_record(self, zone_id: str, record_id: str) -> dict:
        """Delete a DNS record by ID."""
        return self.delete(f"/zones/{zone_id}/dns_records/{record_id}")

    def find_record(self, zone_id: str, rtype: str, name: str) -> Optional[dict]:
        """Find a specific record by type and name."""
        records = self.list_records(zone_id, rtype=rtype)
        for r in records:
            if r.get("type") == rtype.upper() and r.get("name", "").lower() == name.lower():
                return r
        return None

    # -------------------------------------------------------------------------
    # Verify token
    # -------------------------------------------------------------------------

    def verify_token(self) -> bool:
        """Verify the API token is valid."""
        resp = self.get("/user/tokens/verify")
        ok = resp.get("success", False)
        if ok:
            status = resp.get("result", {}).get("status", "unknown")
            print(f"[CF] API Token valid — status: {status}")
        else:
            print(f"[CF] API Token INVALID: {resp.get('errors', 'unknown error')}")
        return ok


# ---------------------------------------------------------------------------
# Action implementations
# ---------------------------------------------------------------------------

def action_setup_all(cf: CloudflareClient) -> None:
    """
    Verify all 3 CVG zones are added to Cloudflare.
    Reports status and what needs to be done.
    """
    print("\n" + "=" * 60)
    print("  CVG CLOUDFLARE ZONE STATUS")
    print("=" * 60)

    for domain, cfg in CVG_DOMAINS.items():
        print(f"\n[{domain}]")
        details = cf.get_zone_details(domain)
        if not details:
            print(f"  STATUS: NOT FOUND in Cloudflare")
            print(f"  ACTION: Add {domain} to Cloudflare at https://dash.cloudflare.com")
            print(f"  ACTION: Import DNS records from HostGator backup")
            continue

        zone_id = details["id"]
        status  = details.get("status", "unknown")
        plan    = details.get("plan", {}).get("name", "unknown")
        ns      = details.get("name_servers", [])

        print(f"  Zone ID:  {zone_id}")
        print(f"  Status:   {status}")
        print(f"  Plan:     {plan}")
        print(f"  Current NS: {', '.join(ns)}")

        # Check if already using custom NS
        ns_lower = [n.lower() for n in ns]
        using_custom = any(cfg["ns1"].lower() in n or cfg["ns2"].lower() in n for n in ns_lower)
        if using_custom:
            print(f"  Custom NS: ACTIVE ({cfg['ns1']} / {cfg['ns2']})")
        else:
            print(f"  Custom NS: NOT YET CONFIGURED")
            print(f"  NEXT: In Cloudflare dashboard → {domain} → DNS → Custom Nameservers")
            print(f"        Add ns1: {cfg['ns1']}")
            print(f"        Add ns2: {cfg['ns2']}")

        # Record count
        zone_id_str = details["id"]
        records = cf.list_records(zone_id_str)
        print(f"  DNS Records: {len(records)} configured")


def action_ns_status(cf: CloudflareClient) -> None:
    """Show nameserver status for all 3 CVG domains."""
    print("\n" + "=" * 60)
    print("  CVG NAMESERVER STATUS")
    print("=" * 60)

    for domain, cfg in CVG_DOMAINS.items():
        print(f"\n[{domain}]")
        details = cf.get_zone_details(domain)
        if not details:
            print(f"  NOT in Cloudflare — add at https://dash.cloudflare.com")
            continue

        cf_ns = details.get("name_servers", [])
        print(f"  Cloudflare assigned NS: {', '.join(cf_ns)}")

        # Check what registrar is reporting via live DNS
        live_ns = _dig_ns(domain)
        print(f"  Live NS (from internet): {', '.join(live_ns) if live_ns else '(none/unavailable)'}")

        # Are we using custom NS?
        ns1_resolved = _resolve(cfg["ns1"])
        ns2_resolved = _resolve(cfg["ns2"])
        print(f"  {cfg['ns1']} resolves to: {ns1_resolved or '(UNRESOLVABLE — glue not registered yet)'}")
        print(f"  {cfg['ns2']} resolves to: {ns2_resolved or '(UNRESOLVABLE — glue not registered yet)'}")

        # Migration state
        live_lower = [n.lower().rstrip(".") for n in live_ns]
        if cfg["ns1"].lower() in live_lower or cfg["ns2"].lower() in live_lower:
            print(f"  MIGRATION STATE: COMPLETE - using {cfg['ns1']}")
        elif any("hostgator" in n for n in live_lower):
            print(f"  MIGRATION STATE: PENDING - still on HostGator NS")
        elif any("cloudflare" in n for n in live_lower):
            print(f"  MIGRATION STATE: ON CLOUDFLARE (generic NS, not yet custom)")
        else:
            print(f"  MIGRATION STATE: UNKNOWN")


def action_list_records(cf: CloudflareClient, domain: str) -> None:
    """List all DNS records for a domain."""
    zone_id = cf.get_zone_id(domain)
    if not zone_id:
        print(f"Zone not found in Cloudflare: {domain}")
        return

    records = cf.list_records(zone_id)
    if not records:
        print(f"No records found in {domain}")
        return

    print(f"\n{'TYPE':<8} {'NAME':<45} {'CONTENT':<40} {'TTL':<6} {'PROXY'}")
    print("-" * 110)
    for r in sorted(records, key=lambda x: (x.get("type", ""), x.get("name", ""))):
        rtype   = r.get("type", "")
        name    = r.get("name", "")
        content = r.get("content", "")
        ttl     = str(r.get("ttl", ""))
        proxied = "ON" if r.get("proxied") else "off"
        # Truncate long content
        if len(content) > 38:
            content = content[:35] + "..."
        print(f"{rtype:<8} {name:<45} {content:<40} {ttl:<6} {proxied}")

    print(f"\nTotal: {len(records)} records")


def action_add_record(
    cf: CloudflareClient,
    domain: str,
    rtype: str,
    name: str,
    value: str,
    ttl: int = 1,
    proxied: bool = False,
    priority: Optional[int] = None,
) -> None:
    """Add a single DNS record."""
    zone_id = cf.get_zone_id(domain)
    if not zone_id:
        print(f"Zone not found in Cloudflare: {domain}")
        return

    # Build FQDN if just a label was given
    fqdn = name if name.endswith(domain) or name == "@" else f"{name}.{domain}"

    print(f"Adding {rtype} record: {fqdn} → {value} (ttl={ttl}, proxied={proxied})")

    # Check for existing record
    existing = cf.find_record(zone_id, rtype, fqdn)
    if existing:
        print(f"  WARNING: Record already exists: {existing.get('content')}")
        ans = input("  Overwrite? (y/N): ").strip().lower()
        if ans != "y":
            print("  Skipped.")
            return
        cf.delete_record(zone_id, existing["id"])
        print(f"  Deleted old record.")

    resp = cf.add_record(
        zone_id, rtype=rtype, name=fqdn, content=value,
        ttl=ttl, proxied=proxied, priority=priority
    )
    if resp.get("success"):
        rec = resp.get("result", {})
        print(f"  Added: {rec.get('type')} {rec.get('name')} → {rec.get('content')}")
    else:
        print(f"  FAILED: {resp.get('errors', 'unknown')}")


def action_verify_propagation(cf: CloudflareClient) -> None:
    """Verify DNS propagation globally for all 3 domains."""
    print("\n" + "=" * 60)
    print("  PROPAGATION VERIFICATION")
    print("=" * 60)

    for domain, cfg in CVG_DOMAINS.items():
        print(f"\n[{domain}]")

        # Check NS records
        live_ns = _dig_ns(domain)
        expected_ns = {cfg["ns1"].lower(), cfg["ns2"].lower()}
        live_ns_clean = {n.lower().rstrip(".") for n in live_ns}

        if expected_ns.issubset(live_ns_clean):
            print(f"  NS: PROPAGATED ({cfg['ns1']} / {cfg['ns2']})")
        elif live_ns:
            print(f"  NS: NOT YET ({', '.join(live_ns)}) — expected {cfg['ns1']} / {cfg['ns2']}")
        else:
            print(f"  NS: UNRESOLVABLE")

        # Check A record
        a_ip = _resolve(domain)
        if a_ip:
            print(f"  A:  {domain} → {a_ip}")
        else:
            print(f"  A:  {domain} → UNRESOLVABLE")

        # Check NS names resolve (glue check)
        ns1_ip = _resolve(cfg["ns1"])
        ns2_ip = _resolve(cfg["ns2"])
        print(f"  Glue {cfg['ns1']}: {ns1_ip or 'NOT REGISTERED'}")
        print(f"  Glue {cfg['ns2']}: {ns2_ip or 'NOT REGISTERED'}")

    print("\n  Quick links:")
    for domain in CVG_DOMAINS:
        print(f"    https://dnschecker.org/#NS/{domain}")


def action_import_zone(cf: CloudflareClient, domain: str, backup_file: str) -> None:
    """
    Import DNS records from a dns_audit backup file into Cloudflare.
    Parses lines in standard BIND zone file format.
    """
    zone_id = cf.get_zone_id(domain)
    if not zone_id:
        print(f"Zone not found in Cloudflare: {domain}")
        sys.exit(1)

    import re
    try:
        with open(backup_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        print(f"File not found: {backup_file}")
        sys.exit(1)

    added = 0
    skipped = 0
    # Parse BIND-style: name TTL IN type content
    record_re = re.compile(
        r"^(\S+)\s+(\d+)\s+IN\s+(A|AAAA|CNAME|MX|TXT|NS|SRV|CAA)\s+(.+)$",
        re.IGNORECASE,
    )
    for line in lines:
        line = line.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        m = record_re.match(line)
        if not m:
            continue
        name, ttl, rtype, content = m.group(1), int(m.group(2)), m.group(3).upper(), m.group(4).strip()

        # Skip NS records — Cloudflare manages those
        if rtype == "NS":
            skipped += 1
            continue

        # MX: content is "priority target"
        priority = None
        if rtype == "MX":
            parts = content.split(None, 1)
            if len(parts) == 2:
                try:
                    priority = int(parts[0])
                    content = parts[1].rstrip(".")
                except ValueError:
                    pass

        # Remove trailing dot from hostnames
        if rtype in ("CNAME", "MX") and content.endswith("."):
            content = content[:-1]

        # Expand @ to domain
        if name == "@":
            name = domain
        elif not name.endswith(domain):
            name = f"{name}.{domain}"

        resp = cf.add_record(
            zone_id, rtype=rtype, name=name, content=content,
            ttl=min(ttl, 3600), priority=priority,
        )
        if resp.get("success"):
            added += 1
            print(f"  [{added}] Added {rtype} {name} → {content[:60]}")
        else:
            errs = resp.get("errors", [])
            err_msg = errs[0].get("message", str(errs)) if errs else "unknown"
            if "already exists" in str(err_msg).lower():
                skipped += 1
            else:
                print(f"  FAIL {rtype} {name}: {err_msg}")

    print(f"\nImport complete: {added} added, {skipped} skipped")


def action_add_cvg_service_records(cf: CloudflareClient, domain: str) -> None:
    """
    Add standard CVG service records to a zone.
    Prompts for IPs.
    """
    zone_id = cf.get_zone_id(domain)
    if not zone_id:
        print(f"Zone not found: {domain}")
        return

    print(f"\nAdding standard CVG service records to {domain}")
    print("(Press Enter to skip any record)")

    services = [
        ("A", "@", "Main IP (root domain)"),
        ("A", "www", "WWW"),
        ("A", "git", "Gitea Git server"),
        ("A", "neuron", "CVG Neuron AI"),
        ("MX", "@", "Mail server (enter: '10 mail.DOMAIN')"),
        ("TXT", "@", "SPF record (enter: 'v=spf1 ...')"),
    ]

    for rtype, name, desc in services:
        val = input(f"  {rtype} {name}.{domain} [{desc}]: ").strip()
        if not val:
            print(f"    Skipped.")
            continue

        fqdn = domain if name == "@" else f"{name}.{domain}"
        priority = None
        if rtype == "MX":
            parts = val.split(None, 1)
            if len(parts) == 2:
                try:
                    priority = int(parts[0])
                    val = parts[1]
                except ValueError:
                    pass

        resp = cf.add_record(zone_id, rtype=rtype, name=fqdn, content=val, priority=priority)
        if resp.get("success"):
            print(f"    Added.")
        else:
            print(f"    Failed: {resp.get('errors', '')}")


# ---------------------------------------------------------------------------
# DNS helpers (stdlib)
# ---------------------------------------------------------------------------

def _resolve(hostname: str) -> str:
    try:
        return socket.gethostbyname(hostname)
    except Exception:
        return ""


def _dig_ns(domain: str) -> list[str]:
    """Get NS records for a domain using nslookup (works on Windows)."""
    try:
        result = subprocess.run(
            ["nslookup", "-type=NS", domain],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        ns_list = []
        for line in result.stdout.splitlines():
            if "nameserver" in line.lower() and "=" in line:
                ns = line.split("=")[-1].strip()
                if ns:
                    ns_list.append(ns)
        return ns_list
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="CVG Cloudflare DNS Setup — manage custom nameservers & records",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Actions:
  setup-all          Check all 3 CVG zones and report what needs doing
  ns-status          Show current NS status and migration state
  list-records       List all DNS records for a zone
  add-record         Add a single DNS record
  verify-propagation Check global propagation for all 3 domains
  import-zone        Import records from a dns_audit backup file
  add-cvg-records    Interactively add standard CVG service records

Examples:
  python cloudflare_dns_setup.py --action setup-all
  python cloudflare_dns_setup.py --action list-records --zone cleargeo.tech
  python cloudflare_dns_setup.py --action add-record --zone cleargeo.tech --type A --name git --value 1.2.3.4
  python cloudflare_dns_setup.py --action import-zone --zone cleargeo.tech --file docs/dns_backup.txt
  python cloudflare_dns_setup.py --action verify-propagation
        """,
    )
    parser.add_argument(
        "--action", required=True,
        choices=["setup-all", "ns-status", "list-records", "add-record",
                 "verify-propagation", "import-zone", "add-cvg-records"],
        help="Action to perform",
    )
    parser.add_argument("--zone", help="Domain name (e.g. cleargeo.tech)")
    parser.add_argument("--type", dest="rtype", help="DNS record type (A, CNAME, MX, TXT...)")
    parser.add_argument("--name", help="Record name / subdomain")
    parser.add_argument("--value", help="Record content / value / IP")
    parser.add_argument("--ttl", type=int, default=1, help="TTL in seconds (1=auto)")
    parser.add_argument("--proxied", action="store_true", help="Enable Cloudflare proxy (orange cloud)")
    parser.add_argument("--priority", type=int, help="Priority (MX records)")
    parser.add_argument("--file", help="Backup file for import-zone action")
    parser.add_argument("--token", help="Cloudflare API token (overrides env var)")

    args = parser.parse_args()

    # Get API token
    api_token = args.token or os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not api_token:
        print("ERROR: Cloudflare API token required.")
        print("  Set env var:  export CLOUDFLARE_API_TOKEN=your_token")
        print("  Or use flag:  --token your_token")
        print()
        print("  Get token at: https://dash.cloudflare.com/profile/api-tokens")
        print("  Required permissions: Zone:DNS:Edit, Zone:Zone:Read")
        sys.exit(1)

    cf = CloudflareClient(api_token)

    # Verify token first
    print("Verifying Cloudflare API token...")
    if not cf.verify_token():
        sys.exit(1)

    # Dispatch action
    if args.action == "setup-all":
        action_setup_all(cf)

    elif args.action == "ns-status":
        action_ns_status(cf)

    elif args.action == "list-records":
        if not args.zone:
            print("--zone required for list-records")
            sys.exit(1)
        action_list_records(cf, args.zone)

    elif args.action == "add-record":
        if not all([args.zone, args.rtype, args.name, args.value]):
            print("--zone, --type, --name, --value all required for add-record")
            sys.exit(1)
        action_add_record(
            cf, args.zone, args.rtype, args.name, args.value,
            ttl=args.ttl, proxied=args.proxied, priority=args.priority,
        )

    elif args.action == "verify-propagation":
        action_verify_propagation(cf)

    elif args.action == "import-zone":
        if not all([args.zone, args.file]):
            print("--zone and --file required for import-zone")
            sys.exit(1)
        action_import_zone(cf, args.zone, args.file)

    elif args.action == "add-cvg-records":
        if not args.zone:
            print("--zone required for add-cvg-records")
            sys.exit(1)
        action_add_cvg_service_records(cf, args.zone)


if __name__ == "__main__":
    main()
