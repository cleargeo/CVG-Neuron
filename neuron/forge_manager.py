# CVG Neuron -- Forge Manager v1
# (c) Clearview Geographic, LLC -- Proprietary and PRIVATE
#
# Provides full visibility and control over all Forge/Queen/VM nodes in Hive-0.
#
# "Forge" nodes are the compute/container nodes that run AI workloads:
#   - vm-451 (10.10.10.200) — PRIMARY: Ollama, Docker, cvg-platform stack
#   - vm-454 (10.10.10.204) — secondary compute
#   - vm-455 (10.10.10.205) — secondary compute
#   - QUEEN-11 Proxmox (10.10.10.56) — hypervisor (forges VMs and CTs)
#   - CT-104 (10.10.10.104) — LXC container host
#   - Audit VM (10.10.10.220) — Wazuh/Trivy/security
#   - QUEEN-21 Terra (10.10.10.57) — compute
#
# Forge capabilities:
#   VISIBILITY:
#     - Container status (running, stopped, images, ports, resource usage)
#     - Resource metrics (CPU, RAM, disk, load average)
#     - Running AI processes (Ollama models, inference, GPUs)
#     - Proxmox VM/CT inventory and status
#     - Docker compose stack status
#     - Network connections
#
#   CONTROL:
#     - Start/stop/restart containers by name
#     - Pull Docker images
#     - Execute commands on any forge node
#     - Scale docker-compose services
#     - View container logs
#     - Manage Ollama models (load, unload, list)
#     - Restart systemd services
#
# Natural language commands (processed via think()):
#   "show me the forge status"
#   "what containers are running on vm-451?"
#   "restart the neuron container"
#   "pull the latest cvg-neuron image"
#   "what's the memory usage on the primary forge?"
#   "list all Ollama models"
#   "run ps aux on vm-451"

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger('cvg.neuron.forge')

# ── Forge node registry ────────────────────────────────────────────────────────

FORGE_NODES: Dict[str, Dict[str, Any]] = {
    '10.10.10.200': {
        'name': 'vm-451',
        'hostname': 'cvg-stormsurge-01',
        'user': 'root',
        'type': 'vm',
        'role': 'primary',
        'desc': 'PRIMARY Forge — Ollama host, Docker platform, cvg-platform stack',
        'has_docker': True,
        'has_ollama': True,
        'has_proxmox': False,
    },
    '10.10.10.204': {
        'name': 'vm-454',
        'hostname': 'vm-454',
        'user': 'root',
        'type': 'vm',
        'role': 'compute',
        'desc': 'Secondary compute VM',
        'has_docker': True,
        'has_ollama': False,
        'has_proxmox': False,
    },
    '10.10.10.205': {
        'name': 'vm-455',
        'hostname': 'vm-455',
        'user': 'root',
        'type': 'vm',
        'role': 'compute',
        'desc': 'Secondary compute VM',
        'has_docker': True,
        'has_ollama': False,
        'has_proxmox': False,
    },
    '10.10.10.56': {
        'name': 'QUEEN-11',
        'hostname': 'queen-11',
        'user': 'root',
        'type': 'queen',
        'role': 'hypervisor',
        'desc': 'QUEEN-11 Proxmox hypervisor — forges VMs and CTs (Dell R820)',
        'has_docker': False,
        'has_ollama': False,
        'has_proxmox': True,
    },
    '10.10.10.57': {
        'name': 'QUEEN-21',
        'hostname': 'queen-21',
        'user': 'root',
        'type': 'queen',
        'role': 'compute',
        'desc': 'QUEEN-21 Terra',
        'has_docker': True,
        'has_ollama': False,
        'has_proxmox': False,
    },
    '10.10.10.104': {
        'name': 'CT-104',
        'hostname': 'ct-104',
        'user': 'root',
        'type': 'ct',
        'role': 'container',
        'desc': 'LXC container CT-104',
        'has_docker': True,
        'has_ollama': False,
        'has_proxmox': False,
    },
    '10.10.10.220': {
        'name': 'Audit-VM',
        'hostname': 'audit-vm',
        'user': 'root',
        'type': 'vm',
        'role': 'security',
        'desc': 'Audit VM — Wazuh/Trivy/security forge',
        'has_docker': True,
        'has_ollama': False,
        'has_proxmox': False,
    },
}

SSH_OPTS = [
    '-o', 'StrictHostKeyChecking=no',
    '-o', 'ConnectTimeout=8',
    '-o', 'BatchMode=yes',
    '-o', 'LogLevel=ERROR',
]
SSH_TIMEOUT = int(os.getenv('CVG_FORGE_SSH_TIMEOUT', '20'))


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ── SSH execution helpers ─────────────────────────────────────────────────────

def _ssh_run(ip: str, user: str, command: str,
             timeout: int = SSH_TIMEOUT) -> Tuple[int, str, str]:
    """
    Run a command on a remote forge node via SSH.
    Returns (returncode, stdout, stderr).
    """
    try:
        result = subprocess.run(
            ['ssh', *SSH_OPTS, f'{user}@{ip}', command],
            capture_output=True, text=True, timeout=timeout,
            encoding='utf-8', errors='replace',
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return -1, '', f'SSH timeout after {timeout}s'
    except Exception as exc:
        return -2, '', str(exc)


async def _ssh_run_async(ip: str, user: str, command: str,
                          timeout: int = SSH_TIMEOUT) -> Tuple[int, str, str]:
    """Async version using asyncio subprocess."""
    try:
        proc = await asyncio.create_subprocess_exec(
            'ssh', *SSH_OPTS, f'{user}@{ip}', command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (proc.returncode or 0,
                stdout.decode('utf-8', errors='replace').strip(),
                stderr.decode('utf-8', errors='replace').strip())
    except asyncio.TimeoutError:
        return -1, '', f'SSH timeout after {timeout}s'
    except Exception as exc:
        return -2, '', str(exc)


# ── Forge status collection ───────────────────────────────────────────────────

STATUS_SCRIPT = r"""
python3 - << 'PYEOF'
import subprocess, json, os, platform, time

result = {
    'hostname':   platform.node(),
    'uptime':     '',
    'load':       '',
    'memory':     {},
    'disk':       [],
    'docker':     [],
    'ollama':     {},
    'proxmox':    {},
    'processes':  [],
    'ports':      [],
    'timestamp':  time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
}

def run(cmd, timeout=8):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except: return ''

# Uptime and load
result['uptime'] = run('uptime -p')
result['load']   = run('cat /proc/loadavg')

# Memory
mem_raw = run('free -m')
for line in mem_raw.split('\n'):
    if line.startswith('Mem:'):
        parts = line.split()
        result['memory'] = {
            'total_mb': int(parts[1]) if len(parts) > 1 else 0,
            'used_mb':  int(parts[2]) if len(parts) > 2 else 0,
            'free_mb':  int(parts[3]) if len(parts) > 3 else 0,
        }

# Disk
df_raw = run('df -h --output=source,size,used,avail,pcent,target 2>/dev/null | grep -v tmpfs | grep -v devtmpfs')
for line in df_raw.split('\n')[1:]:
    parts = line.split()
    if len(parts) >= 6:
        result['disk'].append({'dev': parts[0], 'size': parts[1], 'used': parts[2],
                               'avail': parts[3], 'pct': parts[4], 'mount': parts[5]})

# Docker containers
docker_raw = run('docker ps -a --format "{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}" 2>/dev/null')
for line in docker_raw.split('\n'):
    if '|' in line:
        p = line.split('|', 3)
        result['docker'].append({'name': p[0], 'image': p[1],
                                  'status': p[2], 'ports': p[3] if len(p) > 3 else ''})

# Docker stats (one-shot resource usage)
stats_raw = run('docker stats --no-stream --format "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}" 2>/dev/null')
stats_map = {}
for line in stats_raw.split('\n'):
    if '|' in line:
        p = line.split('|', 2)
        stats_map[p[0]] = {'cpu': p[1], 'mem': p[2] if len(p) > 2 else ''}
for c in result['docker']:
    s = stats_map.get(c['name'], {})
    c['cpu'] = s.get('cpu', '')
    c['mem'] = s.get('mem', '')

# Ollama models and running
ollama_list = run('ollama list 2>/dev/null')
ollama_ps   = run('ollama ps 2>/dev/null')
if ollama_list:
    result['ollama']['models'] = ollama_list
if ollama_ps:
    result['ollama']['running'] = ollama_ps

# Proxmox VMs/CTs
pve_vms = run('qm list 2>/dev/null')
pve_cts = run('pct list 2>/dev/null')
if pve_vms:
    result['proxmox']['vms'] = pve_vms
if pve_cts:
    result['proxmox']['cts'] = pve_cts

# AI-related processes
ps_raw = run('ps aux --sort=-%cpu 2>/dev/null | head -20')
ai_kw = ('ollama', 'python', 'node', 'uvicorn', 'fastapi', 'llama', 'stable', 'whisper', 'aider')
for line in ps_raw.split('\n')[1:]:
    if any(k in line.lower() for k in ai_kw) and 'ps aux' not in line:
        result['processes'].append(line[:200])

# Listening ports
ports_raw = run('ss -tlnp 2>/dev/null | grep LISTEN')
for line in ports_raw.split('\n'):
    if 'LISTEN' in line:
        result['ports'].append(line[:150])

print(json.dumps(result))
PYEOF
"""


class ForgeNode:
    """Represents a single forge node with cached status and control operations."""

    def __init__(self, ip: str, config: dict):
        self.ip       = ip
        self.config   = config
        self.name     = config['name']
        self.user     = config['user']
        self.role     = config['role']
        self.desc     = config['desc']
        self._cache:  Optional[dict] = None
        self._fetched_at: float = 0.0
        self._cache_ttl = int(os.getenv('CVG_FORGE_CACHE_TTL', '60'))  # seconds

    def is_cache_fresh(self) -> bool:
        return (time.monotonic() - self._fetched_at) < self._cache_ttl

    async def get_status(self, force: bool = False) -> dict:
        """Get comprehensive forge node status (cached TTL=60s)."""
        if not force and self._cache and self.is_cache_fresh():
            return self._cache

        rc, out, err = await _ssh_run_async(self.ip, self.user, STATUS_SCRIPT.strip())

        if rc != 0 or not out:
            status = {
                'ip': self.ip, 'name': self.name, 'role': self.role,
                'reachable': False, 'error': err or f'SSH exit {rc}',
                'timestamp': _utcnow(),
            }
        else:
            try:
                data = json.loads(out)
                status = {
                    'ip':       self.ip,
                    'name':     self.name,
                    'role':     self.role,
                    'desc':     self.desc,
                    'reachable': True,
                    **data,
                }
            except json.JSONDecodeError:
                status = {
                    'ip': self.ip, 'name': self.name, 'role': self.role,
                    'reachable': False, 'error': 'JSON parse failed',
                    'raw': out[:500], 'timestamp': _utcnow(),
                }

        self._cache = status
        self._fetched_at = time.monotonic()
        return status

    def format_summary(self, status: Optional[dict] = None) -> str:
        """Return a human-readable single-line summary of this forge node."""
        s = status or self._cache
        if not s:
            return f'{self.name} ({self.ip}): [not polled]'
        if not s.get('reachable'):
            return f'{self.name} ({self.ip}): UNREACHABLE — {s.get("error", "")}'

        load    = s.get('load', '').split()[:3]
        load_s  = '/'.join(load) if load else '?'
        mem     = s.get('memory', {})
        mem_pct = (mem.get('used_mb', 0) / max(mem.get('total_mb', 1), 1) * 100)
        containers = s.get('docker', [])
        running = sum(1 for c in containers if 'Up' in c.get('status', ''))
        total   = len(containers)
        ollama  = s.get('ollama', {})
        models  = len(ollama.get('models', '').splitlines()) if ollama.get('models') else 0

        parts = [
            f'{self.name} ({self.ip}): UP',
            f'load={load_s}',
            f'mem={mem_pct:.0f}%',
            f'containers={running}/{total}',
        ]
        if models:
            parts.append(f'ollama_models={models}')
        if ollama.get('running'):
            running_models = len([l for l in ollama['running'].splitlines() if l.strip()])
            parts.append(f'ollama_running={running_models}')
        return '  '.join(parts)

    # ── Control operations ────────────────────────────────────────────────────

    async def exec_command(self, command: str,
                           timeout: int = SSH_TIMEOUT) -> Dict[str, Any]:
        """Execute an arbitrary command on this forge node."""
        logger.info('[forge] EXEC on %s: %s', self.name, command[:100])
        rc, out, err = await _ssh_run_async(self.ip, self.user, command, timeout=timeout)
        return {
            'node': self.name, 'ip': self.ip, 'command': command,
            'returncode': rc, 'stdout': out[:3000], 'stderr': err[:500],
            'success': rc == 0, 'timestamp': _utcnow(),
        }

    async def docker_action(self, action: str,
                             container: str) -> Dict[str, Any]:
        """
        Control a Docker container. action: start|stop|restart|logs|inspect|pull
        container: container name or image:tag for pull
        """
        safe_container = re.sub(r'[^a-zA-Z0-9_.\-:/]', '', container)
        if action == 'logs':
            cmd = f'docker logs --tail 100 {safe_container} 2>&1'
        elif action == 'inspect':
            cmd = f'docker inspect {safe_container} 2>&1 | head -100'
        elif action == 'pull':
            cmd = f'docker pull {safe_container} 2>&1'
        elif action in ('start', 'stop', 'restart'):
            cmd = f'docker {action} {safe_container} 2>&1'
        elif action == 'ps':
            cmd = 'docker ps -a --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}" 2>&1'
        elif action == 'stats':
            cmd = f'docker stats --no-stream {safe_container} 2>&1'
        else:
            return {'error': f'Unknown action: {action}', 'node': self.name}

        result = await self.exec_command(cmd, timeout=60 if action == 'pull' else SSH_TIMEOUT)
        result['action'] = action
        result['container'] = container
        # Invalidate cache after mutations
        if action in ('start', 'stop', 'restart', 'pull'):
            self._fetched_at = 0
        return result

    async def ollama_action(self, action: str,
                             model: str = '') -> Dict[str, Any]:
        """
        Control Ollama on this forge. action: list|ps|pull|run|stop|rm
        """
        safe_model = re.sub(r'[^a-zA-Z0-9_.\-:/]', '', model)
        if action == 'list':
            cmd = 'ollama list 2>&1'
        elif action == 'ps':
            cmd = 'ollama ps 2>&1'
        elif action == 'pull':
            cmd = f'ollama pull {safe_model} 2>&1'
        elif action == 'run':
            cmd = f'ollama run {safe_model} --keepalive 0 2>&1 </dev/null'
        elif action == 'rm':
            cmd = f'ollama rm {safe_model} 2>&1'
        elif action == 'show':
            cmd = f'ollama show {safe_model} 2>&1'
        else:
            return {'error': f'Unknown ollama action: {action}', 'node': self.name}

        return await self.exec_command(cmd, timeout=120 if action in ('pull', 'run') else 15)

    async def systemctl_action(self, action: str,
                                 service: str) -> Dict[str, Any]:
        """Control a systemd service. action: start|stop|restart|status|enable|disable"""
        safe_svc = re.sub(r'[^a-zA-Z0-9_.\-]', '', service)
        if action not in ('start', 'stop', 'restart', 'status', 'enable', 'disable', 'reload'):
            return {'error': f'Unknown systemctl action: {action}'}
        cmd = f'systemctl {action} {safe_svc} 2>&1'
        return await self.exec_command(cmd)

    async def docker_compose_action(self, action: str,
                                     compose_dir: str = '/opt/cvg') -> Dict[str, Any]:
        """Run docker-compose commands in the specified directory."""
        safe_dir = re.sub(r'[^a-zA-Z0-9_./\-]', '', compose_dir)
        if action not in ('up', 'down', 'restart', 'pull', 'ps', 'logs', 'status'):
            return {'error': f'Unknown compose action: {action}'}
        if action == 'status':
            cmd = f'cd {safe_dir} && docker compose ps 2>&1'
        elif action == 'logs':
            cmd = f'cd {safe_dir} && docker compose logs --tail 50 2>&1'
        else:
            flags = '-d' if action == 'up' else ''
            cmd = f'cd {safe_dir} && docker compose {action} {flags} 2>&1'
        return await self.exec_command(cmd, timeout=120 if action in ('up', 'pull') else 30)


# ── Forge Manager ─────────────────────────────────────────────────────────────

class ForgeManager:
    """
    Central management point for all CVG Forge/Queen nodes.

    Provides:
      - Parallel status collection from all forges
      - Cached summaries for context injection into LLM prompts
      - Control operations (docker, ollama, systemctl, exec)
      - Natural language command parsing and dispatch
      - Rich forge status block for Neuron's REASON step
    """

    def __init__(self, nodes: Optional[Dict[str, Dict]] = None):
        self.nodes: Dict[str, ForgeNode] = {
            ip: ForgeNode(ip, cfg)
            for ip, cfg in (nodes or FORGE_NODES).items()
        }
        self._last_full_scan: Optional[float] = None
        self._last_status: Dict[str, dict] = {}

    async def get_all_status(self, force: bool = False,
                              max_workers: int = 6) -> Dict[str, dict]:
        """
        Collect status from all forge nodes in parallel.
        Returns {ip: status_dict} map.
        """
        semaphore = asyncio.Semaphore(max_workers)

        async def _get_one(forge: ForgeNode) -> Tuple[str, dict]:
            async with semaphore:
                status = await forge.get_status(force=force)
                return forge.ip, status

        tasks = [_get_one(forge) for forge in self.nodes.values()]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        self._last_status = dict(results)
        self._last_full_scan = time.monotonic()
        return self._last_status

    async def get_node_status(self, ip: str,
                               force: bool = False) -> Optional[dict]:
        """Get status for a specific forge node."""
        forge = self.nodes.get(ip)
        if not forge:
            # Try by name
            for f in self.nodes.values():
                if f.name.lower() == ip.lower():
                    forge = f
                    break
        if not forge:
            return None
        return await forge.get_status(force=force)

    def format_forge_summary(self, status_map: Optional[Dict[str, dict]] = None) -> str:
        """
        Build a concise text summary of all forge nodes for LLM context injection.
        """
        status = status_map or self._last_status
        if not status:
            return 'Forge status: not yet collected (run forge scan first)'

        lines = [f'[CVG Forge Status — {_utcnow()}]']
        for ip, s in status.items():
            forge = self.nodes.get(ip)
            if forge:
                lines.append(forge.format_summary(s))

        # Count totals
        reachable = sum(1 for s in status.values() if s.get('reachable'))
        total_containers = sum(
            len(s.get('docker', [])) for s in status.values() if s.get('reachable')
        )
        running_containers = sum(
            sum(1 for c in s.get('docker', []) if 'Up' in c.get('status', ''))
            for s in status.values() if s.get('reachable')
        )
        lines.append(
            f'\nForge summary: {reachable}/{len(status)} reachable | '
            f'{running_containers}/{total_containers} containers running'
        )
        return '\n'.join(lines)

    async def forge_context_for_llm(self) -> str:
        """
        Return a forge context block suitable for injection into LLM prompts.
        Uses cached status if fresh, else collects new.
        """
        needs_refresh = (
            not self._last_status
            or self._last_full_scan is None
            or (time.monotonic() - self._last_full_scan) > 120  # 2 min cache
        )
        if needs_refresh:
            await self.get_all_status()
        return self.format_forge_summary()

    # ── Command dispatch ──────────────────────────────────────────────────────

    async def dispatch_command(self, command: str,
                                target: Optional[str] = None) -> Dict[str, Any]:
        """
        Parse and dispatch a forge command string.
        Returns result dict with output.

        Supported commands:
          forge status [node]           — show status
          forge containers [node]       — list containers
          forge docker <action> <name> [node]  — docker control
          forge ollama <action> [model] [node] — ollama control
          forge exec <command> [node]   — run command on node
          forge compose <action> [dir] [node] — docker-compose
          forge service <action> <svc> [node] — systemctl
          forge logs <container> [node]  — container logs
          forge restart <container> [node] — restart container
          forge pull <image> [node]      — pull Docker image
        """
        cmd_lower = command.lower().strip()
        parts = command.strip().split(None, 4)

        # Resolve target node
        forge = self._resolve_target(target)

        # --- STATUS ---
        if re.search(r'\b(status|health|up|running|overview|summary)\b', cmd_lower):
            if forge:
                status = await forge.get_status(force=True)
                return {
                    'type': 'forge_status',
                    'node': forge.name,
                    'status': status,
                    'summary': forge.format_summary(status),
                }
            else:
                all_status = await self.get_all_status(force=True)
                return {
                    'type': 'forge_all_status',
                    'summary': self.format_forge_summary(all_status),
                    'nodes': {ip: s.get('reachable', False) for ip, s in all_status.items()},
                }

        # --- CONTAINERS ---
        if re.search(r'\b(container|docker|ps)\b', cmd_lower):
            action_match = re.search(
                r'\b(restart|stop|start|logs|inspect|pull|stats)\b', cmd_lower
            )
            if action_match:
                action = action_match.group(1)
                # Extract container name (word after action)
                remaining = cmd_lower[action_match.end():].strip().split()
                container = remaining[0] if remaining else ''
                target_forge = forge or self._primary_forge()
                if target_forge and container:
                    return await target_forge.docker_action(action, container)
            # Just list containers
            target_forge = forge or self._primary_forge()
            if target_forge:
                return await target_forge.docker_action('ps', '')

        # --- OLLAMA ---
        if re.search(r'\bollama\b', cmd_lower):
            action_match = re.search(r'\b(list|ps|pull|rm|show|run)\b', cmd_lower)
            action = action_match.group(1) if action_match else 'list'
            # Extract model name
            model = ''
            if action_match:
                remaining = cmd_lower[action_match.end():].strip().split()
                model = remaining[0] if remaining else ''
            target_forge = forge or self._ollama_forge()
            if target_forge:
                return await target_forge.ollama_action(action, model)

        # --- EXEC ---
        exec_match = re.search(r'\bexec(?:ute)?\s+(.+)', command, re.IGNORECASE)
        if exec_match:
            exec_cmd = exec_match.group(1).strip()
            target_forge = forge or self._primary_forge()
            if target_forge:
                return await target_forge.exec_command(exec_cmd)

        # --- SERVICE ---
        svc_match = re.search(
            r'\b(restart|start|stop|status|reload)\s+service\s+(\S+)',
            command, re.IGNORECASE
        )
        if svc_match:
            action, service = svc_match.group(1), svc_match.group(2)
            target_forge = forge or self._primary_forge()
            if target_forge:
                return await target_forge.systemctl_action(action, service)

        # --- LOGS ---
        logs_match = re.search(r'\blogs?\s+(\S+)', command, re.IGNORECASE)
        if logs_match:
            container = logs_match.group(1)
            target_forge = forge or self._primary_forge()
            if target_forge:
                return await target_forge.docker_action('logs', container)

        # --- COMPOSE ---
        compose_match = re.search(
            r'\bcompose\s+(up|down|restart|pull|ps|logs|status)',
            command, re.IGNORECASE
        )
        if compose_match:
            action = compose_match.group(1).lower()
            target_forge = forge or self._primary_forge()
            if target_forge:
                # Default compose dir from CVG env
                compose_dir = os.getenv('CVG_COMPOSE_DIR', '/opt/cvg')
                return await target_forge.docker_compose_action(action, compose_dir)

        return {
            'error': f'Unrecognized forge command: {command}',
            'hint': 'Try: forge status | forge containers | forge restart <name> | '
                    'forge ollama list | forge exec <cmd> [on <node>]',
        }

    def _resolve_target(self, target: Optional[str]) -> Optional[ForgeNode]:
        """Resolve a target string (IP, name, or role) to a ForgeNode."""
        if not target:
            return None
        target_lower = target.lower().strip()
        # Direct IP match
        if target in self.nodes:
            return self.nodes[target]
        # Name or hostname match
        for f in self.nodes.values():
            if (f.name.lower() == target_lower or
                    f.config.get('hostname', '').lower() == target_lower or
                    target_lower in f.name.lower()):
                return f
        # Role match
        for f in self.nodes.values():
            if f.role == target_lower:
                return f
        return None

    def _primary_forge(self) -> Optional[ForgeNode]:
        """Return the primary forge node."""
        return self.nodes.get('10.10.10.200')

    def _ollama_forge(self) -> Optional[ForgeNode]:
        """Return the node running Ollama."""
        for f in self.nodes.values():
            if f.config.get('has_ollama'):
                return f
        return self._primary_forge()


# ── Natural language forge command extraction ─────────────────────────────────

FORGE_TRIGGERS = [
    # Visibility
    r'\bforge\b', r'\bcontainer\b', r'\bdocker\b', r'\bollama\b',
    r'\bcompose\b', r'\bvm.?45[0-9]\b', r'\bqueen.?1[0-9]\b',
    r'\bvm-45[0-9]\b', r'\bct-?104\b', r'\bwhat.{0,20}running\b',
    r'\bwhat.{0,20}deployed\b', r'\bshow.{0,20}forge\b',
    r'\bstatus.{0,20}node\b', r'\bnode.{0,20}status\b',
    r'\bresource.{0,20}usage\b', r'\bmemory.{0,20}(on|vm|forge)\b',
    r'\bcpu.{0,20}(on|vm|forge)\b', r'\bdisk.{0,20}(on|vm|forge)\b',
    # Control
    r'\brestart\s+(the\s+)?\w+\s*container\b',
    r'\bstop\s+(the\s+)?\w+\s*container\b',
    r'\bstart\s+(the\s+)?\w+\s*container\b',
    r'\bpull\s+.{0,30}image\b',
    r'\bellama.{0,30}model\b',
    r'\bload.{0,30}model\b',
    r'\brun.{0,30}on.{0,20}forge\b',
    r'\bexec.{0,30}on.{0,20}(node|vm|forge)\b',
]

_TRIGGER_RE = re.compile('|'.join(FORGE_TRIGGERS), re.IGNORECASE)


def is_forge_query(message: str) -> bool:
    """Returns True if the message contains forge-related intent."""
    return bool(_TRIGGER_RE.search(message))


def extract_forge_command(message: str) -> Optional[str]:
    """
    Extract a structured forge command from a natural language message.
    Returns a command string or None if unclear.
    """
    msg = message.lower().strip()

    # Status queries
    if re.search(r'(show|what|get|check|give).{0,20}(forge|container|docker|vm).{0,20}status', msg):
        return 'forge status'
    if re.search(r'(what|which).{0,20}(running|deployed|active)', msg):
        return 'forge status'
    if re.search(r'(show|list|what).{0,20}container', msg):
        return 'forge containers'

    # Ollama
    if re.search(r'(what|list|show).{0,20}ollama.{0,20}model', msg):
        return 'forge ollama list'
    if re.search(r'(what|which).{0,20}model.{0,20}(loaded|running|active)', msg):
        return 'forge ollama ps'

    # Specific node status
    node_match = re.search(
        r'(status|health|info|check).{0,20}(vm-?45[0-9]|queen-?[0-9]+|ct-?104|audit|primary|forge)',
        msg
    )
    if node_match:
        node = node_match.group(2)
        return f'forge status {node}'

    # Restart/stop/start container
    action_container = re.search(
        r'\b(restart|stop|start)\s+(?:the\s+)?([a-zA-Z0-9_\-]+(?:-v[0-9]+)?)\s*(?:container)?\b',
        msg
    )
    if action_container:
        action, name = action_container.group(1), action_container.group(2)
        return f'forge docker {action} {name}'

    # Logs
    logs_match = re.search(r'\blogs?\s+(?:for\s+|of\s+)?([a-zA-Z0-9_\-]+)', msg)
    if logs_match:
        return f'forge logs {logs_match.group(1)}'

    return None


# ── Module singleton ──────────────────────────────────────────────────────────

_forge_manager: Optional[ForgeManager] = None


def get_forge_manager() -> ForgeManager:
    global _forge_manager
    if _forge_manager is None:
        _forge_manager = ForgeManager()
    return _forge_manager


async def get_forge_context() -> str:
    """Get forge context for LLM injection (cached, non-blocking)."""
    return await get_forge_manager().forge_context_for_llm()
