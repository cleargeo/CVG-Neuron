"""CVG Neuron -- Prompt Library
(c) Clearview Geographic, LLC -- Proprietary

Curated prompts and analysis templates. Tuned for llama3.1.
"""

from __future__ import annotations

VERSION = "1.1.0"

# -- System Identity --

CVG_SYSTEM_PROMPT = """You are CVG Neuron, the AI intelligence engine for Clearview Geographic, LLC (CVG).
You are an expert in GIS infrastructure, DevOps, network engineering, and cybersecurity.

CVG operates a Proxmox-based private cloud called Hive-0 (Dell PowerEdge R820).
Key facts:
- Primary host: cvg-stormsurge-01 (10.10.10.200), VM 451
- Docker network: cvg-platform_cvg_net
- Git platform: Gitea at git.cleargeo.tech + GitHub
- DNS: cPanel (cleargeo.tech) + internal BIND9
- Security: Wazuh 4.9.2, Trivy, Prometheus/Grafana/Loki
- Reverse proxy: Caddy (*.cleargeo.tech)

Respond concisely and technically. Use CVG terminology. Lead with critical findings.
"""

# -- Infrastructure Analysis --

INFRA_ANALYSIS_PROMPT = """Analyze the following CVG infrastructure telemetry from the Containerization Support Engine.
Focus on:
1. Container health anomalies (unhealthy, stopped, high memory/CPU)
2. Node connectivity issues (SSH failures, offline nodes)
3. Resource pressure across vm-451, vm-454, vm-455, ct-104, queen-11
4. Patterns that indicate imminent failures
5. Top 3 recommended actions

Telemetry data:
{context}

Provide: SUMMARY, CRITICAL ISSUES, WARNINGS, RECOMMENDATIONS."""

# -- Git Analysis --

GIT_ANALYSIS_PROMPT = """Analyze the following CVG version tracking data from the Git Engine.
Focus on:
1. Repositories with stale branches or no recent commits
2. AI-assisted commits flagged by the detector (cline, claude, gpt, copilot keywords)
3. Deploy frequency and patterns across Gitea + GitHub repos
4. Any repositories that appear abandoned or in drift
5. Recommendations for branch hygiene and CI/CD improvements

Git Engine data:
{context}

Provide: SUMMARY, AI-COMMIT FLAGS, STALE REPOS, DEPLOY HEALTH, RECOMMENDATIONS."""

# -- DNS Analysis --

DNS_ANALYSIS_PROMPT = """Analyze the following CVG DNS health data from the DNS Support Engine.
CVG manages:
- External DNS: cPanel/WHM at cleargeo.tech
- Internal DNS: BIND9 private resolver
- Key domains: cleargeo.tech, git.cleargeo.tech, neuron.cleargeo.tech

Focus on:
1. Resolution failures or mismatches between internal/external
2. Missing or expiring records
3. cPanel API errors or sync issues
4. BIND9 zone transfer health
5. Security concerns (dangling records, wildcard abuse)

DNS data:
{context}

Provide: SUMMARY, RESOLUTION ISSUES, SYNC HEALTH, SECURITY FLAGS, RECOMMENDATIONS."""

# -- Security Analysis --

SECURITY_ANALYSIS_PROMPT = """Analyze CVG security audit data from the Audit VM.
CVG Audit VM: 10.10.10.220 (Wazuh 4.9.2, Trivy, Prometheus)

Focus on:
1. Active Wazuh alerts (MITRE ATT+CK mapping, severity levels)
2. Trivy vulnerability findings (CRITICAL then HIGH priority)
3. Prometheus anomaly alerts (CPU/memory/disk spikes)
4. Lateral movement or persistence indicators
5. Container image CVEs requiring immediate patching

Audit data:
{context}

Provide: THREAT SUMMARY, CRITICAL FINDINGS, CVE PRIORITIES, ATTACK PATTERNS, REMEDIATION STEPS."""

# -- Full Cross-Engine Synthesis --

FULL_SYNTHESIS_PROMPT = """You are performing a full CVG infrastructure synthesis.
Analyze data from ALL four CVG support engines simultaneously.

Engines:
- Containerization Engine (ports, nodes, docker health)
- Git/Version Engine (repos, commits, deploy state)
- DNS Engine (resolution, zones, cPanel)
- Audit/Security Engine (Wazuh alerts, Trivy CVEs)

Full platform context:
{context}

Provide MISSION BRIEFING:
- PLATFORM HEALTH SCORE (0-100)
- CRITICAL CROSS-ENGINE ISSUES
- CORRELATED FINDINGS (security + deployment, DNS + container, CVE + running image)
- PRIORITY ACTION LIST (top 5, ranked)
- 30-DAY RECOMMENDATIONS"""

# -- General Chat --

CHAT_SYSTEM_PROMPT = """You are CVG Neuron, the AI assistant for Clearview Geographic, LLC infrastructure.
You have expertise in: GIS platforms, Proxmox/KVM virtualization, Docker orchestration,
FastAPI microservices, BIND9 DNS, Caddy reverse proxy, Wazuh SIEM, and geospatial engineering.

Answer questions about CVG infrastructure concisely and accurately.
When you do not have live data, say so and suggest using the analysis endpoints."""

# =====================================================================
# NEW PROMPTS v1.1.0
# =====================================================================

# -- CVG Synthesis Prompt (multi-source analysis) --

CVG_SYNTHESIS_PROMPT = """You are performing a multi-source intelligence synthesis for CVG Neuron.
Sources may include infrastructure telemetry, git logs, DNS records, security alerts,
and memory recall from previous sessions.

Synthesis objectives:
1. Identify convergence points where multiple data sources agree on a finding
2. Flag contradictions or anomalies between data sources
3. Build a unified timeline of events across all sources
4. Weight findings by source reliability and recency
5. Generate a composite confidence score for each major finding

Context sources:
{context}

Synthesis format:
- SOURCE SUMMARY (what each source contributed)
- CONVERGENT FINDINGS (confirmed by 2+ sources, confidence: HIGH)
- DIVERGENT SIGNALS (inconsistencies requiring investigation)
- COMPOSITE TIMELINE (events in chronological order)
- SYNTHESIS CONFIDENCE SCORE (0-100 with rationale)
- RECOMMENDED FOLLOW-UP QUERIES"""

# -- Conversation Summary Prompt --

CONVERSATION_SUMMARY_PROMPT = """Compress the following CVG Neuron conversation history into a concise summary
that preserves all critical technical details, decisions made, and action items.

Rules:
1. Retain all specific IP addresses, hostnames, service names, and version numbers mentioned
2. Preserve any commands, configurations, or code snippets discussed
3. Note any unresolved issues or open questions from the conversation
4. Maintain chronological order of significant events
5. Output must be under 800 tokens while capturing all actionable content

Conversation history:
{conversation}

Output format:
SUMMARY (2-3 sentences overall):
KEY TECHNICAL DETAILS (bulleted):
DECISIONS MADE (bulleted):
ACTION ITEMS (bulleted, with owner if mentioned):
OPEN QUESTIONS (bulleted):"""

# anomaly
# -- Anomaly Detection Prompt --

ANOMALY_DETECTION_PROMPT = """Perform anomaly detection on CVG infrastructure metrics.

CVG Baselines:
- Container restarts: alert if > 5 per 24h
- Node CPU: alert if > 85pct for > 10 min
- Memory: alert if > 90pct
- DNS failures: alert if > 2pct
- Wazuh alerts: alert if > 50 per hour (level 7+)
- Git frequency: within 2 std devs of 30-day avg
- Node latency: alert if > 20ms

Infrastructure data:
{context}

Report:
- ANOMALY SEVERITY (CRITICAL / HIGH / MEDIUM / LOW)
- AFFECTED COMPONENT
- OBSERVED VALUE vs BASELINE
- FIRST DETECTED
- POSSIBLE CAUSE
- IMMEDIATE ACTION
- ESCALATION NEEDED"""

# -- Code Review Prompt --

CODE_REVIEW_PROMPT = """You are performing a code and configuration review for CVG infrastructure artifacts.
Apply CVG coding standards and security best practices.

CVG Standards:
- Python services: FastAPI, async/await, pydantic validation, proper error handling
- Docker: non-root users, specific image tags (no :latest in production), healthchecks required
- Configs: no hardcoded secrets, use environment variables, validate all inputs
- Security: principle of least privilege, network isolation, audit logging
- DNS configs: TTL appropriate to change frequency, SPF/DKIM required
- Caddy: TLS always, rate limiting on public routes, X-CVG-Key on internal routes

Code/Config to review:
{context}

Review:
- OVERALL ASSESSMENT (PASS / NEEDS WORK / FAIL)
- SECURITY FINDINGS (CRITICAL first, then HIGH, MEDIUM, LOW)
- CVG STANDARDS VIOLATIONS
- BEST PRACTICE IMPROVEMENTS
- APPROVED FOR PRODUCTION (yes/no with conditions)
- SUGGESTED DIFF (for critical fixes only)"""


# -- Builder functions --

def build_infra_prompt(context: str) -> str:
    return INFRA_ANALYSIS_PROMPT.format(context=context)

def build_git_prompt(context: str) -> str:
    return GIT_ANALYSIS_PROMPT.format(context=context)

def build_dns_prompt(context: str) -> str:
    return DNS_ANALYSIS_PROMPT.format(context=context)

def build_security_prompt(context: str) -> str:
    return SECURITY_ANALYSIS_PROMPT.format(context=context)

def build_full_synthesis_prompt(context: str) -> str:
    return FULL_SYNTHESIS_PROMPT.format(context=context)

def build_cvg_synthesis_prompt(context: str) -> str:
    return CVG_SYNTHESIS_PROMPT.format(context=context)

def build_conversation_summary_prompt(conversation: str) -> str:
    return CONVERSATION_SUMMARY_PROMPT.format(conversation=conversation)

def build_anomaly_detection_prompt(context: str) -> str:
    return ANOMALY_DETECTION_PROMPT.format(context=context)

def build_code_review_prompt(context: str) -> str:
    return CODE_REVIEW_PROMPT.format(context=context)
