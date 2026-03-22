# CVG Neuron -- Universal Memory Capture Daemon v1
# (c) Clearview Geographic, LLC -- Proprietary and PRIVATE
#
# Lightweight local HTTP server that runs on the host machine (outside Docker)
# and captures AI interactions from ANY terminal/tool, then forwards them
# into Neuron's capture memory tier.
#
# Supported capture sources:
#   - Cline (VS Code extension) — via POST from shell hook or MCP
#   - Claude CLI — via shell wrapper
#   - GitHub Copilot — via shell wrapper
#   - Aider — via shell wrapper
#   - continue.dev — via webhook
#   - Any custom script — via POST /capture
#
# Runs on: http://localhost:8098  (no auth — localhost only)
#
# Usage:
#   python -m neuron.memory_capture          # run standalone
#   python neuron/memory_capture.py          # run directly
#
# The daemon writes to the SAME memory files that Neuron reads, so captures
# are available immediately on the next consolidation cycle (every 15 min).

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
import threading
import hashlib
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

# ── Configuration ─────────────────────────────────────────────────────────────

CAPTURE_DAEMON_PORT  = int(os.getenv('CVG_CAPTURE_PORT', '8098'))
CAPTURE_DAEMON_HOST  = os.getenv('CVG_CAPTURE_HOST', '127.0.0.1')  # localhost only

# Memory directory — must match Neuron's NEURON_DATA_DIR
# When running on the host (outside Docker), point to the mounted volume or local path
DATA_DIR     = Path(os.getenv('NEURON_DATA_DIR',
                   os.getenv('CVG_MEMORY_DIR',
                       # Auto-detect: prefer /app/data (in container), else local
                       '/app/data' if Path('/app/data').exists()
                       else str(Path.home() / 'cvg_neuron_data')
                   )))
MEMORY_DIR   = DATA_DIR / 'memory'
CAPTURE_FILE = MEMORY_DIR / 'captures.json'

MAX_CAPTURE_ITEMS = 3000
CONSOLIDATION_INTERVAL_SECONDS = int(os.getenv('CVG_CONSOLIDATION_INTERVAL', '900'))  # 15 min

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [CVG-CAPTURE] %(levelname)s %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
logger = logging.getLogger('cvg.capture_daemon')

# ── In-memory queue (thread-safe) ────────────────────────────────────────────

_capture_queue: deque = deque(maxlen=500)
_queue_lock = threading.Lock()
_stats: Dict[str, Any] = {
    'started_at': datetime.now(timezone.utc).isoformat(),
    'total_captures': 0,
    'by_source': {},
    'last_capture': None,
    'last_consolidation': None,
    'errors': 0,
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def _content_hash(text: str) -> str:
    return hashlib.md5(text.lower().strip().encode(), usedforsecurity=False).hexdigest()[:12]


# ── File I/O (no filelock dependency in daemon) ───────────────────────────────

def _load_captures() -> list:
    if CAPTURE_FILE.exists():
        try:
            with CAPTURE_FILE.open('r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as exc:
            logger.warning('Failed to load captures: %s', exc)
    return []


def _save_captures(captures: list) -> None:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CAPTURE_FILE.with_suffix('.tmp')
    try:
        with tmp.open('w', encoding='utf-8') as f:
            json.dump(captures[:MAX_CAPTURE_ITEMS], f, indent=2)
        tmp.replace(CAPTURE_FILE)
    except Exception as exc:
        logger.error('Failed to save captures: %s', exc)
        tmp.unlink(missing_ok=True)


def _ingest_capture(source: str, content: str, role: str = 'assistant',
                    model: Optional[str] = None, metadata: Optional[dict] = None,
                    terminal_id: Optional[str] = None,
                    session_id: Optional[str] = None) -> str:
    """Write a capture directly to the captures.json file."""
    cap_id = f'cap_{datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")}'
    cap = {
        'id':          cap_id,
        'timestamp':   _utcnow_iso(),
        'source':      source,
        'terminal_id': terminal_id or source,
        'session_id':  session_id,
        'role':        role,
        'content':     content[:4000],
        'model':       model,
        'metadata':    metadata or {},
        'processed':   False,
    }

    # Load, prepend, save
    captures = _load_captures()
    captures.insert(0, cap)
    _save_captures(captures)

    # Update stats
    with _queue_lock:
        _stats['total_captures'] += 1
        _stats['by_source'][source] = _stats['by_source'].get(source, 0) + 1
        _stats['last_capture'] = _utcnow_iso()

    logger.info('[capture] %s/%s from %-15s | %d chars | id=%s',
                source, role, terminal_id or source, len(content), cap_id)
    return cap_id


# ── Periodic consolidation trigger ───────────────────────────────────────────

def _trigger_neuron_consolidation() -> None:
    """
    Optionally trigger Neuron's consolidation endpoint if it's running.
    Runs in a background thread, silently fails if Neuron is not reachable.
    """
    import urllib.request
    neuron_url = os.getenv('CVG_NEURON_URL', 'http://localhost:8095')
    neuron_key = os.getenv('CVG_INTERNAL_KEY', 'cvg-internal-2026')
    try:
        req = urllib.request.Request(
            f'{neuron_url}/api/memory/consolidate',
            method='POST',
            headers={
                'X-CVG-Key': neuron_key,
                'Content-Type': 'application/json',
            },
            data=b'{}',
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            logger.info('[consolidation] Triggered: %s', result.get('actions', {}))
            with _queue_lock:
                _stats['last_consolidation'] = _utcnow_iso()
    except Exception as exc:
        logger.debug('[consolidation] Neuron not reachable (OK if not running): %s', exc)


def _consolidation_loop() -> None:
    """Background thread: trigger consolidation every N minutes."""
    while True:
        time.sleep(CONSOLIDATION_INTERVAL_SECONDS)
        _trigger_neuron_consolidation()


# ── HTTP Request Handler ──────────────────────────────────────────────────────

class CaptureHandler(BaseHTTPRequestHandler):
    """
    Simple HTTP handler for the capture daemon.

    Endpoints:
      POST /capture        — Ingest a capture from any AI tool
      POST /capture/batch  — Ingest multiple captures at once
      GET  /health         — Health check
      GET  /stats          — Capture statistics
      GET  /recent         — Recent captures
    """

    def log_message(self, format, *args):
        # Suppress default access log (too noisy)
        pass

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, msg: str, status: int = 400) -> None:
        self._send_json({'error': msg}, status)

    def _read_body(self) -> Optional[dict]:
        try:
            length = int(self.headers.get('Content-Length', 0))
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            return json.loads(raw)
        except Exception as exc:
            logger.warning('Failed to read request body: %s', exc)
            return None

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Source, X-Model')
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        qs   = parse_qs(urlparse(self.path).query)

        if path == '/health':
            self._send_json({
                'status': 'ok',
                'service': 'cvg-capture-daemon',
                'version': '1.0.0',
                'port': CAPTURE_DAEMON_PORT,
                'memory_dir': str(MEMORY_DIR),
                'capture_file_exists': CAPTURE_FILE.exists(),
                'timestamp': _utcnow_iso(),
            })

        elif path == '/stats':
            captures = _load_captures()
            with _queue_lock:
                stats = dict(_stats)
            stats['capture_file_size_kb'] = round(
                CAPTURE_FILE.stat().st_size / 1024, 1
            ) if CAPTURE_FILE.exists() else 0
            stats['total_on_disk'] = len(captures)
            stats['unprocessed'] = sum(1 for c in captures if not c.get('processed'))
            self._send_json(stats)

        elif path == '/recent':
            limit = int(qs.get('limit', ['20'])[0])
            source = qs.get('source', [None])[0]
            captures = _load_captures()
            if source:
                captures = [c for c in captures if c.get('source') == source]
            self._send_json({'captures': captures[:min(limit, 100)]})

        elif path == '/':
            self._send_json({
                'service': 'CVG Neuron Universal Capture Daemon',
                'version': '1.0.0',
                'endpoints': {
                    'POST /capture': 'Ingest a single AI capture',
                    'POST /capture/batch': 'Ingest multiple captures',
                    'GET /health': 'Health check',
                    'GET /stats': 'Statistics',
                    'GET /recent': 'Recent captures',
                },
                'capture_schema': {
                    'source': 'string (required) — e.g. cline, claude, copilot, aider',
                    'content': 'string (required) — the AI interaction content',
                    'role': 'string (optional, default: assistant) — user|assistant|system',
                    'model': 'string (optional) — model name',
                    'terminal_id': 'string (optional) — unique terminal session identifier',
                    'session_id': 'string (optional) — conversation session ID',
                    'metadata': 'object (optional) — any extra context',
                },
            })
        else:
            self._send_error(f'Unknown endpoint: {path}', 404)

    def do_POST(self):
        path = urlparse(self.path).path

        if path in ('/capture', '/api/memory/capture'):
            body = self._read_body()
            if body is None:
                return self._send_error('Invalid JSON body')

            source  = body.get('source', '').strip()
            content = body.get('content', '').strip()
            if not source:
                return self._send_error('source is required')
            if not content:
                return self._send_error('content is required')

            cap_id = _ingest_capture(
                source=source,
                content=content,
                role=body.get('role', 'assistant'),
                model=body.get('model'),
                metadata=body.get('metadata'),
                terminal_id=body.get('terminal_id'),
                session_id=body.get('session_id'),
            )
            self._send_json({'status': 'captured', 'id': cap_id, 'source': source})

        elif path in ('/capture/batch', '/api/memory/capture/batch'):
            body = self._read_body()
            if body is None:
                return self._send_error('Invalid JSON body')

            captures = body.get('captures', [])
            if not isinstance(captures, list):
                return self._send_error('captures must be a list')

            ids = []
            for cap in captures[:50]:  # max 50 per batch
                source  = cap.get('source', '').strip()
                content = cap.get('content', '').strip()
                if not source or not content:
                    continue
                cap_id = _ingest_capture(
                    source=source,
                    content=content,
                    role=cap.get('role', 'assistant'),
                    model=cap.get('model'),
                    metadata=cap.get('metadata'),
                    terminal_id=cap.get('terminal_id'),
                    session_id=cap.get('session_id'),
                )
                ids.append(cap_id)

            self._send_json({'status': 'captured', 'count': len(ids), 'ids': ids})

        else:
            self._send_error(f'Unknown endpoint: {path}', 404)


# ── Server ─────────────────────────────────────────────────────────────────────

class CaptureServer:
    def __init__(self, host: str = CAPTURE_DAEMON_HOST, port: int = CAPTURE_DAEMON_PORT):
        self.host = host
        self.port = port
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self, daemon: bool = True) -> None:
        """Start the capture server in a background thread."""
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        self._server = HTTPServer((self.host, self.port), CaptureHandler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name='cvg-capture-daemon',
            daemon=daemon,
        )
        self._thread.start()
        logger.info('CVG Capture Daemon started on http://%s:%d', self.host, self.port)
        logger.info('Memory directory: %s', MEMORY_DIR)

        # Start consolidation loop
        consolidation_thread = threading.Thread(
            target=_consolidation_loop,
            name='cvg-consolidation',
            daemon=True,
        )
        consolidation_thread.start()
        logger.info('Consolidation loop started (interval: %ds)', CONSOLIDATION_INTERVAL_SECONDS)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            logger.info('CVG Capture Daemon stopped')

    def run_forever(self) -> None:
        """Run in the foreground (blocking). Handles Ctrl+C gracefully."""
        self.start(daemon=False)

        def _shutdown(sig, frame):
            logger.info('Shutting down...')
            self.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        logger.info('CVG Capture Daemon running. Press Ctrl+C to stop.')
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()


# ── Embedded integration for Neuron web_api ───────────────────────────────────

_capture_server: Optional[CaptureServer] = None


def get_capture_server() -> CaptureServer:
    """Get/create the capture server singleton (for embedding in Neuron)."""
    global _capture_server
    if _capture_server is None:
        _capture_server = CaptureServer()
    return _capture_server


def start_embedded_capture_server() -> None:
    """Start the capture daemon embedded in the Neuron process."""
    srv = get_capture_server()
    try:
        srv.start(daemon=True)
        logger.info('[capture] Embedded capture daemon started on port %d', CAPTURE_DAEMON_PORT)
    except OSError as exc:
        if 'Address already in use' in str(exc) or 'Only one usage' in str(exc):
            logger.info('[capture] Capture daemon already running on port %d', CAPTURE_DAEMON_PORT)
        else:
            logger.warning('[capture] Could not start embedded capture daemon: %s', exc)


def start_all_capture_services() -> None:
    """
    Start all capture services in one call:
      1. Capture HTTP daemon (port 8098) — receives from any terminal
      2. Project feeder — autonomously watches CVG support engine directories
    Used when running the capture daemon standalone (outside Neuron process).
    """
    start_embedded_capture_server()
    try:
        import sys
        import os
        # Try to import project_feeder from the neuron package
        _pkg_dir = os.path.dirname(os.path.abspath(__file__))
        if _pkg_dir not in sys.path:
            sys.path.insert(0, os.path.dirname(_pkg_dir))
        from neuron.project_feeder import start_project_feeder
        start_project_feeder()
        logger.info('[capture] Project feeder started alongside capture daemon')
    except Exception as exc:
        logger.debug('[capture] Project feeder not started (run from Neuron): %s', exc)


# ── CLI entry point ────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='CVG Neuron Universal Memory Capture Daemon')
    parser.add_argument('--host', default=CAPTURE_DAEMON_HOST, help='Bind host')
    parser.add_argument('--port', type=int, default=CAPTURE_DAEMON_PORT, help='Bind port')
    parser.add_argument('--data-dir', default=str(DATA_DIR), help='Memory data directory')
    parser.add_argument('--once', action='store_true',
                        help='Send a single test capture and exit')
    args = parser.parse_args()

    if args.data_dir:
        import os as _os
        _os.environ['NEURON_DATA_DIR'] = args.data_dir
        DATA_DIR = Path(args.data_dir)
        MEMORY_DIR = DATA_DIR / 'memory'
        CAPTURE_FILE = MEMORY_DIR / 'captures.json'

    if args.once:
        # Test mode: send a test capture
        cap_id = _ingest_capture(
            source='capture_daemon_test',
            content='CVG Capture Daemon test capture — memory system is operational.',
            role='system',
            metadata={'test': True},
        )
        print(f'Test capture written: {cap_id}')
        print(f'Capture file: {CAPTURE_FILE}')
        sys.exit(0)

    server = CaptureServer(host=args.host, port=args.port)
    server.run_forever()
