# CVG Neuron -- Hive Memory Preloader v1
# (c) Clearview Geographic, LLC -- Proprietary and PRIVATE
#
# Connects to EVERY Queen, VM, CT, and Forge node in Hive-0 via SSH and
# performs a deep, comprehensive one-time memory preload.
#
# Unlike the 5-min incremental harvest, the preloader:
#   - Harvests FULL history (not just recent — goes back as far as possible)
#   - Collects installed AI packages and tools per node
#   - Captures all Docker AI container history and configs
#   - Reads all AI-related config files (~/.config, /etc, /opt)
#   - Captures full shell history (AI commands from all time)
#   - Reads any Ollama model lists, API logs, LLM configs
#   - Records node identity + hardware as semantic facts
#   - Submits directly to Neuron memory (semantic + episodic + capture tiers)
#
# Called automatically at Neuron startup (async, non-blocking).
# Can also run standalone: python -m neuron.hive_preloader
#
# Idempotent: content-hash deduplication prevents re-loading the same data.

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger('cvg.neuron.hive_preloader')

# ── Configuration ─────────────────────────────────────────────────────────────

NEURON_HOST      = os.getenv('CVG_NEURON_HOST',   '127.0.0.1')  # localhost when embedded
NEURON_PORT      = int(os.getenv('CVG_NEURON_PORT', '8095'))
NEURON_KEY       = os.getenv('CVG_INTERNAL_KEY',  'cvg-internal-2026')
CAPTURE_PORT     = int(os.getenv('CVG_CAPTURE_PORT', '8098'))
MAX_WORKERS      = int(os.getenv('CVG_PRELOAD_WORKERS', '6'))   # parallel SSH connections
SSH_TIMEOUT      = int(os.getenv('CVG_PRELOAD_SSH_TIMEOUT', '30'))
MAX_BYTES        = int(os.getenv('CVG_PRELOAD_MAX_BYTES', '6000'))

_LEARN_URL  = f'http://{NEURON_HOST}:{NEURON_PORT}/api/memory/learn'
_CAP_URL    = f'http://127.0.0.1:{CAPTURE_PORT}/capture'
_CAP_URL2   = f'http://{NEURON_HOST}:{NEURON_PORT}/api/memory/capture'

# State: track which nodes have been fully preloaded
_STATE_DIR  = Path(os.getenv('NEURON_DATA_DIR',
    '/app/data' if Path('/app/data').exists()
    else str(Path.home() / 'cvg_neuron_data')
)) / 'memory'
_PRELOAD_STATE_FILE = _STATE_DIR / 'preload_state.json'

# All Hive-0 nodes — same list as history_harvester
_ALL_HIVE_NODES: Dict[str, Dict[str, str]] = {
    '10.10.10.200': {'user': 'root', 'name': 'vm-451/cvg-stormsurge-01', 'type': 'vm',    'role': 'primary_ollama'},
    '10.10.10.204': {'user': 'root', 'name': 'vm-454',                   'type': 'vm',    'role': 'compute'},
    '10.10.10.205': {'user': 'root', 'name': 'vm-455',                   'type': 'vm',    'role': 'compute'},
    '10.10.10.56':  {'user': 'root', 'name': 'QUEEN-11-Proxmox',         'type': 'queen', 'role': 'hypervisor'},
    '10.10.10.57':  {'user': 'root', 'name': 'QUEEN-21-Terra',           'type': 'queen', 'role': 'compute'},
    '10.10.10.100': {'user': 'root', 'name': 'QUEEN-10-TrueNAS',         'type': 'queen', 'role': 'storage'},
    '10.10.10.104': {'user': 'root', 'name': 'CT-104',                   'type': 'ct',    'role': 'container'},
    '10.10.10.220': {'user': 'root', 'name': 'Audit-VM',                 'type': 'vm',    'role': 'security'},
    '10.10.10.53':  {'user': 'admin','name': 'QUEEN-12-Synology-DS1823', 'type': 'nas',   'role': 'storage'},
    '10.10.10.67':  {'user': 'admin','name': 'QUEEN-20-Synology-DS3622', 'type': 'nas',   'role': 'storage'},
    '10.10.10.71':  {'user': 'admin','name': 'QUEEN-30-Synology-DS418',  'type': 'nas',   'role': 'storage'},
}

# Deep harvest script — runs on the remote node, returns comprehensive JSON
_DEEP_HARVEST_SCRIPT = r"""
python3 -c "
import os, json, pathlib, subprocess, platform, sys
home = pathlib.Path.home()
result = {
    'hostname': platform.node(),
    'uname': ' '.join(list(platform.uname())[:3]),
    'python': sys.version.split()[0],
    'history': [],
    'ai_packages': [],
    'docker': [],
    'ollama': {},
    'cline': [],
    'aider': '',
    'llm_cli': [],
    'configs': [],
}

# Hostname/OS identity
try:
    r = subprocess.run(['uname', '-a'], capture_output=True, text=True, timeout=5)
    result['uname'] = r.stdout.strip()
except: pass

# Installed AI tools
for pkg in ['ollama', 'aider', 'llm', 'sgpt', 'python3', 'docker', 'kubectl', 'helm']:
    try:
        r = subprocess.run(['which', pkg], capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            result['ai_packages'].append(pkg)
    except: pass

# pip packages (AI-related)
try:
    r = subprocess.run(['pip3', 'list', '--format=json'], capture_output=True, text=True, timeout=10)
    pkgs = json.loads(r.stdout)
    ai_pkgs = [p['name'] for p in pkgs if any(k in p['name'].lower()
               for k in ('ollama','langchain','openai','anthropic','transformers',
                         'torch','tensorflow','llm','gpt','claude','ai','ml','hugging'))]
    result['ai_packages'].extend(ai_pkgs[:20])
except: pass

# Ollama models and status
try:
    r = subprocess.run(['ollama', 'list'], capture_output=True, text=True, timeout=8)
    if r.returncode == 0:
        result['ollama']['models'] = r.stdout.strip()
    r2 = subprocess.run(['ollama', 'ps'], capture_output=True, text=True, timeout=5)
    if r2.returncode == 0:
        result['ollama']['running'] = r2.stdout.strip()
except: pass

# Docker AI containers (all — including stopped)
try:
    r = subprocess.run(['docker', 'ps', '-a', '--format',
                        '{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'],
                       capture_output=True, text=True, timeout=10)
    if r.returncode == 0:
        ai_kw = ('ollama','neuron','llm','gpt','claude','ai','inference','model','ml')
        for line in r.stdout.strip().split('\n'):
            if any(k in line.lower() for k in ai_kw):
                result['docker'].append(line[:200])
except: pass

# Cline history (all tasks)
for base in [home/'.config/Code/User/globalStorage/saoudrizwan.claude-dev/tasks',
             home/'.local/share/Code/User/globalStorage/saoudrizwan.claude-dev/tasks']:
    if base.exists():
        for td in sorted(base.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True)[:30]:
            cf = td / 'api_conversation_history.json'
            if not cf.exists(): cf = td / 'ui_messages.json'
            if cf.exists() and cf.stat().st_size > 100:
                try:
                    raw = cf.read_text(errors='replace')
                    data = json.loads(raw)
                    msgs = data if isinstance(data, list) else data.get('messages', [])
                    lines = ['[cline task %s]' % td.name[:12]]
                    for m in msgs[-10:]:
                        if isinstance(m, dict):
                            r2 = m.get('role', '?')
                            c2 = m.get('content', '')
                            if isinstance(c2, list): c2 = ' '.join(x.get('text','') if isinstance(x,dict) else str(x) for x in c2)
                            c2 = str(c2).strip()[:300]
                            if c2: lines.append('[%s] %s' % (r2, c2))
                    if len(lines) > 1: result['cline'].append('\n'.join(lines))
                except: pass

# Full aider history
for af in [home/'.aider.chat.history.md', pathlib.Path('/root/.aider.chat.history.md')]:
    if af.exists():
        try:
            result['aider'] = af.read_text(errors='replace')[-5000:]
        except: pass

# LLM CLI all logs
for ld in [home/'.config/io.datasette.llm', pathlib.Path('/root/.config/io.datasette.llm')]:
    if ld.exists():
        try:
            for p in sorted(ld.rglob('*.json'), key=lambda f: f.stat().st_mtime, reverse=True)[:10]:
                result['llm_cli'].append({'file': p.name, 'content': p.read_text(errors='replace')[-2000:]})
        except: pass

# Full bash/zsh history (AI commands)
ai_cmds = ('claude','aider','llm','sgpt','ollama','gpt','copilot','neuron')
for hf in [home/'.bash_history', home/'.zsh_history',
           pathlib.Path('/root/.bash_history'), pathlib.Path('/root/.zsh_history')]:
    if hf.exists():
        try:
            lines2 = hf.read_text(errors='replace').split('\n')
            ai_lines = [l for l in lines2 if any(k in l.lower() for k in ai_cmds)]
            if ai_lines:
                result['history'].extend(ai_lines[-200:])
        except: pass

# AI config files
for cfg in [home/'.ollama', home/'.config/aider', home/'.config/oaidio',
            pathlib.Path('/etc/ollama'), pathlib.Path('/opt/cvg')]:
    if cfg.exists():
        try:
            for cfp in list(cfg.rglob('*'))[:5]:
                if cfp.is_file() and cfp.stat().st_size < 5000:
                    result['configs'].append({'path': str(cfp), 'content': cfp.read_text(errors='replace')[:500]})
        except: pass

print(json.dumps(result))
" 2>/dev/null
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _content_hash(text: str) -> str:
    return hashlib.md5(text.encode('utf-8', errors='replace'),
                       usedforsecurity=False).hexdigest()[:16]


# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _http_post(url: str, payload: dict, headers: Optional[dict] = None) -> bool:
    import urllib.request
    try:
        data = json.dumps(payload).encode('utf-8')
        hdrs = {'Content-Type': 'application/json', **(headers or {})}
        req = urllib.request.Request(url, data=data, headers=hdrs, method='POST')
        with urllib.request.urlopen(req, timeout=8) as resp:
            resp.read()
        return True
    except Exception:
        return False


def _submit_capture(source: str, content: str, role: str = 'system',
                    metadata: Optional[dict] = None) -> bool:
    if not content or len(content.strip()) < 10:
        return False
    payload = {
        'source':      source,
        'content':     content[:MAX_BYTES],
        'role':        role,
        'terminal_id': f'preloader_{source}',
        'metadata':    metadata or {},
    }
    # Try local daemon first, then Neuron API
    if _http_post(_CAP_URL, payload):
        return True
    return _http_post(_CAP_URL2, payload,
                      headers={'X-CVG-Key': NEURON_KEY})


def _learn_fact(key: str, value: Any, source: str = 'hive_preload',
                confidence: float = 0.9) -> bool:
    """Submit a semantic fact directly to Neuron /api/memory/learn."""
    payload = {'key': key, 'value': value, 'source': source, 'confidence': confidence}
    return _http_post(_LEARN_URL, payload,
                      headers={'X-CVG-Key': NEURON_KEY})


# ── Preload state tracker ─────────────────────────────────────────────────────

class PreloadState:
    """Tracks which nodes have been preloaded and seen content hashes."""

    def __init__(self):
        self._data: Dict[str, Any] = {'nodes': {}, 'hashes': []}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            if _PRELOAD_STATE_FILE.exists():
                self._data = json.loads(_PRELOAD_STATE_FILE.read_text())
        except Exception:
            pass

    def save(self) -> None:
        try:
            _STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _PRELOAD_STATE_FILE.with_suffix('.tmp')
            tmp.write_text(json.dumps(self._data))
            tmp.replace(_PRELOAD_STATE_FILE)
        except Exception:
            pass

    def is_preloaded(self, ip: str) -> bool:
        """Has this node been preloaded before?"""
        with self._lock:
            info = self._data.get('nodes', {}).get(ip, {})
            return bool(info.get('preloaded_at'))

    def mark_preloaded(self, ip: str, node_name: str, items: int) -> None:
        with self._lock:
            self._data.setdefault('nodes', {})[ip] = {
                'node_name':    node_name,
                'preloaded_at': _utcnow(),
                'items_loaded': items,
            }
        self.save()

    def is_seen(self, h: str) -> bool:
        with self._lock:
            return h in self._data.get('hashes', [])

    def mark_seen(self, h: str) -> None:
        with self._lock:
            seen = self._data.setdefault('hashes', [])
            seen.append(h)
            if len(seen) > 20_000:
                self._data['hashes'] = seen[-15_000:]

    def get_preloaded_nodes(self) -> dict:
        with self._lock:
            return dict(self._data.get('nodes', {}))


_state = PreloadState()


# ── SSH deep harvest ──────────────────────────────────────────────────────────

def _ssh_run(user: str, ip: str, script: str, timeout: int = SSH_TIMEOUT) -> Optional[str]:
    """Run a script on a remote node via SSH. Returns stdout or None."""
    try:
        result = subprocess.run(
            ['ssh',
             '-o', 'StrictHostKeyChecking=no',
             '-o', f'ConnectTimeout={min(timeout, 10)}',
             '-o', 'BatchMode=yes',
             '-o', 'LogLevel=ERROR',
             f'{user}@{ip}',
             script,
             ],
            capture_output=True, text=True, timeout=timeout,
            encoding='utf-8', errors='replace',
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except subprocess.TimeoutExpired:
        logger.debug('[preloader] %s@%s: SSH timeout', user, ip)
    except Exception as exc:
        logger.debug('[preloader] %s@%s: SSH error: %s', user, ip, exc)
    return None


# ── Per-node preload ──────────────────────────────────────────────────────────

def preload_node(ip: str, info: dict, force: bool = False) -> int:
    """
    Preload memory from a single hive node.
    Returns number of memory items created/submitted.
    """
    user      = info['user']
    node_name = info['name']
    node_type = info['type']
    node_role = info['role']
    source    = f'hive_{ip.replace(".", "_")}'

    if not force and _state.is_preloaded(ip):
        logger.info('[preloader] %s (%s): already preloaded — skipping', node_name, ip)
        return 0

    logger.info('[preloader] Starting preload: %s (%s) type=%s', node_name, ip, node_type)
    t0 = time.monotonic()
    items = 0

    # ── Step 1: Run deep harvest script ──────────────────────────────────────
    raw_output = _ssh_run(user, ip, _DEEP_HARVEST_SCRIPT.strip(), timeout=SSH_TIMEOUT)
    if not raw_output:
        logger.warning('[preloader] %s: SSH unreachable or no output', node_name)
        return 0

    try:
        data = json.loads(raw_output)
    except json.JSONDecodeError:
        logger.warning('[preloader] %s: Could not parse harvest JSON', node_name)
        return 0

    hostname = data.get('hostname', node_name)
    uname    = data.get('uname', '')
    python_v = data.get('python', '')

    # ── Step 2: Store node identity as semantic facts ─────────────────────────
    facts = {
        f'hive.node.{ip}.name':     (node_name, 1.0),
        f'hive.node.{ip}.hostname': (hostname,  1.0),
        f'hive.node.{ip}.type':     (node_type, 1.0),
        f'hive.node.{ip}.role':     (node_role, 1.0),
        f'hive.node.{ip}.os':       (uname,     0.9),
        f'hive.node.{ip}.python':   (python_v,  0.9),
    }

    ai_pkgs = data.get('ai_packages', [])
    if ai_pkgs:
        facts[f'hive.node.{ip}.ai_tools'] = (', '.join(ai_pkgs[:20]), 0.95)

    ollama = data.get('ollama', {})
    if ollama.get('models'):
        facts[f'hive.node.{ip}.ollama_models'] = (ollama['models'][:500], 0.95)
    if ollama.get('running'):
        facts[f'hive.node.{ip}.ollama_running'] = (ollama['running'][:200], 0.9)

    docker_ai = data.get('docker', [])
    if docker_ai:
        facts[f'hive.node.{ip}.ai_containers'] = ('\n'.join(docker_ai[:10]), 0.9)

    for fact_key, (fact_val, confidence) in facts.items():
        if fact_val and str(fact_val).strip():
            if _learn_fact(fact_key, fact_val, source='hive_preload', confidence=confidence):
                items += 1

    # ── Step 3: Submit Cline history ──────────────────────────────────────────
    for conv in data.get('cline', []):
        if conv and len(conv) > 30:
            h = _content_hash(conv)
            if not _state.is_seen(h):
                if _submit_capture(f'cline_{ip}', conv, role='assistant',
                                   metadata={'node': ip, 'hostname': hostname,
                                             'preload': True}):
                    _state.mark_seen(h)
                    items += 1

    # ── Step 4: Submit Aider history ─────────────────────────────────────────
    aider_hist = data.get('aider', '')
    if aider_hist and len(aider_hist) > 50:
        # Split into chunks of MAX_BYTES
        for i in range(0, len(aider_hist), MAX_BYTES):
            chunk = aider_hist[i:i + MAX_BYTES]
            h = _content_hash(chunk)
            if not _state.is_seen(h):
                if _submit_capture(f'aider_{ip}',
                                   f'[aider-preload/{hostname}]\n{chunk}',
                                   role='assistant',
                                   metadata={'node': ip, 'preload': True}):
                    _state.mark_seen(h)
                    items += 1

    # ── Step 5: Submit LLM CLI history ───────────────────────────────────────
    for llm_item in data.get('llm_cli', []):
        content = llm_item.get('content', '')
        fname   = llm_item.get('file', '')
        if content and len(content) > 30:
            h = _content_hash(content)
            if not _state.is_seen(h):
                if _submit_capture(f'llm_cli_{ip}',
                                   f'[llm_cli-preload/{hostname} {fname}]\n{content}',
                                   role='assistant',
                                   metadata={'node': ip, 'file': fname, 'preload': True}):
                    _state.mark_seen(h)
                    items += 1

    # ── Step 6: Submit shell history summary ─────────────────────────────────
    shell_hist = data.get('history', [])
    if shell_hist:
        content = f'[shell-history-preload/{hostname}]\n' + '\n'.join(shell_hist[:100])
        h = _content_hash(content)
        if not _state.is_seen(h):
            if _submit_capture(f'shell_{ip}', content, role='user',
                               metadata={'node': ip, 'preload': True}):
                _state.mark_seen(h)
                items += 1

    # ── Step 7: Submit Docker AI container summary ───────────────────────────
    if docker_ai:
        content = (f'[docker-preload/{hostname}]\n'
                   f'AI containers found:\n' + '\n'.join(docker_ai[:15]))
        h = _content_hash(content)
        if not _state.is_seen(h):
            if _submit_capture(f'docker_{ip}', content, role='system',
                               metadata={'node': ip, 'preload': True}):
                _state.mark_seen(h)
                items += 1

    # ── Step 8: Submit config files summary ──────────────────────────────────
    configs = data.get('configs', [])
    if configs:
        cfg_text = '\n'.join(
            f"=== {c['path']} ===\n{c['content']}"
            for c in configs[:5]
        )
        if cfg_text:
            content = f'[ai-configs-preload/{hostname}]\n{cfg_text}'
            h = _content_hash(content)
            if not _state.is_seen(h):
                if _submit_capture(f'config_{ip}', content, role='system',
                                   metadata={'node': ip, 'preload': True}):
                    _state.mark_seen(h)
                    items += 1

    # ── Save state ────────────────────────────────────────────────────────────
    _state.mark_preloaded(ip, node_name, items)
    _state.save()

    elapsed = time.monotonic() - t0
    logger.info('[preloader] %s (%s): preloaded %d items in %.1fs', node_name, ip, items, elapsed)
    return items


# ── Full hive preload ─────────────────────────────────────────────────────────

def preload_all_nodes(force: bool = False,
                      nodes: Optional[Dict[str, Dict]] = None) -> Dict[str, int]:
    """
    Preload memory from ALL hive nodes in parallel.
    Returns {ip: items_loaded} dict.
    """
    target_nodes = nodes or _ALL_HIVE_NODES
    results: Dict[str, int] = {}
    total = 0

    logger.info('[preloader] Starting full hive preload (%d nodes, %d workers)',
                len(target_nodes), MAX_WORKERS)

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=MAX_WORKERS,
            thread_name_prefix='hive-preload') as exe:
        future_to_ip = {
            exe.submit(preload_node, ip, info, force): ip
            for ip, info in target_nodes.items()
        }
        for future in concurrent.futures.as_completed(future_to_ip, timeout=300):
            ip = future_to_ip[future]
            try:
                n = future.result()
                results[ip] = n
                total += n
            except Exception as exc:
                logger.warning('[preloader] %s: preload error: %s', ip, exc)
                results[ip] = 0

    logger.info('[preloader] Hive preload complete — %d total items from %d nodes',
                total, len(results))
    return results


def get_preload_status() -> dict:
    """Return preload status for all nodes."""
    preloaded = _state.get_preloaded_nodes()
    status = []
    for ip, info in _ALL_HIVE_NODES.items():
        node_state = preloaded.get(ip, {})
        status.append({
            'ip':           ip,
            'name':         info['name'],
            'type':         info['type'],
            'preloaded':    bool(node_state.get('preloaded_at')),
            'preloaded_at': node_state.get('preloaded_at'),
            'items_loaded': node_state.get('items_loaded', 0),
        })
    return {
        'nodes':        status,
        'total_nodes':  len(_ALL_HIVE_NODES),
        'preloaded':    sum(1 for s in status if s['preloaded']),
        'pending':      sum(1 for s in status if not s['preloaded']),
    }


# ── Neuron lifespan integration ───────────────────────────────────────────────

def start_hive_preload_async(force: bool = False) -> None:
    """
    Start hive preload in a background thread — called from Neuron lifespan.
    Runs once on startup. Subsequent startups skip already-preloaded nodes.
    Pass force=True to re-preload all nodes.
    """
    def _run():
        # Brief delay to let Neuron fully boot before preloading
        time.sleep(15)
        try:
            results = preload_all_nodes(force=force)
            total = sum(results.values())
            if total > 0:
                logger.info('[preloader] Startup preload complete: %d items loaded from %d/%d nodes',
                            total, sum(1 for v in results.values() if v > 0), len(results))
        except Exception as exc:
            logger.error('[preloader] Startup preload failed: %s', exc)

    t = threading.Thread(target=_run, name='hive-preload', daemon=True)
    t.start()
    logger.info('[preloader] Hive preload started in background (15s warmup delay)')


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [CVG-PRELOADER] %(levelname)s %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    parser = argparse.ArgumentParser(description='CVG Neuron Hive Memory Preloader')
    parser.add_argument('--all',    action='store_true', help='Preload all nodes')
    parser.add_argument('--node',   metavar='IP',        help='Preload a specific node IP')
    parser.add_argument('--force',  action='store_true', help='Re-preload even if already done')
    parser.add_argument('--status', action='store_true', help='Show preload status for all nodes')
    args = parser.parse_args()

    if args.status:
        status = get_preload_status()
        print(f"Preload status: {status['preloaded']}/{status['total_nodes']} nodes done")
        for node in status['nodes']:
            flag = '[done]' if node['preloaded'] else '[pending]'
            at   = node.get('preloaded_at', '')[:16] if node['preloaded'] else ''
            n    = node.get('items_loaded', 0)
            print(f"  {flag:10s} {node['ip']:15s} {node['name']:30s} {n:3d} items  {at}")
        sys.exit(0)

    elif args.node:
        if args.node not in _ALL_HIVE_NODES:
            print(f'Unknown node IP: {args.node}')
            print(f'Known: {", ".join(_ALL_HIVE_NODES.keys())}')
            sys.exit(1)
        info = _ALL_HIVE_NODES[args.node]
        n = preload_node(args.node, info, force=args.force)
        print(f'Preloaded {n} items from {info["name"]} ({args.node})')

    elif args.all:
        results = preload_all_nodes(force=args.force)
        total = sum(results.values())
        print(f'\nPreload complete: {total} items from {len(results)} nodes')
        for ip, n in sorted(results.items()):
            name = _ALL_HIVE_NODES.get(ip, {}).get('name', ip)
            print(f'  {ip:15s} {name:30s} {n:3d} items')

    else:
        parser.print_help()
        sys.exit(1)
