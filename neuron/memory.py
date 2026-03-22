# CVG Neuron -- Persistent Memory System v2
from __future__ import annotations
import json,logging,os,shutil
from collections import deque
from datetime import datetime,timezone
from pathlib import Path
from typing import Any,Dict,List,Optional

logger=logging.getLogger('cvg.neuron.memory')

DATA_DIR        = Path(os.getenv('NEURON_DATA_DIR', '/app/data'))
MEMORY_DIR      = DATA_DIR / 'memory'
EPISODIC_FILE   = MEMORY_DIR / 'episodic.json'
SEMANTIC_FILE   = MEMORY_DIR / 'semantic.json'
PROCEDURAL_FILE = MEMORY_DIR / 'procedural.json'
EXPORT_DIR      = DATA_DIR / 'memory_exports'
MAX_WORKING_ITEMS     = 100
MAX_EPISODIC_ITEMS    = 1000
MAX_SEMANTIC_ITEMS    = 5000
WORKING_SESSION_LIMIT = 20   # auto-flush after this many user turns

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'

def _dt_serializer(obj: Any) -> str:
    if isinstance(obj, datetime):
        if obj.tzinfo is None:
            obj = obj.replace(tzinfo=timezone.utc)
        return obj.isoformat().replace('+00:00', 'Z')
    raise TypeError(f'Not JSON serializable: {type(obj).__name__}')

def _safe_json_dump(obj: Any, fp, **kw) -> None:
    json.dump(obj, fp, default=_dt_serializer, **kw)

try:
    from filelock import FileLock  # type: ignore
    def _lock(path: Path) -> Any:
        return FileLock(str(path) + '.lock', timeout=10)
except ImportError:
    import contextlib
    @contextlib.contextmanager
    def _lock(path: Path):
        yield  # no-op fallback when filelock not installed

def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    with _lock(path):
        try:
            with tmp.open('w', encoding='utf-8') as fh:
                _safe_json_dump(data, fh, indent=2)
            tmp.replace(path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise


class WorkingMemory:
    '''Short-term session memory. Auto-flushes to episodic after session_limit user turns.'''

    def __init__(self, max_size: int = MAX_WORKING_ITEMS,
                 session_limit: int = WORKING_SESSION_LIMIT):
        self._store: deque = deque(maxlen=max_size)
        self._session_start = _utcnow_iso()
        self._session_limit = session_limit
        self._turn_count: int = 0
        self._flush_callback: Optional[Any] = None

    def add(self, item: dict) -> None:
        if 'timestamp' not in item:
            item['timestamp'] = _utcnow_iso()
        self._store.append(item)
        if item.get('role') == 'user':
            self._turn_count += 1
            if self._turn_count >= self._session_limit and self._flush_callback:
                self._flush_callback()

    def recent(self, n: int = 10) -> list:
        return list(self._store)[-n:]

    def clear(self) -> None:
        self._store.clear()
        self._turn_count = 0

    def store(self, key: str, value: Any, category: str = 'general') -> None:
        self.add({'key': key, 'value': value, 'category': category})

    def recall(self, key: str) -> Optional[Any]:
        for item in reversed(self._store):
            if item.get('key') == key:
                return item.get('value')
        return None

    def recall_recent(self, n: int = 10, category: Optional[str] = None) -> list:
        items = list(self._store)
        if category:
            items = [i for i in items if i.get('category') == category]
        return items[-n:]

    def summary(self) -> str:
        items = self.recall_recent(20)
        if not items:
            return 'Working memory is empty.'
        lines = [f'Session since {self._session_start[:16]}Z -- {len(self._store)} items, {self._turn_count} turns:']
        for item in items[:8]:
            ts = item.get('timestamp', '')[:16]
            cat = item.get('category', item.get('role', '?'))
            key = item.get('key', item.get('content', ''))
            lines.append(f'  [{ts}] [{cat}] {str(key)[:120]}')
        return '\n'.join(lines)

    @property
    def turn_count(self) -> int:
        return self._turn_count

    @property
    def item_count(self) -> int:
        return len(self._store)

    def byte_size(self) -> int:
        try:
            return len(json.dumps(list(self._store), default=_dt_serializer).encode())
        except Exception:
            return 0


class EpisodicMemory:
    '''Long-term event memory. Persisted to JSON ring buffer. Survives restarts.'''

    def __init__(self):
        self._episodes: list = []
        self._load()

    def _load(self) -> None:
        if EPISODIC_FILE.exists():
            try:
                with _lock(EPISODIC_FILE):
                    with EPISODIC_FILE.open('r', encoding='utf-8') as f:
                        self._episodes = json.load(f)
                logger.info('[memory/episodic] Loaded %d episodes', len(self._episodes))
            except Exception as exc:
                logger.warning('[memory/episodic] Load failed: %s', exc)
                self._episodes = []

    def _save(self) -> None:
        try:
            if len(self._episodes) > MAX_EPISODIC_ITEMS:
                self._episodes = self._episodes[:MAX_EPISODIC_ITEMS]
            _atomic_write(EPISODIC_FILE, self._episodes)
        except Exception as exc:
            logger.warning('[memory/episodic] Save failed: %s', exc)

    def record(self, event_type: str, summary: str, detail: Any = None,
               source: str = 'neuron', severity: str = 'info',
               metadata: Optional[dict] = None) -> None:
        ep = {
            'id': f'ep_{datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")}',
            'timestamp': _utcnow_iso(),
            'event_type': event_type,
            'summary': summary,
            'detail': detail if detail is not None else metadata,
            'source': source,
            'severity': severity,
        }
        self._episodes.insert(0, ep)
        self._save()
        logger.debug('[memory/episodic] Recorded: %s -- %s', event_type, summary)

    def recent(self, n: int = 20) -> list:
        return self._episodes[:n]

    def recall_recent(self, n: int = 20, event_type: Optional[str] = None) -> list:
        items = self._episodes
        if event_type:
            items = [e for e in items if e.get('event_type') == event_type]
        return items[:n]

    def recall_since(self, hours: int = 24) -> list:
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        results = []
        for e in self._episodes:
            ts_str = e.get('timestamp', '').replace('Z', '+00:00')
            try:
                if datetime.fromisoformat(ts_str) > cutoff:
                    results.append(e)
            except ValueError:
                pass
        return results

    def search(self, query: str, limit: int = 20) -> list:
        q = query.lower()
        results = []
        for ep in self._episodes:
            if (q in ep.get('summary', '').lower()
                    or q in ep.get('event_type', '').lower()
                    or q in str(ep.get('detail', '')).lower()):
                results.append(ep)
            if len(results) >= limit:
                break
        return results

    def summary(self, n: int = 10) -> str:
        recent = self.recall_recent(n)
        if not recent:
            return 'No episodic memory yet.'
        lines = [f'Recent episodes ({len(self._episodes)} total):']
        for ep in recent:
            ts = ep['timestamp'][:16]
            sev = ep.get('severity', 'info').upper()
            lines.append(f'  [{ts}] [{sev}] {ep["event_type"]}: {ep["summary"][:120]}')
        return '\n'.join(lines)

    def byte_size(self) -> int:
        try:
            return EPISODIC_FILE.stat().st_size if EPISODIC_FILE.exists() else 0
        except Exception:
            return 0

    @property
    def total(self) -> int:
        return len(self._episodes)


class SemanticMemory:
    '''Semantic memory -- facts and knowledge. Persisted to JSON. Grows as Neuron learns.'''

    def __init__(self):
        self._facts: dict = {}
        self._load()

    def _load(self) -> None:
        if SEMANTIC_FILE.exists():
            try:
                with _lock(SEMANTIC_FILE):
                    with SEMANTIC_FILE.open('r', encoding='utf-8') as f:
                        self._facts = json.load(f)
                logger.info('[memory/semantic] Loaded %d facts', len(self._facts))
            except Exception as exc:
                logger.warning('[memory/semantic] Load failed: %s', exc)
                self._facts = {}

    def _save(self) -> None:
        try:
            if len(self._facts) > MAX_SEMANTIC_ITEMS:
                sorted_keys = sorted(self._facts, key=lambda k: self._facts[k].get('confidence', 0))
                for old_key in sorted_keys[:len(self._facts) - MAX_SEMANTIC_ITEMS]:
                    del self._facts[old_key]
            _atomic_write(SEMANTIC_FILE, self._facts)
        except Exception as exc:
            logger.warning('[memory/semantic] Save failed: %s', exc)

    def add_fact(self, key: str, value: Any, confidence: float = 0.9,
                 source: str = 'observation') -> None:
        self.learn(key, value, confidence, source)

    def learn(self, key: str, value: Any, confidence: float = 0.9,
              source: str = 'observation') -> None:
        self._facts[key] = {
            'value': value, 'confidence': confidence, 'source': source,
            'updated': _utcnow_iso(),
            'access_count': self._facts.get(key, {}).get('access_count', 0),
        }
        self._save()

    def recall(self, key: str) -> Optional[dict]:
        fact = self._facts.get(key)
        if fact:
            fact['access_count'] = fact.get('access_count', 0) + 1
        return fact

    def search(self, query: str, max_results: int = 10,
               limit: Optional[int] = None) -> list:
        cap = limit if limit is not None else max_results
        q = query.lower()
        results = []
        for key, fact in self._facts.items():
            if q in key.lower() or q in str(fact.get('value', '')).lower():
                results.append({'key': key, **fact})
        results.sort(key=lambda x: x.get('confidence', 0), reverse=True)
        return results[:cap]

    def summary(self, n: int = 15) -> str:
        if not self._facts:
            return 'Semantic memory empty -- Neuron is still learning.'
        lines = [f'Semantic memory ({len(self._facts)} facts known):']
        sorted_facts = sorted(self._facts.items(),
                              key=lambda kv: kv[1].get('confidence', 0), reverse=True)
        for key, fact in sorted_facts[:n]:
            conf = fact.get('confidence', 0)
            val = str(fact.get('value', ''))[:100]
            lines.append(f'  [{conf:.0%}] {key}: {val}')
        return '\n'.join(lines)

    def all_facts(self) -> list:
        """Return all semantic facts as a list of dicts (key + metadata)."""
        return [{'key': k, **v} for k, v in self._facts.items()]

    def byte_size(self) -> int:
        try:
            return SEMANTIC_FILE.stat().st_size if SEMANTIC_FILE.exists() else 0
        except Exception:
            return 0

    @property
    def total(self) -> int:
        return len(self._facts)


class ProceduralMemory:
    '''Procedural memory -- patterns and behaviors. Persisted to JSON.'''

    def __init__(self):
        self._procedures: dict = {}
        self._load()

    def _load(self) -> None:
        if PROCEDURAL_FILE.exists():
            try:
                with _lock(PROCEDURAL_FILE):
                    with PROCEDURAL_FILE.open('r', encoding='utf-8') as f:
                        self._procedures = json.load(f)
                logger.info('[memory/procedural] Loaded %d procedures', len(self._procedures))
            except Exception as exc:
                logger.warning('[memory/procedural] Load failed: %s', exc)
                self._procedures = {}

    def _save(self) -> None:
        try:
            _atomic_write(PROCEDURAL_FILE, self._procedures)
        except Exception as exc:
            logger.warning('[memory/procedural] Save failed: %s', exc)

    def learn_procedure(self, name: str, trigger: str, steps: list,
                        outcome: str = '', success_rate: float = 1.0) -> None:
        self._procedures[name] = {
            'trigger': trigger, 'steps': steps, 'outcome': outcome,
            'success_rate': success_rate,
            'use_count': self._procedures.get(name, {}).get('use_count', 0),
            'updated': _utcnow_iso(),
        }
        self._save()

    def get_procedure(self, name: str) -> Optional[dict]:
        proc = self._procedures.get(name)
        if proc:
            proc['use_count'] = proc.get('use_count', 0) + 1
            self._save()
        return proc

    def match(self, context: str, limit: int = 10) -> list:
        return self.match_trigger(context)[:limit]

    def match_trigger(self, context: str) -> list:
        context_lower = context.lower()
        matches = []
        for name, proc in self._procedures.items():
            trigger_words = proc.get('trigger', '').lower().split()
            if any(word in context_lower for word in trigger_words):
                matches.append({'name': name, **proc})
        return matches

    def search(self, query: str, limit: int = 10) -> list:
        q = query.lower()
        results = []
        for name, proc in self._procedures.items():
            if (q in name.lower() or q in proc.get('trigger', '').lower()
                    or q in str(proc.get('steps', '')).lower()):
                results.append({'name': name, **proc})
            if len(results) >= limit:
                break
        return results

    def all_patterns(self) -> list:
        return [{'name': k, **v} for k, v in self._procedures.items()]

    def all_procedures(self) -> list:
        return self.all_patterns()

    def byte_size(self) -> int:
        try:
            return PROCEDURAL_FILE.stat().st_size if PROCEDURAL_FILE.exists() else 0
        except Exception:
            return 0

    @property
    def total(self) -> int:
        return len(self._procedures)


class NeuronMemory:
    '''Unified four-tier memory system for CVG Neuron.'''

    def __init__(self):
        self.working    = WorkingMemory(session_limit=WORKING_SESSION_LIMIT)
        self.episodic   = EpisodicMemory()
        self.semantic   = SemanticMemory()
        self.procedural = ProceduralMemory()
        # Wire auto-flush: when working memory hits session_limit user turns,
        # flush to episodic and reset.
        self.working._flush_callback = self._flush_working_to_episodic
        if self.semantic.total == 0:
            self._seed_initial_knowledge()
        logger.info(
            '[memory] NeuronMemory initialized -- episodic:%d semantic:%d procedural:%d',
            self.episodic.total, self.semantic.total, self.procedural.total,
        )

    def _flush_working_to_episodic(self) -> None:
        items = self.working.recent(WORKING_SESSION_LIMIT)
        if not items:
            return
        summary_lines = []
        for item in items:
            role = item.get('role', item.get('category', '?'))
            content = str(item.get('content', item.get('value', item.get('key', ''))))
            summary_lines.append(f'[{role}] {content[:150]}')
        self.episodic.record(
            event_type='session_flush',
            summary=f'Auto-flushed {len(items)} working items after {WORKING_SESSION_LIMIT} turns',
            detail={'items': summary_lines},
            source='memory_manager',
        )
        self.working.clear()
        logger.info('[memory] Working memory auto-flushed to episodic (%d items)', len(items))

    def stats(self) -> dict:
        eb = self.episodic.byte_size()
        sb = self.semantic.byte_size()
        pb = self.procedural.byte_size()
        wb = self.working.byte_size()
        total = eb + sb + pb + wb
        return {
            'working_items':        self.working.item_count,
            'working_turns':        self.working.turn_count,
            'episodic_episodes':    self.episodic.total,
            'semantic_facts':       self.semantic.total,
            'procedural_patterns':  self.procedural.total,
            'working_bytes':        wb,
            'episodic_bytes':       eb,
            'semantic_bytes':       sb,
            'procedural_bytes':     pb,
            'total_bytes':          total,
            'total_kb':             round(total / 1024, 1),
            'working_session_limit': WORKING_SESSION_LIMIT,
            'episodic_max':         MAX_EPISODIC_ITEMS,
            'semantic_max':         MAX_SEMANTIC_ITEMS,
        }

    def get_stats(self) -> dict:
        return self.stats()

    def search(self, query: str, limit: int = 20) -> dict:
        per = max(1, limit // 4)
        q = query.lower()
        return {
            'working':    [i for i in self.working.recent(limit) if q in str(i).lower()][:per],
            'episodic':   self.episodic.search(query, limit=per),
            'semantic':   self.semantic.search(query, limit=per),
            'procedural': self.procedural.search(query, limit=per),
            'query':      query,
        }

    def export(self, label: str = '') -> Path:
        ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        tag = f'_{label}' if label else ''
        export_path = EXPORT_DIR / f'neuron_memory_{ts}{tag}'
        export_path.mkdir(parents=True, exist_ok=True)
        self.persist()
        manifest: Dict[str, Any] = {
            'exported_at': _utcnow_iso(), 'label': label,
            'stats': self.stats(), 'files': [],
        }
        for src in (EPISODIC_FILE, SEMANTIC_FILE, PROCEDURAL_FILE):
            if src.exists():
                shutil.copy2(src, export_path / src.name)
                manifest['files'].append(src.name)
        _atomic_write(export_path / 'manifest.json', manifest)
        logger.info('[memory] Exported to %s', export_path)
        return export_path

    def import_backup(self, export_dir: Path, overwrite: bool = False) -> dict:
        export_dir = Path(export_dir)
        if not export_dir.is_dir():
            raise ValueError(f'Export directory not found: {export_dir}')
        results: Dict[str, str] = {}
        file_map = {
            'episodic.json':   EPISODIC_FILE,
            'semantic.json':   SEMANTIC_FILE,
            'procedural.json': PROCEDURAL_FILE,
        }
        for fname, dst in file_map.items():
            src = export_dir / fname
            if src.exists():
                if dst.exists() and not overwrite:
                    results[fname] = 'skipped (overwrite=False)'
                else:
                    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    results[fname] = 'imported'
            else:
                results[fname] = 'not found in export'
        self.episodic   = EpisodicMemory()
        self.semantic   = SemanticMemory()
        self.procedural = ProceduralMemory()
        logger.info('[memory] Import complete: %s', results)
        return results

    def build_context_summary(self) -> str:
        parts = []
        es = self.episodic.summary(8)
        if es:
            parts.append(es)
        ws = self.working.summary()
        if ws:
            parts.append(ws)
        return '\n\n'.join(parts)

    def persist(self) -> None:
        self.episodic._save()
        self.semantic._save()
        self.procedural._save()
        logger.debug('[memory] All tiers persisted to disk')

    def _seed_initial_knowledge(self) -> None:
        initial_facts = {
            'cvg.cluster.name':         ('Hive-0', 1.0, 'built-in'),
            'cvg.cluster.domain':       ('hive0.cleargeo.tech', 1.0, 'built-in'),
            'cvg.cluster.local':        ('hive0.cleargeo.tech.local', 1.0, 'built-in'),
            'cvg.cluster.location':     ('New Smyrna Beach, FL', 1.0, 'built-in'),
            'cvg.network.authorized':   ('10.0.0.0/8 ONLY -- see CVG_NETWORK_STANDARD.md', 1.0, 'built-in'),
            'cvg.network.lan10':        ('10.10.10.0/24 -- Queen/Infra VLAN', 1.0, 'built-in'),
            'cvg.network.lan20':        ('10.10.20.0/24 -- Workstation/Admin VLAN', 1.0, 'built-in'),
            'cvg.network.gateway10':    ('10.10.10.1 (FortiGate)', 1.0, 'built-in'),
            'cvg.network.gateway20':    ('10.10.20.1 (FortiGate)', 1.0, 'built-in'),
            'cvg.network.legacy.deprecated': ('192.168.100.0/24 -- DEPRECATED DO NOT USE', 1.0, 'built-in'),
            'cvg.network.docker':       ('cvg-platform_cvg_net', 1.0, 'built-in'),
            'cvg.primary.host':         ('cvg-stormsurge-01', 1.0, 'built-in'),
            'cvg.primary.ip':           ('10.10.10.200', 1.0, 'built-in'),
            'cvg.vm.451.ip':            ('10.10.10.200', 1.0, 'built-in'),
            'cvg.vm.454.ip':            ('10.10.10.204', 1.0, 'built-in'),
            'cvg.vm.455.ip':            ('10.10.10.205', 1.0, 'built-in'),
            'cvg.ct.104.ip':            ('10.10.10.104', 1.0, 'built-in'),
            'cvg.queen11.proxmox.ip':   ('10.10.10.56', 1.0, 'built-in'),
            'cvg.queen11.idrac.ip':     ('10.10.10.50', 1.0, 'built-in'),
            'cvg.queen11.hardware':     ('Dell PowerEdge R820, 4xE5-4650, ~512 GB RAM', 1.0, 'built-in'),
            'cvg.queen12.ip':           ('10.10.10.53', 1.0, 'built-in'),
            'cvg.queen12.hardware':     ('Synology DS1823+, 8-bay NAS', 1.0, 'built-in'),
            'cvg.queen20.ip':           ('10.10.10.67', 1.0, 'built-in'),
            'cvg.queen20.hardware':     ('Synology DS3622xs+, 12-bay, 10GbE NAS', 1.0, 'built-in'),
            'cvg.queen21.ip':           ('10.10.10.57', 1.0, 'built-in'),
            'cvg.queen30.ip':           ('10.10.10.71', 1.0, 'built-in'),
            'cvg.queen10.esxi.ip':      ('10.10.10.61', 1.0, 'built-in'),
            'cvg.queen10.ilo.ip':       ('10.10.10.58', 1.0, 'built-in'),
            'cvg.queen10.truenas.ip':   ('10.10.10.100', 1.0, 'built-in'),
            'cvg.queen10.hardware':     ('HP ProLiant ML350 Gen10, 2xGold 5118, 192 GB RAM', 1.0, 'built-in'),
            'cvg.audit.vm.ip':          ('10.10.10.220', 1.0, 'built-in'),
            'cvg.audit.vm.port':        (8001, 1.0, 'built-in'),
            'cvg.security.siem':        ('Wazuh 4.9.2 -- on Audit VM', 1.0, 'built-in'),
            'cvg.security.scanner':     ('Trivy -- on Audit VM', 1.0, 'built-in'),
            'cvg.domain.external':      ('cleargeo.tech', 1.0, 'built-in'),
            'cvg.domain.git':           ('git.cleargeo.tech (Gitea)', 1.0, 'built-in'),
            'cvg.domain.neuron':        ('neuron.cleargeo.tech', 1.0, 'built-in'),
            'cvg.proxy':                ('Caddy -- reverse proxy', 1.0, 'built-in'),
            'cvg.dns.internal':         ('BIND9 on FortiGate / LAN DNS', 1.0, 'built-in'),
            'cvg.dns.external':         ('cPanel/WHM -- cleargeo.tech zones', 1.0, 'built-in'),
            'cvg.monitoring':           ('Prometheus + Grafana + Loki', 1.0, 'built-in'),
            'cvg.git.host':             ('Gitea (git.cleargeo.tech) + GitHub', 1.0, 'built-in'),
            'cvg.engine.git.port':      (8092, 1.0, 'built-in'),
            'cvg.engine.dns.port':      (8094, 1.0, 'built-in'),
            'cvg.engine.container.port': (8091, 1.0, 'built-in'),
            'cvg.engine.audit.port':    (8001, 1.0, 'built-in'),
            'cvg.neuron.port':          (8095, 1.0, 'built-in'),
            'cvg.neuron.container':     ('cvg-neuron-v1', 1.0, 'built-in'),
            'cvg.neuron.identity':      ('Private AI -- not public, not on Ollama registry', 1.0, 'built-in'),
            'cvg.internal.key':         ('cvg-internal-2026', 1.0, 'built-in'),
            'cvg.ollama.host':          ('10.10.10.200:11434 (vm-451 / cvg-stormsurge-01)', 1.0, 'built-in'),
            'cvg.company':              ('Clearview Geographic, LLC', 1.0, 'built-in'),
            'cvg.principal':            ('Alex Zelenski, GISP (President and CEO)', 1.0, 'built-in'),
            'cvg.staff.support':        ('Jennifer Mounivong (client support)', 1.0, 'built-in'),
            'cvg.staff.science':        ('Dr. Jason Evans PhD (Chief Science Officer)', 1.0, 'built-in'),
            'cvg.location':             ('DeLand, FL 32720 (HQ) / New Smyrna Beach (Cluster)', 1.0, 'built-in'),
        }
        for key, (value, confidence, source) in initial_facts.items():
            self.semantic.learn(key, value, confidence, source)
        logger.info('[memory] Seeded %d initial CVG knowledge facts', len(initial_facts))


_memory: Optional[NeuronMemory] = None


def get_memory() -> NeuronMemory:
    global _memory
    if _memory is None:
        _memory = NeuronMemory()
    return _memory
