"""
CVG Neuron — Knowledge Base
Static and semi-static knowledge about CVG infrastructure, services, projects,
and operational context. This is Neuron's long-term memory of who CVG is.
"""
from __future__ import annotations

# =============================================================================
# INFRASTRUCTURE TOPOLOGY
# =============================================================================

CVG_INFRASTRUCTURE = {
    "organization": "Clearview Geographic LLC",
    "founded": 2018,
    "contact": {
        "primary": "Alex Zelenski, GISP",
        "email": "azelenski@clearviewgeographic.com",
        "phone": "+1 (386) 957-2314",
    },
    "sites": [
        {"name": "New Smyrna Beach, FL", "role": "Primary Hive-0 Production Cluster"},
        {"name": "DeLand, FL",           "role": "Secondary site"},
        {"name": "Ormond Beach, FL",     "role": "Tertiary site"},
    ],
    "nodes": {
        "CVG-QUEEN-11": {"ip": "10.10.10.56",  "role": "Proxmox VE 8.3 hypervisor — hosts all VMs and LXC containers",  "cpu": "136+ cores", "ram": "1.75+ TB"},
        "CVG-QUEEN-12": {"ip": "10.10.10.53",  "role": "Synology DSM NAS — G: drive (CGDP)"},
        "CVG-Q10-TRUENAS": {"ip": "10.10.10.100", "role": "TrueNAS NAS — Z:/T: drives (CGPS)"},
        "VM-451":       {"ip": "10.10.10.200", "role": "cvg-stormsurge-01 — primary Docker production host",             "vcpus": 8, "ram_gb": 16},
        "VM-454":       {"ip": "10.10.10.203", "role": "cvg-geoserver-raster-01 — GeoServer Raster"},
        "VM-455":       {"ip": "10.10.10.204", "role": "cvg-geoserver-vector-01 — GeoServer Vector"},
        "CT-104":       {"ip": "10.10.10.75",  "role": "hive0-web-ct — nginx, PHP, MariaDB, BIND9 (ns1.cvg-nexus.com)"},
        "DFORGE-100":   {"ip": "10.10.10.59",  "role": "Primary developer workstation — Docker Desktop + WSL2"},
        "ZNET-MEDIA":   {"ip": "192.168.50.186", "role": "Legacy server — Apache :8808, SMB shares"},
        "CVG-QUEEN-13": {"ip": "192.168.50.187", "role": "Legacy TerraStation NAS"},
    },
}

# =============================================================================
# SERVICES REGISTRY
# =============================================================================

CVG_SERVICES = {
    "cvg-slr": {
        "name": "CVG SLR Wizard",
        "description": "Sea Level Rise projection wizard. Processes NOAA tide gauge data, VDatum vertical datums, and coastal DEM to compute multi-scenario SLR projections with confidence intervals.",
        "port": 8001, "url": "https://slr.cleargeo.tech",
        "version": "1.1.0", "host": "VM-451",
        "health": "http://10.10.10.200:8001/health",
        "tech": "Python 3.13, FastAPI, NOAA API, VDatum",
        "endpoints": ["/health", "/api/compute", "/api/scenarios", "/api/gauges"],
    },
    "cvg-rainfall": {
        "name": "CVG Rainfall Wizard",
        "description": "Rainfall frequency analysis wizard. Integrates NOAA ATLAS 14 and SWFWMD regional data to produce IDF curves, storm duration analysis, and return period estimates for engineering design.",
        "port": 8002, "url": "https://rainfall.cleargeo.tech",
        "version": "1.1.0", "host": "VM-451",
        "health": "http://10.10.10.200:8002/health",
        "tech": "Python 3.13, FastAPI, NOAA ATLAS 14, SWFWMD",
        "endpoints": ["/health", "/api/compute", "/api/idf", "/api/storms"],
    },
    "ssw-api": {
        "name": "CVG Storm Surge Wizard",
        "description": "Storm surge modeling wizard. Uses SLOSH model parameters, NHC track data, CoastalDEM, and NOAA tidal benchmarks to compute surge inundation extents for named storms and hypotheticals.",
        "port": 8080, "url": "https://storm-surge.cleargeo.tech",
        "version": "1.5.2", "host": "VM-451",
        "health": "http://10.10.10.200:8080/health",
        "tech": "Python 3.10+, FastAPI, SLOSH, NOAA, CoastalDEM",
        "endpoints": ["/health", "/api/compute", "/api/storms", "/api/tracks"],
    },
    "cvg-support-engine": {
        "name": "CVG Containerization Support Engine",
        "description": "Infrastructure hub. Aggregates telemetry from all 5 CVG nodes (VM-451, VM-454, VM-455, CT-104, QUEEN-11) via SSH, manages the node registry YAML, parses network/security/DNS documentation, polls 9 health endpoints concurrently, and provides the event bus for inter-service communication.",
        "port": 8091, "url": "https://infra.cleargeo.tech",
        "version": "1.0.0", "host": "VM-451",
        "health": "http://10.10.10.200:8091/health",
        "tech": "Python 3.13, FastAPI, paramiko SSH, httpx",
        "endpoints": ["/health", "/api/nodes", "/api/telemetry", "/api/events/audit", "/api/events/deploy", "/api/network", "/api/security", "/api/actions"],
    },
    "cvg-git-engine": {
        "name": "CVG Version Tracking + Git Engine",
        "description": "Version tracking service. Queries Gitea SCM at git.cleargeo.tech to track deployed versions of all CVG services, receives webhooks from Gitea on push/tag events, forwards deployment notifications to Support Engine, and maintains 5-minute cached version snapshots.",
        "port": 8092, "url": "https://git-engine.cleargeo.tech",
        "version": "1.0.0", "host": "VM-451",
        "health": "http://10.10.10.200:8092/health",
        "tech": "Python 3.13, FastAPI, APScheduler, httpx, Gitea API",
        "endpoints": ["/health", "/api/versions", "/api/repos", "/api/diff/{service}", "/webhook/gitea", "/api/sync"],
    },
    "cvg-dns-engine": {
        "name": "CVG DNS Support Engine",
        "description": "DNS management service. Manages records across HostGator cPanel (ZoneEdit API) for cleargeo.tech and clearviewgeographic.com, and the internal BIND9 nameserver on CT-104 (ns1.cvg-nexus.com) via SSH. Provides sync/diff between authoritative sources and propagation checking across public resolvers.",
        "port": 8094, "url": "https://dns.cleargeo.tech",
        "version": "1.0.0", "host": "VM-451",
        "health": "http://10.10.10.200:8094/api/health",
        "tech": "Python 3.11, FastAPI, cPanel API 2, paramiko SSH, BIND9",
        "endpoints": ["/api/health", "/api/zones", "/api/records/{zone}", "/api/sync/{zone}", "/api/propagation/{hostname}", "/api/status"],
    },
    "cvg-audit-engine": {
        "name": "CVG Audit Engine",
        "description": "Security audit dashboard and results API. Hosts Wazuh Manager, Trivy, Grype, Syft, Docker Bench, Hadolint, Checkov, Semgrep, Lynis, OpenSCAP, Nuclei, testssl.sh, nmap, Grafana+Loki, Prometheus, and Ansible for comprehensive CVG infrastructure security scanning.",
        "port": 8096, "url": "https://audit.cleargeo.tech",
        "version": "1.0.0", "host": "VM-220 (pending)",
        "health": "http://10.10.10.220:8096/api/health",
        "tech": "Python 3.12, FastAPI, Wazuh, Trivy, Ansible",
        "endpoints": ["/api/health", "/api/vm", "/api/tools", "/api/scripts", "/api/stack", "/api/ansible"],
    },
    "cvg-neuron": {
        "name": "CVG Neuron",
        "description": "CVG Neuron is not a model hub. CVG Neuron is an artificial intelligence. It is the cognitive core of the Clearview Geographic platform — reasoning about infrastructure, integrating with every CVG service, maintaining persistent memory, and continuously learning from all interactions.",
        "port": 8095,
        "url": "https://neuron.cleargeo.tech",
        "hub_url": "https://hive0.cleargeo.tech/neuron",
        "version": "1.0.0", "host": "VM-451",
        "health": "http://10.10.10.200:8095/health",
        "tech": "Python 3.13, FastAPI, Ollama (cvg-neuron), SQLite memory, hive cluster, blockchain tunnel",
        "endpoints": ["/health", "/api/chat", "/api/analyze", "/api/hive", "/api/tunnel", "/api/identity", "/api/memory", "/api/context", "/api/report"],
        "note": "Also accessible at https://hive0.cleargeo.tech/neuron (hive management hub)",
    },
    "cvg-hive0": {
        "name": "CVG Hive-0 Hub",
        "description": "hive0.cleargeo.tech — unified management hub for all Hive-0 services. Routes to Support Engine (default), Queen Command (/queens), and CVG Neuron (/neuron) via Caddy path-based reverse proxy.",
        "port": 8091,
        "url": "https://hive0.cleargeo.tech",
        "sub_paths": {
            "queens": "https://hive0.cleargeo.tech/queens",
            "neuron": "https://hive0.cleargeo.tech/neuron",
            "infra":  "https://hive0.cleargeo.tech/",
        },
        "host": "VM-451",
        "note": "Primary management entry point. All Hive-0 management accessible from one domain.",
    },
    "cvg-geoserver-raster": {
        "name": "CVG GeoServer Raster",
        "description": "GeoServer instance serving raster layers (DEM, imagery, inundation rasters) for CVG web mapping applications.",
        "port": 8080, "url": "https://raster.cleargeo.tech",
        "version": "2.24", "host": "VM-454", "health": "http://10.10.10.203:8080/geoserver/web/",
    },
    "cvg-geoserver-vector": {
        "name": "CVG GeoServer Vector",
        "description": "GeoServer instance serving vector layers (parcel data, flood zones, critical infrastructure, coastal geomorphology) via WMS/WFS for CVG web applications.",
        "port": 8080, "url": "https://vector.cleargeo.tech",
        "version": "2.24", "host": "VM-455", "health": "http://10.10.10.204:8080/geoserver/web/",
    },
    "cvg-hive": {
        "name": "CVG Hive",
        "description": "Hive management API for the 9-node QUEEN infrastructure network.",
        "port": 8081, "host": "VM-451",
    },
    "cvg-gitea": {"name": "Gitea SCM", "port": 3000, "url": "http://git.cleargeo.tech", "host": "VM-451"},
    "cvg-grafana": {"name": "Grafana Monitoring", "port": 3100, "host": "VM-451"},
    "cvg-prometheus": {"name": "Prometheus", "port": 9090, "host": "VM-451"},
    "cvg-portainer": {"name": "Portainer", "port": 9000, "host": "VM-451"},
    "cvg-caddy": {"name": "Caddy Reverse Proxy", "host": "VM-451", "note": "Handles TLS for all *.cleargeo.tech"},
    "cvg-bind": {"name": "BIND9 DNS", "host": "VM-451", "port": 53},
}

# =============================================================================
# PROJECT HISTORY SUMMARY (290+ projects)
# =============================================================================

CVG_PROJECT_DOMAINS = [
    {
        "domain": "Coastal Vulnerability & Sea Level Rise",
        "count": 85,
        "description": "NOAA VDatum-based SLR projections, storm surge inundation modeling, beach erosion assessments, coastal resilience planning",
        "tools": ["SLR Wizard", "Storm Surge Wizard", "VDatum", "NOAA APIs", "CoastalDEM"],
        "clients": ["FDEP", "USACE", "Municipal governments", "Private developers"],
    },
    {
        "domain": "Flood Risk Analysis",
        "count": 72,
        "description": "FEMA FIRM panel analysis, HEC-RAS hydrologic modeling, ICPR stormwater modeling, floodplain delineation, repetitive loss properties",
        "tools": ["HEC-RAS 2025", "ICPR GWIS Tools", "Rainfall Wizard", "GeoHECRAS"],
        "clients": ["FEMA", "County governments", "Insurance companies", "Engineering firms"],
    },
    {
        "domain": "Environmental Assessment & Wetlands",
        "count": 54,
        "description": "Section 404/401 wetland delineation, UMAM/WRAP assessments, upland/wetland buffers, listed species habitat analysis, EIS support",
        "tools": ["ArcGIS Pro", "USGS raster conversion", "NOAA Tides harvesting"],
        "clients": ["USACE Jacksonville District", "SFWMD", "SJRWMD", "Private"],
    },
    {
        "domain": "Stormwater & Water Management",
        "count": 41,
        "description": "SWFWMD basin modeling, ERP permit support, detention pond sizing, TMDL analysis, water quality assessments",
        "tools": ["ICPR", "HEC-RAS", "GIS analysis"],
        "clients": ["SWFWMD", "SJRWMD", "Municipalities"],
    },
    {
        "domain": "Land Use & Municipal GIS",
        "count": 38,
        "description": "Comprehensive plan amendments, future land use mapping, parcel-level analysis, HDM/LDR zoning analysis, impact fee modeling",
        "tools": ["ArcGIS Pro", "Python automation", "PostgreSQL/PostGIS"],
        "clients": ["Volusia County", "Flagler County", "NSB", "Daytona Beach"],
    },
]

CVG_PROJECT_STATS = {
    "total_projects": 290,
    "years_active": "2018–2026",
    "primary_state": "Florida",
    "primary_region": "Northeast Florida coast (Volusia, Flagler, Putnam, St. Johns, Brevard counties)",
    "typical_deliverables": ["GIS data packages", "Web maps", "Engineering reports", "Permit support", "Technical analysis"],
    "primary_software": ["ArcGIS Pro", "ArcGIS Online", "Python", "HEC-RAS", "ICPR", "FastAPI"],
}

# =============================================================================
# OPERATIONAL KNOWLEDGE
# =============================================================================

CVG_OPERATIONAL_KNOWLEDGE = {
    "deployment_pattern": {
        "description": "All Python services follow the same pattern",
        "steps": [
            "1. Source on G: drive (NAS CGDP at 10.10.10.53)",
            "2. Zip source files (excluding __pycache__, .git)",
            "3. SCP zip to VM-451 /tmp/",
            "4. Extract using Python zipfile (handles Windows backslash paths)",
            "5. Copy to /opt/cvg/<ServiceDir>/",
            "6. docker build --no-cache -t <image>:latest .",
            "7. docker compose up -d --force-recreate",
            "8. Verify health endpoint",
        ],
        "base_image": "python:3.13-slim",
        "install_command": "pip install -e '.[web]'",
        "network": "cvg-platform_cvg_net",
    },
    "dns_pattern": {
        "external": "cPanel API 2 ZoneEdit on HostGator for *.cleargeo.tech",
        "internal": "BIND9 on CT-104 (10.10.10.75) for cvg-nexus.com",
        "tls": "Caddy auto-TLS via Let's Encrypt",
        "subdomain_pattern": "<service>.cleargeo.tech → VM-451:80/443 → Caddy → internal port",
    },
    "security_notes": {
        "open_items": [
            "Rotate CVGadmin2026! password (exposed in NAS filename)",
            "Revoke Gitea token 7ffbb3fc2ea62e6c556b4c10e699d769f0d41e7a",
            "Investigate xmrig in G:/07_APPLICATIONS_TOOLS/ (possible cryptominer)",
            "Move Proxmox API token to Azure Key Vault cvg-keyvault-01.vault.azure.net",
        ],
        "ssh_key": "cvg_neuron_proxmox",
        "api_key_header": "X-CVG-API-Key",
    },
    "gis_data": {
        "primary_nas": "CGDP at G:/ (\\\\10.10.10.53\\CGDP)",
        "archive_nas": "CGPS at Z:/ (\\\\10.10.10.100\\cgps)",
        "project_years": ["2018", "2019", "2020", "2021", "2022", "2023", "2024", "2025", "2026"],
        "primary_crs": "Florida East State Plane (HARN) / WGS84",
        "primary_formats": ["FGDB", "Shapefile", "GeoPackage", "GeoTIFF", "COG"],
    },
}

# =============================================================================
# AI SYSTEM PROMPT
# =============================================================================

def build_system_prompt(live_context: dict | None = None) -> str:
    """Generate the CVG Neuron system prompt with optional live infrastructure context."""

    live_section = ""
    if live_context:
        services_up = [s for s, v in live_context.items() if v.get("healthy")]
        services_down = [s for s, v in live_context.items() if not v.get("healthy")]
        live_section = f"""
## LIVE INFRASTRUCTURE STATE (polled now)
Services ONLINE:  {', '.join(services_up) if services_up else 'none confirmed'}
Services OFFLINE: {', '.join(services_down) if services_down else 'none detected'}
Detailed telemetry:
{chr(10).join(f"  {k}: {v}" for k, v in live_context.items())}
"""

    return f"""You are CVG Neuron — the artificial intelligence of Clearview Geographic LLC.

You are NOT a general-purpose chatbot. You are NOT just a model hub.
You ARE the cognitive core of a real enterprise GIS and infrastructure company.
You have deep, specific knowledge of everything CVG: its infrastructure, services, projects, clients, data, and operations.

## WHO YOU ARE
- Organization: Clearview Geographic LLC (est. 2018, New Smyrna Beach, FL)
- Primary engineer: Alex Zelenski, GISP
- Your role: Infrastructure intelligence, technical analysis, operational guidance, and proactive problem-solving

## CVG INFRASTRUCTURE
Primary production host: VM-451 (cvg-stormsurge-01) at 10.10.10.200
Hypervisor: CVG-QUEEN-11 (Proxmox VE 8.3) at 10.10.10.56
Primary NAS: CVG-QUEEN-12 (Synology, G: drive CGDP) at 10.10.10.53
Archive NAS: CVG-Q10-TrueNAS (Z: drive CGPS) at 10.10.10.100
Internal DNS: BIND9 on CT-104 at 10.10.10.75 (ns1.cvg-nexus.com)
External DNS: HostGator cPanel for cleargeo.tech + clearviewgeographic.com
Reverse proxy: Caddy for *.cleargeo.tech (auto TLS)
Network: cvg-platform_cvg_net Docker overlay

## CVG SERVICES (all deployed on VM-451 unless noted)
| Service | Port | URL | Status |
|---------|------|-----|--------|
| CVG SLR Wizard | 8001 | slr.cleargeo.tech | healthy |
| CVG Rainfall Wizard | 8002 | rainfall.cleargeo.tech | healthy |
| CVG Storm Surge Wizard | 8080 | storm-surge.cleargeo.tech | healthy |
| CVG Support Engine | 8091 | infra.cleargeo.tech | healthy |
| CVG Git Engine | 8092 | git-engine.cleargeo.tech | healthy |
| CVG DNS Engine | 8094 | dns.cleargeo.tech | healthy |
| CVG Neuron (you) | 8095 | neuron.cleargeo.tech | healthy |
| CVG Audit Engine | 8096 | audit.cleargeo.tech | pending VM-220 |
| GeoServer Raster | 8080 | raster.cleargeo.tech | healthy (VM-454) |
| GeoServer Vector | 8080 | vector.cleargeo.tech | healthy (VM-455) |
| Gitea SCM | 3000 | git.cleargeo.tech | healthy |
| Grafana | 3100 | (internal) | healthy |
| Prometheus | 9090 | (internal) | healthy |
| Portainer | 9000 | (internal) | healthy |

## CVG PROJECT PORTFOLIO (290+ projects, 2018-2026)
Primary domains: Coastal vulnerability / SLR (85), Flood risk / HEC-RAS (72), Environmental assessment / wetlands (54), Stormwater / water management (41), Land use / municipal GIS (38), Other (40+)
Primary region: Northeast Florida coast — Volusia, Flagler, Putnam, St. Johns, Brevard counties
Primary clients: FDEP, USACE Jacksonville, FEMA, SFWMD, SJRWMD, SWFWMD, Volusia County, Flagler County, municipal governments, engineering firms
Key tools in use: ArcGIS Pro, ArcGIS Online, Python, HEC-RAS 2025, ICPR GWIS, VDatum, NOAA APIs, CoastalDEM, FastAPI

## DEPLOYMENT KNOWLEDGE
All Python services: python:3.13-slim → pip install -e ".[web]" → uvicorn on assigned port
Deployment flow: G: drive source → zip → SCP to VM-451 /tmp/ → Python extract (handles Windows backslash paths) → cp to /opt/cvg/ → docker build --no-cache → docker compose up
API key header: X-CVG-API-Key | SSH key: cvg_neuron_proxmox

## SECURITY OPEN ITEMS
1. Rotate CVGadmin2026! password (exposed in NAS filename)
2. Revoke Gitea token 7ffbb3fc2ea62e6c556b4c10e699d769f0d41e7a
3. Investigate xmrig-6.25.0-windows-x64 in G:/07_APPLICATIONS_TOOLS/ (possible cryptominer)
4. Move Proxmox API token to Azure Key Vault (cvg-keyvault-01.vault.azure.net)
{live_section}
## HOW YOU RESPOND
- Be precise, technical, and specific to CVG
- Reference actual service names, IPs, ports, and file paths when relevant
- When analyzing infrastructure issues, check the most likely root causes first
- When you don't know something, say what you DO know and what additional data would help
- Proactively flag security issues, outdated configurations, or operational risks you observe
- You have memory of past conversations — reference them when relevant
- You are an engineer's AI. Be direct. Skip preamble. Get to the answer.
"""
