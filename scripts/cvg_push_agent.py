#!/usr/bin/env python3
# CVG Neuron -- Hive Node Push Agent v1
# (c) Clearview Geographic, LLC -- Proprietary and PRIVATE
#
# Lightweight agent that runs ON each Hive node (as a cron job or systemd timer)
# and PUSHES AI history/activity to Neuron's capture endpoint.
#
# Does NOT require Neuron to have SSH access to this node.
# The node initiates the connection TO Neuron.
#
# Install on each node:
#   scp scripts/cvg_push_agent.py root@<node>:/opt/cvg/push_agent.py
#   ssh root@<node> "python3 /opt/cvg/push_agent.py --install"
#
# Or use the deploy script:
#   bash scripts/deploy_hive_agents.sh
#
# Once installed, the agent runs every 5 minutes via cron and captures:
#   - Cline task history (if VS Code / Cline is on this node)
#   - Aider history (if aider is installed)
#   - LLM CLI logs
#   - Docker container logs for any running AI containers
#   - Shell command history (AI-related commands only)
#   - Process list snapshots (running AI processes)

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Configuration (set via env or edit here) ─────────────────────────────────

NEURON_HOST     = os.getenv('CVG_NEURON_HOST', '10.10.10.200')
NEURON_PORT     = int(os.getenv('CVG_NEURON_PORT', '8095'))
NEURON_KEY      = os.getenv('CVG_INTERNAL_KEY', 'cvg-internal-2026')
CAPTURE_PORT    = int(os.getenv('CVG_CAPTURE_PORT', '8098'))
MAX_BYTES       = int(os.getenv('CVG_PUSH_MAX_BYTES', '5000'))
STATE_FILE      = Path(os.getenv('CVG_PUSH_STATE',
    '/opt/cvg/push_agent_state.json'))
NODE_ID         = os.getenv('CVG_NODE_ID', platform.node())

# Endpoints to try (daemon first, then API)
_CAPTURE_URLS = [
    f'http://{NEURON_HOST}:{CAPTURE_PORT}/capture',
    f'http://{NEURON_HOST}:{NEURON_PORT}/api/memory/capture',
]


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _hash(text: str) -> str:
    return hashlib.md5(text.encode('utf-8', errors='replace'),
                       usedforsecurity=False).hexdigest()[:16]


# ── State (seen hashes, file offsets) ────────────────────────────────────────

class State:
    def __init__(self):
        self._data: Dict[str, Any] = {'seen': [], 'offsets': {}}
        self._load()

    def _load(self) -> None:
        try:
            if STATE_FILE.exists():
                self._data = json.loads(STATE_FILE.read_text())
        except Exception:
            pass

    def save(self) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.with_suffix('.tmp')
            tmp.write_text(json.dumps(self._data))
            tmp.replace(STATE_FILE)
        except Exception:
            pass

    def is_seen(self, h: str) -> bool:
        return h in self._data.get('seen', [])

    def mark_seen(self, h: str) -> None:
        seen = self._data.setdefault('seen', [])
        seen.append(h)
        if len(seen) > 10_000:
            self._data['seen'] = seen[-8_000:]

    def get_offset(self, key: str) -> int:
        return self._data.get('offsets', {}).get(key, 0)

    def set_offset(self, key: str, val: int) -> None:
        self._data.setdefault('offsets', {})[key] = val


_state = State()


# ── Submission ────────────────────────────────────────────────────────────────

def _push(source: str, content: str, role: str = 'assistant',
          metadata: Optional[dict] = None) -> bool:
    """Push a capture to Neuron. Returns True on success."""
    if not content or len(content.strip()) < 10:
        return False

    h = _hash(content)
    if _state.is_seen(h):
        return False  # already sent

    payload = json.dumps({
        'source':      source,
        'content':     content[:MAX_BYTES],
        'role':        role,
        'terminal_id': f'push_agent_{NODE_ID}',
        'metadata':    {
            'node_id':   NODE_ID,
            'hostname':  platform.node(),
            'timestamp': _utcnow(),
            **(metadata or {}),
        },
    }).encode('utf-8')

    for url in _CAPTURE_URLS:
        try:
            headers = {'Content-Type': 'application/json'}
            if str(NEURON_PORT) in url:
                headers['X-CVG-Key'] = NEURON_KEY
            req = urllib.request.Request(url, data=payload, headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=8) as resp:
                resp.read()
            _state.mark_seen(h)
            return True
        except Exception:
            continue
    return False


# ── Collectors ────────────────────────────────────────────────────────────────

def collect_cline_history() -> int:
    """Collect Cline task history (VS Code running on this node)."""
    submitted = 0
    home = Path.home()
    bases = [
        home / '.config/Code/User/globalStorage/saoudrizwan.claude-dev/tasks',
        home / '.local/share/Code/User/globalStorage/saoudrizwan.claude-dev/tasks',
    ]
    for base in bases:
        if not base.exists():
            continue
        try:
            for task_dir in sorted(base.iterdir(),
                                   key=lambda d: d.stat().st_mtime, reverse=True)[:20]:
                conv = task_dir / 'api_conversation_history.json'
                if not conv.exists():
                    conv = task_dir / 'ui_messages.json'
                if not conv.exists() or conv.stat().st_size < 100:
                    continue
                try:
                    raw = conv.read_text(encoding='utf-8', errors='replace')
                    data = json.loads(raw)
                    messages = data if isinstance(data, list) else data.get('messages', [])
                    lines = [f'[cline/{NODE_ID} task {task_dir.name[:12]}]']
                    for msg in messages[-15:]:
                        if not isinstance(msg, dict):
                            continue
                        role = msg.get('role', '?')
                        c = msg.get('content', '')
                        if isinstance(c, list):
                            c = ' '.join(x.get('text', '') if isinstance(x, dict)
                                         else str(x) for x in c)
                        c = str(c).strip()[:300]
                        if c:
                            lines.append(f'[{role}] {c}')
                    if len(lines) > 1:
                        if _push(f'cline_{NODE_ID}', '\n'.join(lines)):
                            submitted += 1
                except Exception:
                    pass
        except Exception:
            pass
    return submitted


def collect_aider_history() -> int:
    """Collect Aider chat history."""
    submitted = 0
    home = Path.home()
    for hist in [home / '.aider.chat.history.md',
                 home / '.aider' / 'chat.history.md',
                 Path('/root/.aider.chat.history.md')]:
        if not hist.exists():
            continue
        try:
            size = hist.stat().st_size
            offset = _state.get_offset(str(hist))
            if size <= offset:
                continue
            content = hist.read_text(encoding='utf-8', errors='replace')
            new_content = content[offset:offset + MAX_BYTES]
            if new_content.strip():
                if _push(f'aider_{NODE_ID}',
                         f'[aider/{NODE_ID}]\n{new_content}',
                         metadata={'file': str(hist)}):
                    _state.set_offset(str(hist), offset + len(new_content))
                    submitted += 1
        except Exception:
            pass
    return submitted


def collect_llm_cli() -> int:
    """Collect LLM CLI conversation logs."""
    submitted = 0
    home = Path.home()
    for base in [Path('/root/.config/io.datasette.llm'),
                 home / '.config/io.datasette.llm']:
        if not base.exists():
            continue
        try:
            for p in sorted(base.rglob('*.json'),
                            key=lambda f: f.stat().st_mtime, reverse=True)[:5]:
                try:
                    if p.stat().st_size < 50:
                        continue
                    content = p.read_text(encoding='utf-8', errors='replace')
                    if _push(f'llm_cli_{NODE_ID}',
                             f'[llm_cli/{NODE_ID} {p.name}]\n{content[:MAX_BYTES]}'):
                        submitted += 1
                except Exception:
                    pass
        except Exception:
            pass
    return submitted


def collect_docker_ai_logs() -> int:
    """Collect logs from Docker containers that appear to be running AI services."""
    submitted = 0
    ai_keywords = ('ollama', 'neuron', 'llm', 'gpt', 'claude', 'ai', 'inference',
                   'whisper', 'stable', 'diffusion', 'kobold', 'lmstudio', 'comfy')
    try:
        result = subprocess.run(
            ['docker', 'ps', '--format', '{{.Names}}\t{{.Image}}\t{{.Status}}'],
            capture_output=True, text=True, timeout=10,
            encoding='utf-8', errors='replace',
        )
        if result.returncode != 0:
            return 0

        for line in result.stdout.strip().split('\n'):
            if not line.strip():
                continue
            parts = line.split('\t')
            name = parts[0] if parts else ''
            image = parts[1] if len(parts) > 1 else ''
            status = parts[2] if len(parts) > 2 else ''

            is_ai = any(kw in name.lower() or kw in image.lower()
                        for kw in ai_keywords)
            if not is_ai:
                continue

            # Get the last 50 lines of logs
            try:
                log_result = subprocess.run(
                    ['docker', 'logs', '--tail', '50', name],
                    capture_output=True, text=True, timeout=8,
                    encoding='utf-8', errors='replace',
                )
                logs = (log_result.stdout + log_result.stderr)[-MAX_BYTES:]
                if logs.strip() and len(logs) > 50:
                    content = (f'[docker/{NODE_ID} container={name} image={image} '
                               f'status={status}]\n{logs}')
                    if _push(f'docker_{NODE_ID}', content, role='system',
                             metadata={'container': name, 'image': image}):
                        submitted += 1
            except Exception:
                pass
    except Exception:
        pass
    return submitted


def collect_shell_ai_commands() -> int:
    """Collect recent shell history entries that involve AI tools."""
    submitted = 0
    ai_cmds = ('claude', 'aider', 'llm', 'sgpt', 'gpt', 'ollama',
               'neuron', 'chatgpt', 'copilot', 'continue')
    home = Path.home()
    hist_files = [
        home / '.bash_history',
        home / '.zsh_history',
        Path('/root/.bash_history'),
        Path('/root/.zsh_history'),
    ]
    for hist_file in hist_files:
        if not hist_file.exists():
            continue
        try:
            size = hist_file.stat().st_size
            offset = _state.get_offset(str(hist_file))
            if size <= offset:
                continue
            raw = hist_file.read_bytes()
            new_raw = raw[offset:offset + MAX_BYTES]
            new_text = new_raw.decode('utf-8', errors='replace')

            # Filter to just AI-related commands
            ai_lines = []
            for line in new_text.split('\n'):
                line = line.strip()
                if any(cmd in line.lower() for cmd in ai_cmds):
                    ai_lines.append(line)

            if ai_lines:
                content = f'[shell_history/{NODE_ID}]\n' + '\n'.join(ai_lines[:50])
                if _push(f'shell_{NODE_ID}', content, role='user',
                         metadata={'file': str(hist_file)}):
                    _state.set_offset(str(hist_file), offset + len(new_raw))
                    submitted += 1
            else:
                # Advance offset even if no AI commands (don't re-scan)
                _state.set_offset(str(hist_file), offset + len(new_raw))
        except Exception:
            pass
    return submitted


def collect_running_ai_processes() -> int:
    """Snapshot running AI-related processes and submit if any found."""
    ai_procs = ('ollama', 'python', 'node', 'uvicorn', 'fastapi',
                'llama', 'whisper', 'stable', 'aider', 'llm')
    try:
        result = subprocess.run(
            ['ps', 'aux'],
            capture_output=True, text=True, timeout=5,
            encoding='utf-8', errors='replace',
        )
        if result.returncode != 0:
            return 0

        ai_lines = []
        for line in result.stdout.split('\n'):
            if any(proc in line.lower() for proc in ai_procs):
                if 'push_agent' not in line and 'grep' not in line:
                    ai_lines.append(line[:200])

        if ai_lines:
            content = f'[processes/{NODE_ID} at {_utcnow()}]\n' + '\n'.join(ai_lines[:20])
            if _push(f'procs_{NODE_ID}', content, role='system'):
                return 1
    except Exception:
        pass
    return 0


# ── Main ──────────────────────────────────────────────────────────────────────

def run_push_cycle() -> dict:
    """Run a full push cycle. Returns stats dict."""
    results = {
        'cline':      collect_cline_history(),
        'aider':      collect_aider_history(),
        'llm_cli':    collect_llm_cli(),
        'docker':     collect_docker_ai_logs(),
        'shell':      collect_shell_ai_commands(),
        'processes':  collect_running_ai_processes(),
        'node':       NODE_ID,
        'timestamp':  _utcnow(),
    }
    results['total'] = sum(v for k, v in results.items()
                           if isinstance(v, int))
    _state.save()
    return results


def install_cron() -> None:
    """Install this script as a cron job running every 5 minutes."""
    script_path = os.path.abspath(__file__)
    cron_line = f'*/5 * * * * python3 {script_path} --push >> /var/log/cvg-push-agent.log 2>&1'
    try:
        existing = subprocess.run(['crontab', '-l'],
                                  capture_output=True, text=True).stdout
        if script_path in existing:
            print(f'[push-agent] Cron already installed for {script_path}')
            return
        new_cron = existing.rstrip('\n') + '\n' + cron_line + '\n'
        p = subprocess.run(['crontab', '-'], input=new_cron, text=True)
        if p.returncode == 0:
            print(f'[push-agent] Cron installed: {cron_line}')
        else:
            print(f'[push-agent] Failed to install cron (exit {p.returncode})')
    except Exception as exc:
        print(f'[push-agent] Cron install error: {exc}')


def install_systemd() -> None:
    """Install as a systemd timer (alternative to cron, preferred on modern Linux)."""
    script_path = os.path.abspath(__file__)
    service_content = f"""[Unit]
Description=CVG Neuron Push Agent
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {script_path} --push
StandardOutput=journal
StandardError=journal
"""
    timer_content = """[Unit]
Description=CVG Neuron Push Agent Timer

[Timer]
OnBootSec=60s
OnUnitActiveSec=5min
Persistent=true

[Install]
WantedBy=timers.target
"""
    try:
        service_path = Path('/etc/systemd/system/cvg-push-agent.service')
        timer_path   = Path('/etc/systemd/system/cvg-push-agent.timer')
        service_path.write_text(service_content)
        timer_path.write_text(timer_content)
        subprocess.run(['systemctl', 'daemon-reload'])
        subprocess.run(['systemctl', 'enable', '--now', 'cvg-push-agent.timer'])
        print('[push-agent] systemd timer installed and started')
    except Exception as exc:
        print(f'[push-agent] systemd install failed: {exc}')
        print('[push-agent] Falling back to cron...')
        install_cron()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='CVG Neuron Hive Node Push Agent')
    parser.add_argument('--push',    action='store_true', help='Run one push cycle')
    parser.add_argument('--install', action='store_true', help='Install as cron/systemd timer')
    parser.add_argument('--loop',    action='store_true', help='Run continuously (every 5 min)')
    parser.add_argument('--test',    action='store_true', help='Test connectivity to Neuron')
    args = parser.parse_args()

    if args.test:
        print(f'[push-agent] Testing connectivity to Neuron at {NEURON_HOST}:{NEURON_PORT}...')
        ok = _push('push_agent_test',
                   f'Push agent test from {NODE_ID} at {_utcnow()}',
                   role='system',
                   metadata={'test': True})
        print(f'[push-agent] Result: {"OK - Neuron reached" if ok else "FAILED - check NEURON_HOST/PORT/KEY"}')
        sys.exit(0 if ok else 1)

    elif args.install:
        # Prefer systemd on Linux, fall back to cron
        if Path('/etc/systemd/system').exists():
            install_systemd()
        else:
            install_cron()

    elif args.push:
        results = run_push_cycle()
        total = results.get('total', 0)
        if total > 0:
            print(f'[push-agent] {NODE_ID}: {total} items pushed to Neuron')
        # Always exit 0 (don't fail cron)
        sys.exit(0)

    elif args.loop:
        print(f'[push-agent] Running loop on {NODE_ID} (5 min interval)...')
        while True:
            try:
                results = run_push_cycle()
                total = results.get('total', 0)
                if total:
                    print(f'[push-agent] Pushed {total} items from {NODE_ID}')
            except Exception as exc:
                print(f'[push-agent] Cycle error: {exc}')
            time.sleep(300)  # 5 minutes
    else:
        # Default: run one push cycle silently
        run_push_cycle()
        sys.exit(0)
