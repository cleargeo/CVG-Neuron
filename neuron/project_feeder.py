# CVG Neuron -- Local Project Feeder v1
# (c) Clearview Geographic, LLC -- Proprietary and PRIVATE
#
# Autonomously monitors local CVG support engine project directories and
# feeds changes, findings, configs, and API outputs into Neuron's capture memory.
#
# Watched projects (configurable via env or CVG_PROJECT_DIRS):
#   - CVG_Audit_VM              → source: cvg_audit
#   - CVG_DNS_SupportEngine     → source: cvg_dns
#   - CVG_Containerization_SupportEngine → source: cvg_container
#   - CVG (umbrella / shared)   → source: cvg_shared
#
# Feed modes (all run concurrently):
#   1. File watcher: detects changed .py/.json/.yml/.env/.md files, captures diffs/summaries
#   2. API poller:   polls each project's HTTP API for fresh data summaries
#   3. Git log:      captures new git commits across all watched repos
#
# Runs as a background thread — embedded in Neuron via start_project_feeder().
# Can also run standalone: python -m neuron.project_feeder

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger('cvg.neuron.project_feeder')

# ── Default watched directory config ─────────────────────────────────────────

_DEFAULT_PROJECTS: List[Dict[str, Any]] = [
    {
        'name':    'cvg_audit',
        'path':    os.getenv('CVG_PROJ_AUDIT',
                             r'G:\07_APPLICATIONS_TOOLS\CVG\CVG_Audit_VM'),
        'api_url': os.getenv('CVG_AUDIT_API', 'http://10.10.10.220:8001/api/summary'),
        'api_key': os.getenv('CVG_INTERNAL_KEY', 'cvg-internal-2026'),
        'priority_patterns': ['results_api', 'findings', 'alerts', 'wazuh', 'trivy', '.env'],
    },
    {
        'name':    'cvg_dns',
        'path':    os.getenv('CVG_PROJ_DNS',
                             r'G:\07_APPLICATIONS_TOOLS\CVG\CVG_DNS_SupportEngine'),
        'api_url': os.getenv('CVG_DNS_API', 'http://localhost:8094/api/status'),
        'api_key': os.getenv('CVG_INTERNAL_KEY', 'cvg-internal-2026'),
        'priority_patterns': ['zones', 'records', 'resolv', 'bind', '.env'],
    },
    {
        'name':    'cvg_dns_alt',
        'path':    os.getenv('CVG_PROJ_DNS_ALT',
                             r'G:\07_APPLICATIONS_TOOLS\CVG_DNS_SupportEngine'),
        'api_url': os.getenv('CVG_DNS_ALT_API', 'http://localhost:8094/api/status'),
        'api_key': os.getenv('CVG_INTERNAL_KEY', 'cvg-internal-2026'),
        'priority_patterns': ['zones', 'records', 'resolv', 'bind', '.env'],
        'dedup_with': 'cvg_dns',  # merge with cvg_dns if same content
    },
    {
        'name':    'cvg_container',
        'path':    os.getenv('CVG_PROJ_CONTAINER',
                             r'G:\07_APPLICATIONS_TOOLS\CVG_Containerization_SupportEngine'),
        'api_url': os.getenv('CVG_CONTAINER_API', 'http://localhost:8091/api/summary'),
        'api_key': os.getenv('CVG_INTERNAL_KEY', 'cvg-internal-2026'),
        'priority_patterns': ['docker', 'containers', 'deploy', 'compose', '.env', 'telemetry'],
    },
]

# Load additional project dirs from env (colon-separated on Linux, semicolons on Windows)
_extra = os.getenv('CVG_PROJECT_DIRS', '')
if _extra:
    for p in _extra.replace(';', ':').split(':'):
        p = p.strip()
        if p:
            name = Path(p).name.lower().replace('-', '_').replace(' ', '_')
            _DEFAULT_PROJECTS.append({'name': name, 'path': p, 'api_url': None, 'api_key': None,
                                       'priority_patterns': []})

# ── Config ────────────────────────────────────────────────────────────────────

FILE_POLL_INTERVAL  = int(os.getenv('CVG_FEED_FILE_INTERVAL', '30'))   # seconds between file scans
API_POLL_INTERVAL   = int(os.getenv('CVG_FEED_API_INTERVAL',  '120'))  # seconds between API polls
GIT_POLL_INTERVAL   = int(os.getenv('CVG_FEED_GIT_INTERVAL',  '60'))   # seconds between git log checks
MAX_FILE_SIZE_BYTES = int(os.getenv('CVG_FEED_MAX_FILE_BYTES', '8000'))  # max bytes to capture per file

# File extensions to watch
WATCHED_EXTENSIONS = {
    '.py', '.json', '.yml', '.yaml', '.env', '.md', '.cfg',
    '.ini', '.conf', '.txt', '.sh', '.toml',
}
# Paths to skip
SKIP_PATTERNS = {
    '__pycache__', '.git', 'node_modules', '.venv', 'venv', 'env',
    '.mypy_cache', '.pytest_cache', 'dist', 'build', '.tox',
    'static', 'site-packages', '.eggs',
}

CAPTURE_URL  = os.getenv('CVG_CAPTURE_URL',  'http://127.0.0.1:8098/capture')
NEURON_URL   = os.getenv('CVG_NEURON_URL',   'http://localhost:8095/api/memory/capture')
NEURON_KEY   = os.getenv('CVG_INTERNAL_KEY', 'cvg-internal-2026')


# ── Helpers ───────────────────────────────────────────────────────────────────

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def _file_hash(path: Path) -> str:
    try:
        h = hashlib.md5(usedforsecurity=False)
        h.update(path.read_bytes()[:MAX_FILE_SIZE_BYTES])
        return h.hexdigest()
    except Exception:
        return ''


def _send_capture(source: str, content: str, role: str = 'system',
                  metadata: Optional[dict] = None) -> bool:
    """Send a capture to the daemon or Neuron API. Returns True on success."""
    import urllib.request
    if not content or len(content.strip()) < 10:
        return False

    payload = json.dumps({
        'source':      source,
        'content':     content[:MAX_FILE_SIZE_BYTES],
        'role':        role,
        'terminal_id': f'project_feeder_{source}',
        'metadata':    metadata or {},
    }).encode()

    for url in (CAPTURE_URL, NEURON_URL):
        try:
            headers = {'Content-Type': 'application/json'}
            if '8095' in url:
                headers['X-CVG-Key'] = NEURON_KEY
            req = urllib.request.Request(url, data=payload,
                                         headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=4) as resp:
                resp.read()
            return True
        except Exception:
            continue
    return False


def _read_file_excerpt(path: Path, max_bytes: int = MAX_FILE_SIZE_BYTES) -> str:
    """Read a file, returning up to max_bytes. Truncates gracefully."""
    try:
        raw = path.read_bytes()
        text = raw[:max_bytes].decode('utf-8', errors='replace')
        if len(raw) > max_bytes:
            text += f'\n... [truncated at {max_bytes} bytes of {len(raw)} total]'
        return text
    except Exception:
        return ''


def _run_git_log(repo_path: Path, n: int = 5) -> str:
    """Return the last n git commits as a formatted string."""
    try:
        result = subprocess.run(
            ['git', '-C', str(repo_path), 'log', f'-{n}',
             '--pretty=format:%H|%ai|%an|%s', '--no-merges'],
            capture_output=True, text=True, timeout=8,
            encoding='utf-8', errors='replace',
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = []
            for line in result.stdout.strip().split('\n'):
                parts = line.split('|', 3)
                if len(parts) == 4:
                    sha, ts, author, msg = parts
                    lines.append(f'  [{ts[:16]}] {sha[:8]} ({author}): {msg}')
            return '\n'.join(lines)
    except Exception:
        pass
    return ''


def _get_git_head(repo_path: Path) -> str:
    """Return current HEAD commit hash."""
    try:
        result = subprocess.run(
            ['git', '-C', str(repo_path), 'rev-parse', 'HEAD'],
            capture_output=True, text=True, timeout=5,
            encoding='utf-8', errors='replace',
        )
        return result.stdout.strip()[:12] if result.returncode == 0 else ''
    except Exception:
        return ''


def _fetch_api(url: str, key: Optional[str] = None, timeout: int = 8) -> Optional[dict]:
    """Fetch JSON from a project API endpoint."""
    import urllib.request
    if not url:
        return None
    try:
        headers = {'User-Agent': 'cvg-neuron-feeder/1.0'}
        if key:
            headers['X-CVG-Key'] = key
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        logger.debug('[feeder] API fetch failed (%s): %s', url, exc)
        return None


def _content_hash(text: str) -> str:
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()[:12]


# ── Project state tracker ─────────────────────────────────────────────────────

class ProjectState:
    """Tracks file hashes and git HEAD for a single project directory."""

    def __init__(self, project: dict):
        self.name     = project['name']
        self.path     = Path(project.get('path', ''))
        self.api_url  = project.get('api_url')
        self.api_key  = project.get('api_key')
        self.priority = project.get('priority_patterns', [])
        self.dedup_with = project.get('dedup_with')

        self._file_hashes: Dict[str, str] = {}   # path→hash
        self._git_head:   str = ''
        self._api_hash:   str = ''
        self._last_api_content: str = ''
        self._lock = threading.Lock()

    def exists(self) -> bool:
        return self.path.exists() and self.path.is_dir()

    def is_priority_file(self, path: Path) -> bool:
        """Returns True if this file matches a priority watch pattern."""
        name_lower = path.name.lower()
        path_str   = str(path).lower()
        return any(p.lower() in path_str or p.lower() in name_lower
                   for p in self.priority)

    def scan_files(self) -> List[Tuple[Path, str]]:
        """
        Scan the project directory for changed files.
        Returns list of (path, change_type) tuples: 'new', 'modified', 'deleted'.
        """
        if not self.exists():
            return []

        changes: List[Tuple[Path, str]] = []
        current_paths: set = set()

        for p in self.path.rglob('*'):
            # Skip unwanted directories/files
            if any(skip in p.parts for skip in SKIP_PATTERNS):
                continue
            if not p.is_file():
                continue
            if p.suffix.lower() not in WATCHED_EXTENSIONS:
                continue
            if p.stat().st_size > 500_000:  # skip very large files
                continue

            current_paths.add(str(p))
            with self._lock:
                old_hash = self._file_hashes.get(str(p))
            new_hash = _file_hash(p)

            if old_hash is None:
                changes.append((p, 'new'))
            elif old_hash != new_hash:
                changes.append((p, 'modified'))

            with self._lock:
                self._file_hashes[str(p)] = new_hash

        # Detect deletions
        with self._lock:
            known = set(self._file_hashes.keys())
        for old_path in known - current_paths:
            changes.append((Path(old_path), 'deleted'))
            with self._lock:
                self._file_hashes.pop(old_path, None)

        return changes

    def check_git(self) -> Optional[str]:
        """
        Check if git HEAD changed. Returns commit log string if new commits found, else None.
        """
        if not self.exists():
            return None
        new_head = _get_git_head(self.path)
        if not new_head:
            return None
        with self._lock:
            old_head = self._git_head
        if new_head != old_head:
            log = _run_git_log(self.path, n=5)
            with self._lock:
                self._git_head = new_head
            if old_head:  # only report if we had a previous head (not first run)
                return log
        return None

    def check_api(self) -> Optional[str]:
        """
        Poll the project API. Returns summary string if content changed, else None.
        """
        if not self.api_url:
            return None
        data = _fetch_api(self.api_url, self.api_key)
        if data is None:
            return None
        content = json.dumps(data, sort_keys=True)
        new_hash = _content_hash(content)
        with self._lock:
            old_hash = self._api_hash
        if new_hash != old_hash:
            with self._lock:
                self._api_hash = new_hash
                self._last_api_content = content
            if old_hash:  # not first poll
                return content[:3000]
            else:
                # First poll — always capture as baseline
                return content[:3000]
        return None


# ── Main Feeder ───────────────────────────────────────────────────────────────

class ProjectFeeder:
    """
    Autonomously feeds local CVG project directories into Neuron's memory capture tier.

    Three feed loops run in background threads:
      1. file_watcher_loop   — detects file changes every FILE_POLL_INTERVAL seconds
      2. api_poller_loop     — polls project HTTP APIs every API_POLL_INTERVAL seconds
      3. git_watcher_loop    — checks git HEAD every GIT_POLL_INTERVAL seconds

    All feeds go through the capture endpoint → NeuronMemory.capture tier →
    consolidation cycle → episodic + semantic memory.
    """

    def __init__(self, projects: Optional[List[dict]] = None):
        self._projects = [ProjectState(p) for p in (projects or _DEFAULT_PROJECTS)
                          if p.get('path')]
        self._running = False
        self._threads: List[threading.Thread] = []
        self._stats: Dict[str, int] = {
            'files_captured': 0,
            'api_captures': 0,
            'git_captures': 0,
            'errors': 0,
        }

    def start(self, daemon: bool = True) -> None:
        """Start all feeder threads."""
        self._running = True

        # Log which projects actually exist on disk
        for ps in self._projects:
            if ps.exists():
                logger.info('[feeder] Watching: %s -> %s', ps.name, ps.path)
            else:
                logger.debug('[feeder] Project dir not found (skipping): %s -> %s', ps.name, ps.path)

        loops = [
            ('file_watcher',  self._file_watcher_loop,  FILE_POLL_INTERVAL),
            ('api_poller',    self._api_poller_loop,     API_POLL_INTERVAL),
            ('git_watcher',   self._git_watcher_loop,    GIT_POLL_INTERVAL),
        ]
        for name, target, _ in loops:
            t = threading.Thread(target=target, name=f'cvg-feeder-{name}', daemon=daemon)
            t.start()
            self._threads.append(t)

        logger.info('[feeder] Started %d feed loops (file=%ds api=%ds git=%ds)',
                    len(loops), FILE_POLL_INTERVAL, API_POLL_INTERVAL, GIT_POLL_INTERVAL)

    def stop(self) -> None:
        self._running = False
        logger.info('[feeder] Stopped. Stats: %s', self._stats)

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    # ── Feed loop 1: file changes ─────────────────────────────────────────────

    def _file_watcher_loop(self) -> None:
        # Initial scan to populate hashes (don't capture on first run)
        for ps in self._projects:
            if ps.exists():
                ps.scan_files()  # populate hashes only
        logger.debug('[feeder/files] Initial hash scan complete for %d projects',
                     sum(1 for ps in self._projects if ps.exists()))

        while self._running:
            time.sleep(FILE_POLL_INTERVAL)
            if not self._running:
                break
            for ps in self._projects:
                if not ps.exists():
                    continue
                try:
                    changes = ps.scan_files()
                    if not changes:
                        continue

                    # Group changes into a single capture per project
                    changed_files = []
                    file_contents = []
                    for path, change_type in changes[:20]:  # cap at 20 files per cycle
                        changed_files.append(f'  [{change_type}] {path.relative_to(ps.path)}')
                        if change_type != 'deleted' and path.suffix in WATCHED_EXTENSIONS:
                            content = _read_file_excerpt(path, max_bytes=2000)
                            if len(content) > 20:
                                is_priority = ps.is_priority_file(path)
                                file_contents.append(
                                    f'=== {"[PRIORITY] " if is_priority else ""}'
                                    f'{path.relative_to(ps.path)} ===\n{content}'
                                )

                    summary = f'[{ps.name}] {len(changes)} file change(s) detected:\n'
                    summary += '\n'.join(changed_files[:10])
                    if file_contents:
                        summary += '\n\nFile contents:\n' + '\n\n'.join(file_contents[:5])

                    ok = _send_capture(
                        source=ps.name,
                        content=summary,
                        role='system',
                        metadata={
                            'feed_type':    'file_change',
                            'project_path': str(ps.path),
                            'files_changed': len(changes),
                            'timestamp':    _utcnow_iso(),
                        },
                    )
                    if ok:
                        self._stats['files_captured'] += len(changes)
                        logger.info('[feeder/files] %s: %d changes captured', ps.name, len(changes))
                    else:
                        self._stats['errors'] += 1

                except Exception as exc:
                    logger.warning('[feeder/files] %s error: %s', ps.name, exc)
                    self._stats['errors'] += 1

    # ── Feed loop 2: API polling ──────────────────────────────────────────────

    def _api_poller_loop(self) -> None:
        # Stagger startup to avoid all projects hitting APIs simultaneously
        time.sleep(5)

        while self._running:
            for ps in self._projects:
                if not ps.api_url or not self._running:
                    continue
                try:
                    content = ps.check_api()
                    if content:
                        ok = _send_capture(
                            source=ps.name,
                            content=f'[{ps.name}] API update from {ps.api_url}:\n{content}',
                            role='system',
                            metadata={
                                'feed_type':   'api_poll',
                                'api_url':     ps.api_url,
                                'timestamp':   _utcnow_iso(),
                            },
                        )
                        if ok:
                            self._stats['api_captures'] += 1
                            logger.info('[feeder/api] %s: API data captured (%d chars)',
                                        ps.name, len(content))
                        else:
                            self._stats['errors'] += 1
                except Exception as exc:
                    logger.debug('[feeder/api] %s error: %s', ps.name, exc)

            # Sleep between full poll cycles
            for _ in range(API_POLL_INTERVAL):
                if not self._running:
                    break
                time.sleep(1)

    # ── Feed loop 3: git log watching ─────────────────────────────────────────

    def _git_watcher_loop(self) -> None:
        # Initial state — populate HEAD without capturing
        for ps in self._projects:
            ps.check_git()

        while self._running:
            time.sleep(GIT_POLL_INTERVAL)
            if not self._running:
                break
            for ps in self._projects:
                if not ps.exists():
                    continue
                try:
                    log = ps.check_git()
                    if log:
                        ok = _send_capture(
                            source=ps.name,
                            content=f'[{ps.name}] New git commits detected:\n{log}',
                            role='system',
                            metadata={
                                'feed_type':    'git_commit',
                                'project_path': str(ps.path),
                                'timestamp':    _utcnow_iso(),
                            },
                        )
                        if ok:
                            self._stats['git_captures'] += 1
                            logger.info('[feeder/git] %s: commit changes captured', ps.name)
                        else:
                            self._stats['errors'] += 1
                except Exception as exc:
                    logger.debug('[feeder/git] %s error: %s', ps.name, exc)


# ── Singleton / embedding API ─────────────────────────────────────────────────

_feeder_instance: Optional[ProjectFeeder] = None


def get_project_feeder() -> ProjectFeeder:
    global _feeder_instance
    if _feeder_instance is None:
        _feeder_instance = ProjectFeeder()
    return _feeder_instance


def start_project_feeder() -> None:
    """Start the project feeder — called from Neuron lifespan or capture daemon."""
    feeder = get_project_feeder()
    try:
        feeder.start(daemon=True)
        active = sum(1 for ps in feeder._projects if ps.exists())
        logger.info('[feeder] Project feeder started — %d/%d projects exist on disk',
                    active, len(feeder._projects))
    except Exception as exc:
        logger.warning('[feeder] Could not start project feeder: %s', exc)


def get_feeder_stats() -> dict:
    """Return project feeder statistics (safe to call even if not started)."""
    global _feeder_instance
    if _feeder_instance is None:
        return {'running': False}
    return {
        'running': _feeder_instance._running,
        'projects': [
            {
                'name': ps.name,
                'path': str(ps.path),
                'exists': ps.exists(),
                'files_tracked': len(ps._file_hashes),
                'api_url': ps.api_url,
            }
            for ps in _feeder_instance._projects
        ],
        'stats': _feeder_instance.stats,
    }


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse
    import signal
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [CVG-FEEDER] %(levelname)s %(message)s',
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    parser = argparse.ArgumentParser(description='CVG Neuron Local Project Feeder')
    parser.add_argument('--list', action='store_true', help='List configured projects and exit')
    parser.add_argument('--once', action='store_true',
                        help='Run one scan cycle for all projects and exit')
    args = parser.parse_args()

    if args.list:
        for p in _DEFAULT_PROJECTS:
            path = Path(p.get('path', ''))
            exists = 'EXISTS    ' if path.exists() else 'NOT FOUND '
            print(f"  {p['name']:20s} {exists} {p.get('path','')}")
        sys.exit(0)

    if args.once:
        feeder = ProjectFeeder()
        for ps in feeder._projects:
            if not ps.exists():
                print(f'  SKIP (not found): {ps.name} -> {ps.path}')
                continue
            print(f'  Scanning: {ps.name} -> {ps.path}')
            changes = ps.scan_files()
            print(f'    Files tracked: {len(ps._file_hashes)}')
            log = _run_git_log(ps.path, n=3)
            if log:
                print(f'    Git log:\n{log}')
            if ps.api_url:
                content = ps.check_api()
                if content:
                    print(f'    API ({ps.api_url}): {len(content)} chars captured')
        sys.exit(0)

    feeder = ProjectFeeder()
    feeder.start(daemon=False)

    def _stop(sig, frame):
        print('\n[feeder] Stopping...')
        feeder.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    print('[CVG Feeder] Running. Press Ctrl+C to stop.')
    print('[CVG Feeder] Stats every 60s. CAPTURE_URL=%s' % CAPTURE_URL)
    while True:
        time.sleep(60)
        print(f'[CVG Feeder] Stats: {feeder.stats}')
