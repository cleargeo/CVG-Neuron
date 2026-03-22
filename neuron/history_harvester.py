# CVG Neuron -- AI History Harvester v1
# (c) Clearview Geographic, LLC -- Proprietary and PRIVATE
#
# Autonomously harvests conversation history from ALL known AI tools on this machine
# AND from remote machines in the CVG Hive cluster via SSH.
#
# Harvested sources (local):
#   - Cline       : %APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\tasks\
#   - Claude App  : %LOCALAPPDATA%\Claude\  (conversation cache/logs)
#   - Copilot     : %APPDATA%\Code\User\globalStorage\github.copilot-chat\
#   - Aider       : ~/.aider.chat.history.md, ~/.aider/
#   - LLM CLI     : ~/.config/io.datasette.llm/ or %LOCALAPPDATA%\io.datasette.llm\
#   - Cursor IDE  : %APPDATA%\Cursor\User\globalStorage\
#   - Continue    : %APPDATA%\Code\User\globalStorage\continue.continue\
#
# Harvested sources (hive — remote via SSH):
#   - All of the above on each node that has them
#   - Configured via CVG_HIVE_NODES env or defaults from known Hive-0 IPs
#
# All harvested content is submitted to the Neuron capture endpoint with
# source tags like 'cline', 'claude_app', 'copilot', 'aider', etc.
# Only NEW content (not previously seen) is submitted — tracked by content hash.

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger('cvg.neuron.history_harvester')

# ── Configuration ─────────────────────────────────────────────────────────────

HARVEST_INTERVAL   = int(os.getenv('CVG_HARVEST_INTERVAL', '60'))   # seconds between local harvests
HIVE_HARVEST_INTERVAL = int(os.getenv('CVG_HIVE_HARVEST_INTERVAL', '300'))  # 5 min for remote
MAX_CONV_BYTES     = int(os.getenv('CVG_HARVEST_MAX_BYTES', '6000'))
CAPTURE_URL        = os.getenv('CVG_CAPTURE_URL',  'http://127.0.0.1:8098/capture')
NEURON_URL         = os.getenv('CVG_NEURON_URL',   'http://localhost:8095/api/memory/capture')
NEURON_KEY         = os.getenv('CVG_INTERNAL_KEY', 'cvg-internal-2026')

# Remote hive nodes to harvest from via SSH
# ALL known Hive-0 nodes — SSHable nodes only (NAS/iDRAC/iLO use different auth)
# Format: "user@host" — must have key-based SSH auth already configured
# Override via env: CVG_HIVE_NODES=root@host1,root@host2,...
_ALL_HIVE_NODES = [
    # Primary VMs (vm-451, vm-454, vm-455) on cvg-stormsurge-01
    'root@10.10.10.200',   # vm-451 / cvg-stormsurge-01 (PRIMARY Ollama host)
    'root@10.10.10.204',   # vm-454
    'root@10.10.10.205',   # vm-455
    # QUEEN-11 (Dell PowerEdge R820 — Proxmox host)
    'root@10.10.10.56',    # QUEEN-11 Proxmox
    # QUEEN-21 Terra
    'root@10.10.10.57',    # QUEEN-21
    # QUEEN-10 (HP ProLiant ML350 Gen10 — ESXi host)
    # ESXi SSH (if enabled) or TrueNAS
    'root@10.10.10.100',   # QUEEN-10 TrueNAS
    # CT-104 (LXC container)
    'root@10.10.10.104',   # CT-104
    # Audit VM (Ubuntu 22.04 — Wazuh/Trivy)
    'root@10.10.10.220',   # Audit VM
    # NAS devices — ssh admin if enabled (Synology uses 'admin' or 'root')
    'admin@10.10.10.53',   # QUEEN-12 Synology DS1823+
    'admin@10.10.10.67',   # QUEEN-20 Synology DS3622xs+
    'admin@10.10.10.71',   # QUEEN-30 Synology DS418
]

_DEFAULT_HIVE_NODES = [
    n.strip() for n in os.getenv('CVG_HIVE_NODES',
        ','.join(_ALL_HIVE_NODES)
    ).split(',') if n.strip()
]

# State file for tracking what's already been harvested
_STATE_DIR = Path(os.getenv('NEURON_DATA_DIR',
    '/app/data' if Path('/app/data').exists()
    else str(Path.home() / 'cvg_neuron_data')
)) / 'memory'
_SEEN_HASHES_FILE = _STATE_DIR / 'harvester_seen.json'


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def _content_hash(text: str) -> str:
    return hashlib.md5(text.strip().encode('utf-8', errors='replace'),
                       usedforsecurity=False).hexdigest()[:16]


# ── Seen-hash tracker (deduplication) ────────────────────────────────────────

class SeenHashes:
    """Persists a set of content hashes to avoid re-submitting the same content."""

    def __init__(self):
        self._hashes: Set[str] = set()
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if _SEEN_HASHES_FILE.exists():
            try:
                data = json.loads(_SEEN_HASHES_FILE.read_text(encoding='utf-8'))
                self._hashes = set(data.get('hashes', []))
                logger.debug('[harvester] Loaded %d seen hashes', len(self._hashes))
            except Exception:
                self._hashes = set()

    def _save(self) -> None:
        try:
            _STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = _SEEN_HASHES_FILE.with_suffix('.tmp')
            tmp.write_text(json.dumps({'hashes': list(self._hashes),
                                       'updated': _utcnow_iso()}),
                           encoding='utf-8')
            tmp.replace(_SEEN_HASHES_FILE)
        except Exception as exc:
            logger.debug('[harvester] Could not save seen hashes: %s', exc)

    def is_seen(self, h: str) -> bool:
        with self._lock:
            return h in self._hashes

    def mark_seen(self, h: str) -> None:
        with self._lock:
            self._hashes.add(h)
            # Trim if too large
            if len(self._hashes) > 50_000:
                self._hashes = set(list(self._hashes)[-40_000:])
        self._save()

    def check_and_mark(self, content: str) -> bool:
        """Returns True if content is NEW (not seen before). Marks it as seen."""
        h = _content_hash(content)
        if self.is_seen(h):
            return False
        self.mark_seen(h)
        return True


_seen = SeenHashes()


# ── Capture submission ────────────────────────────────────────────────────────

def _submit(source: str, content: str, role: str = 'assistant',
            metadata: Optional[dict] = None) -> bool:
    """Submit a capture. Returns True on success. Skips if content was already seen."""
    if not content or len(content.strip()) < 20:
        return False
    if not _seen.check_and_mark(content):
        logger.debug('[harvester] Skipping duplicate: %s (%d chars)', source, len(content))
        return False

    payload = json.dumps({
        'source':      source,
        'content':     content[:MAX_CONV_BYTES],
        'role':        role,
        'terminal_id': f'harvester_{source}',
        'metadata':    metadata or {},
    }).encode('utf-8')

    import urllib.request
    for url in (CAPTURE_URL, NEURON_URL):
        try:
            headers = {'Content-Type': 'application/json'}
            if '8095' in url:
                headers['X-CVG-Key'] = NEURON_KEY
            req = urllib.request.Request(url, data=payload, headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp.read()
            return True
        except Exception:
            continue
    return False


# ── LOCAL HARVESTERS ──────────────────────────────────────────────────────────

class ClineHarvester:
    """
    Reads Cline task conversations from:
      %APPDATA%\\Code\\User\\globalStorage\\saoudrizwan.claude-dev\\tasks\\<task_id>\\api_conversation_history.json

    Each task directory contains:
      - api_conversation_history.json  — full message history array
      - ui_messages.json               — UI-level messages
      - task_metadata.json             — task info
    """

    def __init__(self):
        appdata = Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming'))
        self.base_dirs = [
            appdata / 'Code/User/globalStorage/saoudrizwan.claude-dev/tasks',
            appdata / 'Code - Insiders/User/globalStorage/saoudrizwan.claude-dev/tasks',
        ]
        self._seen_tasks: Set[str] = set()
        self._last_mtime: Dict[str, float] = {}

    def harvest(self) -> int:
        """Harvest new/updated Cline conversations. Returns count submitted."""
        submitted = 0
        for base in self.base_dirs:
            if not base.exists():
                continue
            try:
                task_dirs = [d for d in base.iterdir() if d.is_dir()]
            except Exception:
                continue

            for task_dir in sorted(task_dirs, key=lambda d: d.stat().st_mtime, reverse=True)[:50]:
                try:
                    task_id = task_dir.name
                    # Check if any file in task changed since last scan
                    conv_file = task_dir / 'api_conversation_history.json'
                    if not conv_file.exists():
                        conv_file = task_dir / 'ui_messages.json'
                    if not conv_file.exists():
                        continue

                    mtime = conv_file.stat().st_mtime
                    if self._last_mtime.get(str(conv_file)) == mtime:
                        continue  # unchanged
                    self._last_mtime[str(conv_file)] = mtime

                    raw = conv_file.read_text(encoding='utf-8', errors='replace')
                    data = json.loads(raw)

                    # Extract conversation as readable text
                    lines = [f'[cline task {task_id[:12]}]']
                    if isinstance(data, list):
                        messages = data
                    elif isinstance(data, dict):
                        messages = data.get('messages', data.get('history', [data]))
                    else:
                        continue

                    for msg in messages[-20:]:  # last 20 messages
                        if not isinstance(msg, dict):
                            continue
                        role = msg.get('role', msg.get('type', '?'))
                        # Extract text content
                        content_raw = msg.get('content', '')
                        if isinstance(content_raw, list):
                            text = ' '.join(
                                c.get('text', '') if isinstance(c, dict) else str(c)
                                for c in content_raw
                            )
                        elif isinstance(content_raw, str):
                            text = content_raw
                        else:
                            text = str(content_raw)
                        text = text.strip()[:500]
                        if text:
                            lines.append(f'[{role}] {text}')

                    if len(lines) > 1:
                        content_str = '\n'.join(lines)
                        if _submit('cline', content_str, role='assistant',
                                   metadata={'task_id': task_id, 'source_file': str(conv_file)}):
                            submitted += 1
                            logger.debug('[harvester/cline] task %s: submitted', task_id[:12])
                except Exception as exc:
                    logger.debug('[harvester/cline] task error: %s', exc)

        return submitted


class ClaudeAppHarvester:
    """
    Reads Claude Desktop app conversation cache from:
      %LOCALAPPDATA%\\Claude\\  (various cache/log files)

    Claude Desktop stores data in Chromium-style leveldb/JSON.
    We read any readable JSON/text files we can find.
    """

    def __init__(self):
        lad = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local'))
        self.base_dirs = [
            lad / 'Claude',
            lad / 'AnthropicClaude',
            Path(os.environ.get('APPDATA', '')) / 'Claude',
        ]
        self._last_mtime: Dict[str, float] = {}

    def harvest(self) -> int:
        submitted = 0
        for base in self.base_dirs:
            if not base.exists():
                continue
            # Look for conversation-like JSON files
            for p in base.rglob('*.json'):
                try:
                    if p.stat().st_size < 100 or p.stat().st_size > 5_000_000:
                        continue
                    mtime = p.stat().st_mtime
                    if self._last_mtime.get(str(p)) == mtime:
                        continue
                    self._last_mtime[str(p)] = mtime

                    raw = p.read_text(encoding='utf-8', errors='replace')
                    # Quick check — does it look like conversation data?
                    if not any(k in raw for k in ('"role"', '"content"', '"message"',
                                                   '"human"', '"assistant"', '"text"')):
                        continue
                    data = json.loads(raw)
                    text = self._extract_text(data, p.name)
                    if text and len(text) > 50:
                        if _submit('claude_app', text, role='assistant',
                                   metadata={'file': str(p.relative_to(base))}):
                            submitted += 1
                except Exception:
                    pass

            # Also read any .log or .txt files
            for p in base.rglob('*.log'):
                try:
                    if p.stat().st_size < 100:
                        continue
                    mtime = p.stat().st_mtime
                    if self._last_mtime.get(str(p)) == mtime:
                        continue
                    self._last_mtime[str(p)] = mtime
                    text = p.read_text(encoding='utf-8', errors='replace')[-3000:]
                    if len(text) > 100:
                        if _submit('claude_app', f'[claude log {p.name}]\n{text}',
                                   role='system',
                                   metadata={'file': p.name}):
                            submitted += 1
                except Exception:
                    pass
        return submitted

    def _extract_text(self, data: Any, filename: str) -> str:
        lines = [f'[claude_app {filename}]']
        if isinstance(data, list):
            for item in data[-20:]:
                if isinstance(item, dict):
                    role = item.get('role', item.get('type', '?'))
                    content = item.get('content', item.get('text', item.get('message', '')))
                    if isinstance(content, list):
                        text = ' '.join(c.get('text', '') if isinstance(c, dict) else str(c)
                                        for c in content)
                    else:
                        text = str(content)
                    text = text.strip()[:400]
                    if text:
                        lines.append(f'[{role}] {text}')
        elif isinstance(data, dict):
            for key in ('messages', 'history', 'conversation', 'chat'):
                if key in data:
                    return self._extract_text(data[key], filename)
            # Direct dict — just dump relevant fields
            for k, v in list(data.items())[:10]:
                if isinstance(v, str) and len(v) > 20:
                    lines.append(f'  {k}: {v[:200]}')
        return '\n'.join(lines) if len(lines) > 1 else ''


class CopilotChatHarvester:
    """
    Reads GitHub Copilot Chat history from:
      %APPDATA%\\Code\\User\\globalStorage\\github.copilot-chat\\

    Copilot stores sessions in JSON files in this directory.
    """

    def __init__(self):
        appdata = Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming'))
        self.base_dirs = [
            appdata / 'Code/User/globalStorage/github.copilot-chat',
            appdata / 'Code - Insiders/User/globalStorage/github.copilot-chat',
        ]
        self._last_mtime: Dict[str, float] = {}

    def harvest(self) -> int:
        submitted = 0
        for base in self.base_dirs:
            if not base.exists():
                continue
            for p in sorted(base.rglob('*.json'),
                            key=lambda f: f.stat().st_mtime, reverse=True)[:30]:
                try:
                    if p.stat().st_size < 50:
                        continue
                    mtime = p.stat().st_mtime
                    if self._last_mtime.get(str(p)) == mtime:
                        continue
                    self._last_mtime[str(p)] = mtime

                    raw = p.read_text(encoding='utf-8', errors='replace')
                    if not any(k in raw for k in ('"role"', '"content"', '"message"')):
                        continue
                    data = json.loads(raw)
                    text = self._extract(data, p.name)
                    if text and len(text) > 50:
                        if _submit('copilot', text, role='assistant',
                                   metadata={'file': p.name}):
                            submitted += 1
                except Exception:
                    pass
        return submitted

    def _extract(self, data: Any, filename: str) -> str:
        lines = [f'[copilot {filename}]']
        messages = []
        if isinstance(data, list):
            messages = data
        elif isinstance(data, dict):
            for k in ('messages', 'turns', 'history', 'session'):
                if k in data and isinstance(data[k], list):
                    messages = data[k]
                    break
        for msg in messages[-15:]:
            if not isinstance(msg, dict):
                continue
            role = msg.get('role', '?')
            content = msg.get('content', msg.get('message', msg.get('text', '')))
            if isinstance(content, list):
                text = ' '.join(c.get('text', '') if isinstance(c, dict) else str(c)
                                for c in content)
            else:
                text = str(content)
            text = text.strip()[:400]
            if text:
                lines.append(f'[{role}] {text}')
        return '\n'.join(lines) if len(lines) > 1 else ''


class AiderHarvester:
    """Reads Aider chat history from ~/.aider.chat.history.md"""

    def __init__(self):
        home = Path.home()
        self.history_files = [
            home / '.aider.chat.history.md',
            home / '.aider' / 'chat.history.md',
            Path(os.getcwd()) / '.aider.chat.history.md',
        ]
        self._last_size: Dict[str, int] = {}

    def harvest(self) -> int:
        submitted = 0
        for hist_file in self.history_files:
            if not hist_file.exists():
                continue
            try:
                size = hist_file.stat().st_size
                last = self._last_size.get(str(hist_file), 0)
                if size == last:
                    continue
                self._last_size[str(hist_file)] = size
                # Read only new content (tail)
                text = hist_file.read_text(encoding='utf-8', errors='replace')
                new_text = text[last:last + MAX_CONV_BYTES] if last else text[-MAX_CONV_BYTES:]
                if new_text.strip():
                    if _submit('aider', f'[aider history]\n{new_text}',
                               role='assistant', metadata={'file': str(hist_file)}):
                        submitted += 1
            except Exception:
                pass
        return submitted


class LLMCliHarvester:
    """Reads Simon Willison's LLM CLI conversation logs."""

    def __init__(self):
        lad = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData/Local'))
        home = Path.home()
        self.log_dirs = [
            lad / 'io.datasette.llm',
            home / '.config/io.datasette.llm',
            home / 'Library/Application Support/io.datasette.llm',  # macOS
        ]
        self._last_mtime: Dict[str, float] = {}

    def harvest(self) -> int:
        submitted = 0
        for base in self.log_dirs:
            if not base.exists():
                continue
            for p in base.rglob('*.json'):
                try:
                    if p.stat().st_size < 50:
                        continue
                    mtime = p.stat().st_mtime
                    if self._last_mtime.get(str(p)) == mtime:
                        continue
                    self._last_mtime[str(p)] = mtime
                    raw = p.read_text(encoding='utf-8', errors='replace')
                    if _submit('llm_cli', f'[llm_cli {p.name}]\n{raw[:MAX_CONV_BYTES]}',
                               role='assistant', metadata={'file': p.name}):
                        submitted += 1
                except Exception:
                    pass
        return submitted


# ── REMOTE HIVE HARVESTER ─────────────────────────────────────────────────────

class HiveHarvester:
    """
    Harvests AI history from remote machines in the CVG Hive-0 cluster via SSH.

    For each node, runs a remote script that:
      1. Checks for Cline/Claude/Aider/LLM history directories
      2. Cats recent conversation files
      3. Returns the content as JSON

    Requires key-based SSH auth already configured on the Hive nodes.
    SSH keys should be in ~/.ssh/ with Host entries in ~/.ssh/config (or standard).
    """

    REMOTE_HARVEST_SCRIPT = r"""
python3 -c "
import os, json, pathlib, sys
home = pathlib.Path.home()
results = []
# Cline tasks
cline_base = home / '.config/Code/User/globalStorage/saoudrizwan.claude-dev/tasks'
if cline_base.exists():
    for task_dir in sorted(cline_base.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True)[:10]:
        cf = task_dir / 'api_conversation_history.json'
        if cf.exists() and cf.stat().st_size > 100:
            try:
                results.append({'source':'cline','file':str(cf),'content':cf.read_text(errors='replace')[-3000:]})
            except: pass
# Aider
for af in [home/'.aider.chat.history.md', home/'.aider/chat.history.md']:
    if af.exists() and af.stat().st_size > 100:
        try:
            results.append({'source':'aider','file':str(af),'content':af.read_text(errors='replace')[-3000:]})
        except: pass
# LLM CLI
for ld in [home/'.config/io.datasette.llm', pathlib.Path('/root/.config/io.datasette.llm')]:
    if ld.exists():
        for p in list(ld.rglob('*.json'))[:5]:
            try:
                results.append({'source':'llm_cli','file':p.name,'content':p.read_text(errors='replace')[-2000:]})
            except: pass
print(json.dumps(results))
" 2>/dev/null
"""

    def __init__(self, nodes: Optional[List[str]] = None):
        self.nodes = nodes or _DEFAULT_HIVE_NODES
        self._last_mtime: Dict[str, str] = {}  # node+file -> content hash

    def harvest(self) -> int:
        """Harvest all nodes in PARALLEL (ThreadPoolExecutor) — avoids 11 x 15s serial wait."""
        submitted = 0
        max_workers = min(len(self.nodes), 8)  # cap at 8 concurrent SSH connections

        def _harvest_node(node: str) -> int:
            count = 0
            try:
                output = self._ssh_harvest(node)
                if not output:
                    return 0
                items = json.loads(output)
                for item in items:
                    src     = item.get('source', 'hive')
                    fname   = item.get('file', '')
                    content = item.get('content', '')
                    if content and len(content) > 50:
                        node_host  = node.split('@')[-1]
                        source_tag = f'{src}_{node_host}'
                        if _submit(source_tag, content, role='assistant',
                                   metadata={'node': node, 'file': fname}):
                            count += 1
                            logger.info('[harvester/hive] %s: captured %s (%d chars)',
                                        node, src, len(content))
            except Exception as exc:
                logger.debug('[harvester/hive] %s error: %s', node, exc)
            return count

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers,
                                                   thread_name_prefix='hive-harvest') as exe:
            futures = {exe.submit(_harvest_node, node): node for node in self.nodes}
            for future in concurrent.futures.as_completed(futures, timeout=60):
                try:
                    submitted += future.result()
                except Exception as exc:
                    logger.debug('[harvester/hive] future error: %s', exc)

        return submitted

    def _ssh_harvest(self, node: str, timeout: int = 15) -> Optional[str]:
        """Run the remote harvest script via SSH. Returns stdout or None."""
        try:
            result = subprocess.run(
                ['ssh',
                 '-o', 'StrictHostKeyChecking=no',
                 '-o', 'ConnectTimeout=8',
                 '-o', 'BatchMode=yes',      # fail if key auth not available
                 '-o', 'LogLevel=ERROR',
                 node,
                 self.REMOTE_HARVEST_SCRIPT.strip(),
                 ],
                capture_output=True, text=True, timeout=timeout,
                encoding='utf-8', errors='replace',
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except subprocess.TimeoutExpired:
            logger.debug('[harvester/hive] %s: SSH timeout', node)
        except Exception as exc:
            logger.debug('[harvester/hive] %s: SSH error: %s', node, exc)
        return None


# ── Main Harvester ────────────────────────────────────────────────────────────

class HistoryHarvester:
    """
    Orchestrates all local and remote AI history harvesters.
    Runs as background threads embedded in Neuron or standalone.
    """

    def __init__(self):
        self._harvesters = [
            ClineHarvester(),
            ClaudeAppHarvester(),
            CopilotChatHarvester(),
            AiderHarvester(),
            LLMCliHarvester(),
        ]
        self._hive = HiveHarvester()
        self._running = False
        self._stats: Dict[str, int] = {
            'cline': 0, 'claude_app': 0, 'copilot': 0,
            'aider': 0, 'llm_cli': 0, 'hive': 0, 'total': 0,
        }
        self._lock = threading.Lock()

    def harvest_local_once(self) -> int:
        """Run one local harvest cycle. Returns total items submitted."""
        total = 0
        names = ['cline', 'claude_app', 'copilot', 'aider', 'llm_cli']
        for h, name in zip(self._harvesters, names):
            try:
                n = h.harvest()
                if n:
                    with self._lock:
                        self._stats[name] += n
                        self._stats['total'] += n
                    logger.info('[harvester/%s] %d new items submitted', name, n)
                total += n
            except Exception as exc:
                logger.warning('[harvester/%s] error: %s', name, exc)
        return total

    def harvest_hive_once(self) -> int:
        """Run one hive harvest cycle across all remote nodes."""
        try:
            n = self._hive.harvest()
            if n:
                with self._lock:
                    self._stats['hive'] += n
                    self._stats['total'] += n
            return n
        except Exception as exc:
            logger.warning('[harvester/hive] error: %s', exc)
            return 0

    def start(self, daemon: bool = True) -> None:
        """Start background harvest loops."""
        self._running = True

        # Local harvest thread
        def _local_loop():
            # Run immediately on first start
            self.harvest_local_once()
            while self._running:
                time.sleep(HARVEST_INTERVAL)
                if not self._running:
                    break
                self.harvest_local_once()

        # Hive harvest thread
        def _hive_loop():
            time.sleep(10)  # brief delay before first hive harvest
            self.harvest_hive_once()
            while self._running:
                time.sleep(HIVE_HARVEST_INTERVAL)
                if not self._running:
                    break
                self.harvest_hive_once()

        for target, name in [(_local_loop, 'local'), (_hive_loop, 'hive')]:
            t = threading.Thread(target=target, name=f'cvg-harvester-{name}', daemon=daemon)
            t.start()

        logger.info('[harvester] Started — local interval=%ds hive interval=%ds nodes=%d',
                    HARVEST_INTERVAL, HIVE_HARVEST_INTERVAL, len(self._hive.nodes))

    def stop(self) -> None:
        self._running = False

    @property
    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)


# ── Singleton ─────────────────────────────────────────────────────────────────

_harvester: Optional[HistoryHarvester] = None


def get_harvester() -> HistoryHarvester:
    global _harvester
    if _harvester is None:
        _harvester = HistoryHarvester()
    return _harvester


def start_history_harvester() -> None:
    """Start the history harvester — called from Neuron lifespan."""
    h = get_harvester()
    try:
        h.start(daemon=True)
        logger.info('[harvester] History harvester started')
    except Exception as exc:
        logger.warning('[harvester] Could not start harvester: %s', exc)


def get_harvester_stats() -> dict:
    global _harvester
    if _harvester is None:
        return {'running': False}
    return {
        'running': _harvester._running,
        'stats': _harvester.stats,
        'hive_nodes': _harvester._hive.nodes,
        'local_harvesters': ['cline', 'claude_app', 'copilot', 'aider', 'llm_cli'],
        'intervals': {
            'local_seconds': HARVEST_INTERVAL,
            'hive_seconds': HIVE_HARVEST_INTERVAL,
        },
    }


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse, signal

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [CVG-HARVESTER] %(levelname)s %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    parser = argparse.ArgumentParser(description='CVG Neuron AI History Harvester')
    parser.add_argument('--once', action='store_true', help='Run one cycle and exit')
    parser.add_argument('--hive', action='store_true', help='Include hive nodes in --once')
    parser.add_argument('--list-nodes', action='store_true', help='List configured hive nodes')
    args = parser.parse_args()

    if args.list_nodes:
        print('Configured hive nodes:')
        for n in _DEFAULT_HIVE_NODES:
            print(f'  {n}')
        sys.exit(0)

    h = HistoryHarvester()

    if args.once:
        print('Running local harvest...')
        n = h.harvest_local_once()
        print(f'Local: {n} items submitted')
        if args.hive:
            print('Running hive harvest...')
            n2 = h.harvest_hive_once()
            print(f'Hive: {n2} items submitted')
        print(f'Stats: {h.stats}')
        sys.exit(0)

    h.start(daemon=False)

    def _stop(sig, frame):
        print('\n[harvester] Stopping...')
        h.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    print('[CVG Harvester] Running. Press Ctrl+C to stop.')
    while True:
        time.sleep(60)
        print('[CVG Harvester] Stats: %s' % h.stats)
