# CVG Neuron -- Persistent Memory System v3
# (c) Clearview Geographic, LLC -- Proprietary and PRIVATE
#
# v3 improvements:
#   - Increased all tier limits significantly
#   - Added ImportanceScore and access-weighted retention
#   - Added AssociativeMemory tier for cross-session links
#   - Memory consolidation: episodic→semantic promotion when patterns repeat
#   - Semantic deduplication and merge
#   - Cross-terminal session tracking (source tagging)
#   - Universal capture ingestion from any AI terminal on the machine
#   - Richer summary for LLM context injection
#   - Consolidated snapshot for warm-start context

from __future__ import annotations
import json, logging, os, shutil, hashlib
from collections import deque, Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger('cvg.neuron.memory')

DATA_DIR            = Path(os.getenv('NEURON_DATA_DIR', '/app/data'))
MEMORY_DIR          = DATA_DIR / 'memory'
EPISODIC_FILE       = MEMORY_DIR / 'episodic.json'
SEMANTIC_FILE       = MEMORY_DIR / 'semantic.json'
PROCEDURAL_FILE     = MEMORY_DIR / 'procedural.json'
ASSOCIATIVE_FILE    = MEMORY_DIR / 'associative.json'
CAPTURE_FILE        = MEMORY_DIR / 'captures.json'
EXPORT_DIR          = DATA_DIR / 'memory_exports'

# Tier limits (increased from v2)
MAX_WORKING_ITEMS     = 200
MAX_EPISODIC_ITEMS    = 5000
MAX_SEMANTIC_ITEMS    = 10000
MAX_ASSOCIATIVE_ITEMS = 2000
MAX_CAPTURE_ITEMS     = 3000
WORKING_SESSION_LIMIT = 40    # auto-flush after this many user turns (was 20)

# Consolidation thresholds
CONSOLIDATION_REPEAT_THRESHOLD = 3   # episodes with same key pattern → promote to semantic
CONSOLIDATION_MIN_CONFIDENCE   = 0.6  # minimum confidence for promoted facts
IMPORTANCE_DECAY_DAYS          = 90   # items older than this with zero access decay faster


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


def _content_hash(text: str) -> str:
    """Generate a short hash for deduplication."""
    return hashlib.md5(text.lower().strip().encode(), usedforsecurity=False).hexdigest()[:12]


try:
    from filelock import FileLock  # type: ignore
    def _lock(path: Path) -> Any:
        return FileLock(str(path) + '.lock', timeout=10)
except ImportError:
    import contextlib
    @contextlib.contextmanager
    def _lock(path: Path):
        yield  # no-op fallback


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


def _importance_score(item: dict) -> float:
    """
    Compute importance score for retention prioritization.
    Considers: access_count, confidence, recency, source trust.
    """
    access  = min(1.0, item.get('access_count', 0) / 20.0) * 0.3
    conf    = item.get('confidence', 0.5) * 0.4
    # Recency bonus (0.0–0.3): items updated within 7 days get max bonus
    updated = item.get('updated', item.get('timestamp', ''))
    recency = 0.0
    if updated:
        try:
            ts = datetime.fromisoformat(updated.replace('Z', '+00:00'))
            age_days = (datetime.now(timezone.utc) - ts).days
            recency = max(0.0, 0.3 * (1.0 - min(age_days / 30.0, 1.0)))
        except Exception:
            pass
    # Source trust bonus
    source_trust = {
        'built-in': 0.1, 'api_direct': 0.08, 'edge_registration': 0.08,
        'startup_scan': 0.06, 'manual_scan': 0.06, 'observation': 0.04,
        'neuron_inference': 0.02, 'auto_learned': 0.01, 'external_terminal': 0.05,
        'cline': 0.07, 'claude': 0.07, 'copilot': 0.06,
    }
    src = item.get('source', '')
    trust = source_trust.get(src, 0.03)
    return round(access + conf + recency + trust, 4)


# =============================================================================
# WORKING MEMORY
# =============================================================================

class WorkingMemory:
    '''
    Short-term session memory.
    Auto-flushes to episodic after session_limit user turns.
    Tracks source terminal for cross-session awareness.
    '''

    def __init__(self, max_size: int = MAX_WORKING_ITEMS,
                 session_limit: int = WORKING_SESSION_LIMIT):
        self._store: deque = deque(maxlen=max_size)
        self._session_start = _utcnow_iso()
        self._session_limit = session_limit
        self._turn_count: int = 0
        self._flush_callback: Optional[Any] = None
        self._source: str = 'neuron'  # default source terminal

    def add(self, item: dict) -> None:
        if 'timestamp' not in item:
            item['timestamp'] = _utcnow_iso()
        if 'source' not in item and self._source:
            item['source'] = self._source
        self._store.append(item)
        if item.get('role') == 'user':
            self._turn_count += 1
            if self._turn_count >= self._session_limit and self._flush_callback:
                self._flush_callback()

    def set_source(self, source: str) -> None:
        """Tag this session's working memory with a source terminal identifier."""
        self._source = source

    def recent(self, n: int = 10) -> list:
        return list(self._store)[-n:]

    def clear(self) -> None:
        self._store.clear()
        self._turn_count = 0
        self._session_start = _utcnow_iso()

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
        items = self.recall_recent(30)
        if not items:
            return 'Working memory is empty.'
        lines = [f'Session since {self._session_start[:16]}Z -- {len(self._store)} items, {self._turn_count} turns:']
        for item in items[-10:]:
            ts  = item.get('timestamp', '')[:16]
            cat = item.get('category', item.get('role', '?'))
            key = item.get('key', item.get('content', ''))
            src = item.get('source', '')
            src_tag = f'[{src}] ' if src and src != 'neuron' else ''
            lines.append(f'  [{ts}] [{cat}] {src_tag}{str(key)[:140]}')
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


# =============================================================================
# EPISODIC MEMORY
# =============================================================================

class EpisodicMemory:
    '''
    Long-term event memory. Persisted to JSON ring buffer.
    Survives restarts. Supports source-tagged cross-terminal episodes.
    '''

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
                # Keep highest-importance items when trimming
                scored = []
                for ep in self._episodes:
                    detail = ep.get('detail') or {}
                    if isinstance(detail, dict):
                        score = detail.get('confidence_score', 0.5)
                    else:
                        score = 0.3
                    scored.append((score, ep))
                scored.sort(key=lambda x: x[0], reverse=True)
                self._episodes = [ep for _, ep in scored[:MAX_EPISODIC_ITEMS]]
            _atomic_write(EPISODIC_FILE, self._episodes)
        except Exception as exc:
            logger.warning('[memory/episodic] Save failed: %s', exc)

    def record(self, event_type: str, summary: str, detail: Any = None,
               source: str = 'neuron', severity: str = 'info',
               metadata: Optional[dict] = None) -> str:
        ep_id = f'ep_{datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")}'
        ep = {
            'id': ep_id,
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
        return ep_id

    def recent(self, n: int = 20) -> list:
        return self._episodes[:n]

    def recall_recent(self, n: int = 20, event_type: Optional[str] = None,
                      source: Optional[str] = None) -> list:
        items = self._episodes
        if event_type:
            items = [e for e in items if e.get('event_type') == event_type]
        if source:
            items = [e for e in items if e.get('source') == source]
        return items[:n]

    def recall_since(self, hours: int = 24) -> list:
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
                    or q in str(ep.get('detail', '')).lower()
                    or q in ep.get('source', '').lower()):
                results.append(ep)
            if len(results) >= limit:
                break
        return results

    def get_frequent_patterns(self, min_count: int = CONSOLIDATION_REPEAT_THRESHOLD,
                               hours: int = 168) -> List[Tuple[str, int]]:
        """
        Identify event types / summary patterns that repeat frequently.
        Used by consolidation to promote episodic → semantic.
        """
        recent = self.recall_since(hours=hours)
        event_types: Counter = Counter()
        for ep in recent:
            # Extract key words from summary for pattern matching
            et = ep.get('event_type', '')
            event_types[et] += 1
        return [(et, count) for et, count in event_types.most_common()
                if count >= min_count and et not in ('session_flush', 'interaction')]

    def summary(self, n: int = 10) -> str:
        recent = self.recall_recent(n)
        if not recent:
            return 'No episodic memory yet.'
        lines = [f'Recent episodes ({len(self._episodes)} total):']
        for ep in recent:
            ts  = ep['timestamp'][:16]
            sev = ep.get('severity', 'info').upper()
            src = ep.get('source', '')
            src_tag = f'[{src}] ' if src and src not in ('neuron', '') else ''
            lines.append(f'  [{ts}] [{sev}] {src_tag}{ep["event_type"]}: {ep["summary"][:120]}')
        return '\n'.join(lines)

    def byte_size(self) -> int:
        try:
            return EPISODIC_FILE.stat().st_size if EPISODIC_FILE.exists() else 0
        except Exception:
            return 0

    @property
    def total(self) -> int:
        return len(self._episodes)


# =============================================================================
# SEMANTIC MEMORY
# =============================================================================

class SemanticMemory:
    '''
    Semantic memory -- facts and knowledge. Persisted to JSON.
    v3: importance scoring, deduplication by content hash, merge semantics.
    '''

    def __init__(self):
        self._facts: dict = {}
        self._hash_index: dict = {}  # content_hash → key for dedup
        self._load()

    def _load(self) -> None:
        if SEMANTIC_FILE.exists():
            try:
                with _lock(SEMANTIC_FILE):
                    with SEMANTIC_FILE.open('r', encoding='utf-8') as f:
                        self._facts = json.load(f)
                # Rebuild hash index
                self._hash_index = {}
                for k, v in self._facts.items():
                    h = _content_hash(str(v.get('value', '')))
                    self._hash_index[h] = k
                logger.info('[memory/semantic] Loaded %d facts', len(self._facts))
            except Exception as exc:
                logger.warning('[memory/semantic] Load failed: %s', exc)
                self._facts = {}
                self._hash_index = {}

    def _save(self) -> None:
        try:
            if len(self._facts) > MAX_SEMANTIC_ITEMS:
                # Evict lowest-importance items
                scored = sorted(
                    self._facts.items(),
                    key=lambda kv: _importance_score(kv[1])
                )
                for old_key, _ in scored[:len(self._facts) - MAX_SEMANTIC_ITEMS]:
                    val_hash = _content_hash(str(self._facts[old_key].get('value', '')))
                    self._hash_index.pop(val_hash, None)
                    del self._facts[old_key]
            _atomic_write(SEMANTIC_FILE, self._facts)
        except Exception as exc:
            logger.warning('[memory/semantic] Save failed: %s', exc)

    def is_duplicate(self, value: Any) -> Optional[str]:
        """Return the key of an existing fact with the same content, or None."""
        h = _content_hash(str(value))
        return self._hash_index.get(h)

    def learn(self, key: str, value: Any, confidence: float = 0.9,
              source: str = 'observation', merge: bool = True) -> str:
        """
        Store a fact. If merge=True, merges with existing fact at same key
        (takes higher confidence). Deduplicates by content hash.
        Returns 'learned', 'merged', or 'duplicate'.
        """
        val_str = str(value)
        existing_key = self.is_duplicate(value)

        # Exact content duplicate — just bump access count and confidence
        if existing_key and existing_key != key and merge:
            existing = self._facts[existing_key]
            existing['access_count'] = existing.get('access_count', 0) + 1
            if confidence > existing.get('confidence', 0):
                existing['confidence'] = confidence
            self._save()
            return 'duplicate'

        # Existing fact at this key — merge
        if key in self._facts and merge:
            existing = self._facts[key]
            old_hash = _content_hash(str(existing.get('value', '')))
            self._hash_index.pop(old_hash, None)
            # Keep higher confidence
            new_conf = max(confidence, existing.get('confidence', 0))
            self._facts[key] = {
                'value':        value,
                'confidence':   new_conf,
                'source':       source,
                'updated':      _utcnow_iso(),
                'access_count': existing.get('access_count', 0),
                'prev_value':   existing.get('value'),
            }
            h = _content_hash(val_str)
            self._hash_index[h] = key
            self._save()
            return 'merged'

        # New fact
        self._facts[key] = {
            'value':        value,
            'confidence':   confidence,
            'source':       source,
            'updated':      _utcnow_iso(),
            'access_count': 0,
        }
        h = _content_hash(val_str)
        self._hash_index[h] = key
        self._save()
        return 'learned'

    def add_fact(self, key: str, value: Any, confidence: float = 0.9,
                 source: str = 'observation') -> str:
        return self.learn(key, value, confidence, source)

    def recall(self, key: str) -> Optional[dict]:
        fact = self._facts.get(key)
        if fact:
            fact['access_count'] = fact.get('access_count', 0) + 1
        return fact

    def search(self, query: str, max_results: int = 10,
               limit: Optional[int] = None, min_confidence: float = 0.0) -> list:
        cap = limit if limit is not None else max_results
        q = query.lower()
        results = []
        for key, fact in self._facts.items():
            if fact.get('confidence', 0) < min_confidence:
                continue
            if not q or q in key.lower() or q in str(fact.get('value', '')).lower():
                score = _importance_score(fact)
                results.append({'key': key, 'importance': score, **fact})
        results.sort(key=lambda x: x.get('importance', 0), reverse=True)
        return results[:cap]

    def summary(self, n: int = 15) -> str:
        if not self._facts:
            return 'Semantic memory empty -- Neuron is still learning.'
        lines = [f'Semantic memory ({len(self._facts)} facts known):']
        sorted_facts = sorted(
            self._facts.items(),
            key=lambda kv: _importance_score(kv[1]),
            reverse=True,
        )
        for key, fact in sorted_facts[:n]:
            conf = fact.get('confidence', 0)
            val  = str(fact.get('value', ''))[:100]
            lines.append(f'  [{conf:.0%}] {key}: {val}')
        return '\n'.join(lines)

    def all_facts(self) -> list:
        return [{'key': k, **v} for k, v in self._facts.items()]

    def byte_size(self) -> int:
        try:
            return SEMANTIC_FILE.stat().st_size if SEMANTIC_FILE.exists() else 0
        except Exception:
            return 0

    @property
    def total(self) -> int:
        return len(self._facts)


# =============================================================================
# PROCEDURAL MEMORY
# =============================================================================

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


# =============================================================================
# ASSOCIATIVE MEMORY  (new in v3)
# =============================================================================

class AssociativeMemory:
    '''
    Associative memory -- links between concepts, sessions, and sources.
    Enables cross-terminal and cross-session knowledge linking.
    Persisted to JSON.
    '''

    def __init__(self):
        self._associations: list = []
        self._load()

    def _load(self) -> None:
        if ASSOCIATIVE_FILE.exists():
            try:
                with _lock(ASSOCIATIVE_FILE):
                    with ASSOCIATIVE_FILE.open('r', encoding='utf-8') as f:
                        self._associations = json.load(f)
                logger.info('[memory/associative] Loaded %d associations', len(self._associations))
            except Exception as exc:
                logger.warning('[memory/associative] Load failed: %s', exc)
                self._associations = []

    def _save(self) -> None:
        try:
            if len(self._associations) > MAX_ASSOCIATIVE_ITEMS:
                # Keep most recent
                self._associations = self._associations[:MAX_ASSOCIATIVE_ITEMS]
            _atomic_write(ASSOCIATIVE_FILE, self._associations)
        except Exception as exc:
            logger.warning('[memory/associative] Save failed: %s', exc)

    def link(self, concept_a: str, concept_b: str, relation: str = 'related',
             strength: float = 0.7, source: str = 'neuron') -> None:
        """Create an associative link between two concepts."""
        # Check for existing link
        for assoc in self._associations:
            ca, cb = assoc.get('concept_a', ''), assoc.get('concept_b', '')
            if (ca == concept_a and cb == concept_b) or (ca == concept_b and cb == concept_a):
                assoc['strength'] = min(1.0, assoc.get('strength', 0) + 0.1)
                assoc['last_seen'] = _utcnow_iso()
                self._save()
                return
        self._associations.insert(0, {
            'concept_a': concept_a,
            'concept_b': concept_b,
            'relation': relation,
            'strength': strength,
            'source': source,
            'created': _utcnow_iso(),
            'last_seen': _utcnow_iso(),
        })
        self._save()

    def recall_links(self, concept: str, min_strength: float = 0.3) -> list:
        concept_lower = concept.lower()
        results = []
        for assoc in self._associations:
            if (concept_lower in assoc.get('concept_a', '').lower()
                    or concept_lower in assoc.get('concept_b', '').lower()):
                if assoc.get('strength', 0) >= min_strength:
                    results.append(assoc)
        results.sort(key=lambda x: x.get('strength', 0), reverse=True)
        return results[:20]

    def byte_size(self) -> int:
        try:
            return ASSOCIATIVE_FILE.stat().st_size if ASSOCIATIVE_FILE.exists() else 0
        except Exception:
            return 0

    @property
    def total(self) -> int:
        return len(self._associations)


# =============================================================================
# CAPTURE MEMORY  (new in v3) — universal terminal capture
# =============================================================================

class CaptureMemory:
    '''
    Universal AI terminal capture buffer.
    Any terminal running AI operations on this machine can submit here.
    Sources: cline, claude-cli, copilot, aider, continue, custom scripts, etc.
    Captures are ring-buffered and fed into episodic + semantic on consolidation.
    '''

    def __init__(self):
        self._captures: list = []
        self._load()

    def _load(self) -> None:
        if CAPTURE_FILE.exists():
            try:
                with _lock(CAPTURE_FILE):
                    with CAPTURE_FILE.open('r', encoding='utf-8') as f:
                        self._captures = json.load(f)
                logger.info('[memory/capture] Loaded %d captures', len(self._captures))
            except Exception as exc:
                logger.warning('[memory/capture] Load failed: %s', exc)
                self._captures = []

    def _save(self) -> None:
        try:
            if len(self._captures) > MAX_CAPTURE_ITEMS:
                self._captures = self._captures[:MAX_CAPTURE_ITEMS]
            _atomic_write(CAPTURE_FILE, self._captures)
        except Exception as exc:
            logger.warning('[memory/capture] Save failed: %s', exc)

    def ingest(self, source: str, content: str, role: str = 'assistant',
               model: Optional[str] = None, metadata: Optional[dict] = None,
               terminal_id: Optional[str] = None) -> str:
        """
        Ingest a capture from any AI terminal.
        Returns capture ID.
        """
        cap_id = f'cap_{datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")}'
        cap = {
            'id': cap_id,
            'timestamp': _utcnow_iso(),
            'source': source,
            'terminal_id': terminal_id or source,
            'role': role,
            'content': content[:4000],  # cap at 4k chars
            'model': model,
            'metadata': metadata or {},
            'processed': False,  # flag for consolidation
        }
        self._captures.insert(0, cap)
        self._save()
        logger.debug('[memory/capture] Ingested from %s (%s): %d chars', source, role, len(content))
        return cap_id

    def get_unprocessed(self, limit: int = 50) -> list:
        return [c for c in self._captures if not c.get('processed')][:limit]

    def mark_processed(self, cap_ids: List[str]) -> None:
        id_set = set(cap_ids)
        for cap in self._captures:
            if cap.get('id') in id_set:
                cap['processed'] = True
        self._save()

    def recent(self, n: int = 20, source: Optional[str] = None) -> list:
        items = self._captures
        if source:
            items = [c for c in items if c.get('source') == source]
        return items[:n]

    def sources(self) -> Dict[str, int]:
        """Return count of captures per source."""
        counts: Counter = Counter()
        for cap in self._captures:
            counts[cap.get('source', 'unknown')] += 1
        return dict(counts)

    def byte_size(self) -> int:
        try:
            return CAPTURE_FILE.stat().st_size if CAPTURE_FILE.exists() else 0
        except Exception:
            return 0

    @property
    def total(self) -> int:
        return len(self._captures)

    @property
    def unprocessed_count(self) -> int:
        return sum(1 for c in self._captures if not c.get('processed'))


# =============================================================================
# NEURON MEMORY  — unified 5-tier system
# =============================================================================

class NeuronMemory:
    '''
    Unified five-tier memory system for CVG Neuron v3.

    Tiers:
      working     — Short-term session buffer (volatile, auto-flushes)
      episodic    — Long-term event log (ring buffer, persisted)
      semantic    — Facts and knowledge (key-value, deduplicated, persisted)
      procedural  — Patterns and procedures (trigger-matched, persisted)
      associative — Concept links and cross-session associations (persisted)
      capture     — Universal AI terminal capture buffer (persisted)

    Consolidation:
      - Working → Episodic: auto-flush after session_limit turns
      - Episodic → Semantic: consolidate() promotes repeated patterns
      - Captures → Episodic + Semantic: process_captures() ingests external AI ops
    '''

    def __init__(self):
        self.working     = WorkingMemory(session_limit=WORKING_SESSION_LIMIT)
        self.episodic    = EpisodicMemory()
        self.semantic    = SemanticMemory()
        self.procedural  = ProceduralMemory()
        self.associative = AssociativeMemory()
        self.capture     = CaptureMemory()

        # Wire auto-flush
        self.working._flush_callback = self._flush_working_to_episodic

        if self.semantic.total == 0:
            self._seed_initial_knowledge()

        logger.info(
            '[memory] NeuronMemory v3 initialized -- '
            'episodic:%d semantic:%d procedural:%d associative:%d captures:%d',
            self.episodic.total, self.semantic.total,
            self.procedural.total, self.associative.total, self.capture.total,
        )

    # -------------------------------------------------------------------------
    # AUTO-FLUSH: working → episodic
    # -------------------------------------------------------------------------

    def _flush_working_to_episodic(self) -> None:
        items = self.working.recent(WORKING_SESSION_LIMIT)
        if not items:
            return
        summary_lines = []
        for item in items:
            role    = item.get('role', item.get('category', '?'))
            content = str(item.get('content', item.get('value', item.get('key', ''))))
            src     = item.get('source', '')
            src_tag = f'[{src}]' if src and src != 'neuron' else ''
            summary_lines.append(f'{src_tag}[{role}] {content[:200]}')
        self.episodic.record(
            event_type='session_flush',
            summary=f'Auto-flushed {len(items)} working items after {WORKING_SESSION_LIMIT} turns',
            detail={'items': summary_lines, 'source': self.working._source},
            source=self.working._source,
        )
        self.working.clear()
        logger.info('[memory] Working memory auto-flushed to episodic (%d items)', len(items))

    # -------------------------------------------------------------------------
    # CONSOLIDATION: episodic → semantic promotion
    # -------------------------------------------------------------------------

    def consolidate(self) -> dict:
        '''
        Promote frequently repeating episodic patterns into semantic memory.
        Process pending captures from external AI terminals.
        Returns a summary of consolidation actions.
        '''
        actions = {'promoted': 0, 'captures_processed': 0, 'associations_created': 0}

        # 1. Process unprocessed captures
        unprocessed = self.capture.get_unprocessed(limit=100)
        processed_ids = []
        for cap in unprocessed:
            source  = cap.get('source', 'external')
            content = cap.get('content', '')
            role    = cap.get('role', 'assistant')
            model   = cap.get('model', 'unknown')

            # Record as episodic event
            self.episodic.record(
                event_type=f'capture.{source}',
                summary=f'[{source}/{role}] {content[:150]}',
                detail={
                    'content': content[:500],
                    'model': model,
                    'terminal_id': cap.get('terminal_id'),
                    'metadata': cap.get('metadata', {}),
                },
                source=source,
            )

            # Extract and learn key facts from assistant responses
            if role == 'assistant' and len(content) > 50:
                self._extract_and_learn(content, source=source, confidence=0.6)

            # Feed into working memory for immediate context
            self.working.add({
                'role': role,
                'content': content[:500],
                'source': source,
                'model': model,
            })

            processed_ids.append(cap['id'])
            actions['captures_processed'] += 1

        if processed_ids:
            self.capture.mark_processed(processed_ids)

        # 2. Promote repeated episodic patterns to semantic
        patterns = self.episodic.get_frequent_patterns(
            min_count=CONSOLIDATION_REPEAT_THRESHOLD, hours=168
        )
        for event_type, count in patterns:
            recent_of_type = self.episodic.recall_recent(5, event_type=event_type)
            if not recent_of_type:
                continue
            # Summarize the pattern
            summaries = [ep.get('summary', '') for ep in recent_of_type]
            combined = ' | '.join(summaries[:3])
            fact_key = f'pattern.{event_type}.{_content_hash(combined)}'
            conf = min(0.9, CONSOLIDATION_MIN_CONFIDENCE + count * 0.05)
            result = self.semantic.learn(
                key=fact_key,
                value=f'Repeated {count}x in 7 days: {combined[:300]}',
                confidence=conf,
                source='consolidation',
            )
            if result in ('learned', 'merged'):
                actions['promoted'] += 1
                logger.debug('[memory/consolidate] Promoted pattern: %s (x%d)', event_type, count)

        # 3. Create associations between sources that appear together in episodes
        recent_eps = self.episodic.recent(50)
        source_set: Counter = Counter()
        for ep in recent_eps:
            src = ep.get('source', '')
            if src:
                source_set[src] += 1
        common_sources = [s for s, _ in source_set.most_common(5) if _ >= 2]
        for i, sa in enumerate(common_sources):
            for sb in common_sources[i + 1:]:
                self.associative.link(sa, sb, relation='co-active', strength=0.6, source='consolidation')
                actions['associations_created'] += 1

        logger.info('[memory/consolidate] Promoted=%d captures=%d associations=%d',
                    actions['promoted'], actions['captures_processed'], actions['associations_created'])
        return actions

    def _extract_and_learn(self, content: str, source: str = 'external',
                           confidence: float = 0.6) -> int:
        """
        Extract factual statements from AI response content and add to semantic memory.
        Returns number of facts extracted.
        """
        learned = 0
        for line in content.split('\n'):
            line = line.strip()
            # Filter for plausible factual statements
            if len(line) < 25 or len(line) > 300:
                continue
            if line.startswith(('#', '-', '*', '`', '>', '|')):
                continue
            # Look for definitive patterns
            factual_patterns = [
                'CVG ', 'Neuron is', 'cluster is', 'hive-0', 'hive0',
                'queen-', '10.10.10.', 'clearview', 'cleargeo',
                ' is running', ' is deployed', ' is configured',
                ' version ', 'port ', 'endpoint ',
            ]
            is_factual = any(p.lower() in line.lower() for p in factual_patterns)
            if is_factual:
                fact_key = f'capture.{source}.{_content_hash(line)}'
                self.semantic.learn(
                    key=fact_key,
                    value=line,
                    confidence=confidence,
                    source=source,
                )
                learned += 1
                if learned >= 5:  # max 5 facts per content block
                    break
        return learned

    # -------------------------------------------------------------------------
    # STATS
    # -------------------------------------------------------------------------

    def stats(self) -> dict:
        eb = self.episodic.byte_size()
        sb = self.semantic.byte_size()
        pb = self.procedural.byte_size()
        wb = self.working.byte_size()
        ab = self.associative.byte_size()
        cb = self.capture.byte_size()
        total = eb + sb + pb + wb + ab + cb
        return {
            'working_items':          self.working.item_count,
            'working_turns':          self.working.turn_count,
            'episodic_episodes':      self.episodic.total,
            'semantic_facts':         self.semantic.total,
            'procedural_patterns':    self.procedural.total,
            'associative_links':      self.associative.total,
            'capture_total':          self.capture.total,
            'capture_unprocessed':    self.capture.unprocessed_count,
            'capture_sources':        self.capture.sources(),
            'working_bytes':          wb,
            'episodic_bytes':         eb,
            'semantic_bytes':         sb,
            'procedural_bytes':       pb,
            'associative_bytes':      ab,
            'capture_bytes':          cb,
            'total_bytes':            total,
            'total_kb':               round(total / 1024, 1),
            'working_session_limit':  WORKING_SESSION_LIMIT,
            'episodic_max':           MAX_EPISODIC_ITEMS,
            'semantic_max':           MAX_SEMANTIC_ITEMS,
        }

    def get_stats(self) -> dict:
        return self.stats()

    # -------------------------------------------------------------------------
    # SEARCH
    # -------------------------------------------------------------------------

    def search(self, query: str, limit: int = 20) -> dict:
        per = max(1, limit // 5)
        q = query.lower()
        return {
            'working':      [i for i in self.working.recent(limit) if q in str(i).lower()][:per],
            'episodic':     self.episodic.search(query, limit=per),
            'semantic':     self.semantic.search(query, limit=per),
            'procedural':   self.procedural.search(query, limit=per),
            'associative':  self.associative.recall_links(query),
            'captures':     [c for c in self.capture.recent(limit) if q in str(c).lower()][:per],
            'query':        query,
        }

    # -------------------------------------------------------------------------
    # CONTEXT SUMMARY — richer warm-start for LLM
    # -------------------------------------------------------------------------

    def build_context_summary(self) -> str:
        parts = []
        es = self.episodic.summary(8)
        if es:
            parts.append(es)
        ws = self.working.summary()
        if ws:
            parts.append(ws)
        # Include capture sources for cross-terminal awareness
        sources = self.capture.sources()
        if sources:
            src_str = ', '.join(f'{s}:{n}' for s, n in sorted(sources.items(), key=lambda x: -x[1])[:5])
            parts.append(f'Active AI terminals: {src_str}')
        return '\n\n'.join(parts)

    def build_rich_context(self, query: str = '') -> str:
        '''
        Build a rich, query-aware context summary for LLM injection.
        Includes relevant semantic facts, recent episodes, working state,
        and cross-terminal captures.
        '''
        lines = ['[MEMORY CONTEXT]']

        # Top semantic facts relevant to query (or top importance if no query)
        semantic = self.semantic.search(query, limit=10) if query else self.semantic.search('', limit=8)
        if semantic:
            lines.append('\nKnown facts (by importance):')
            for f in semantic[:8]:
                conf = f.get('confidence', 0)
                lines.append(f'  [{conf:.0%}] {f["key"]}: {str(f.get("value",""))[:120]}')

        # Recent episodic events
        recent_eps = self.episodic.recent(6)
        if recent_eps:
            lines.append('\nRecent events:')
            for ep in recent_eps:
                ts  = ep['timestamp'][:16]
                src = ep.get('source', '')
                src_tag = f'[{src}]' if src and src not in ('neuron', '') else ''
                lines.append(f'  {src_tag}[{ts}] {ep["event_type"]}: {ep["summary"][:100]}')

        # Working memory recent turns
        working = self.working.recent(8)
        if working:
            lines.append('\nRecent conversation:')
            for w in working[-6:]:
                role = w.get('role', '?')
                content = str(w.get('content', ''))[:120]
                src = w.get('source', '')
                src_tag = f'[{src}]' if src and src not in ('neuron', '') else ''
                lines.append(f'  {src_tag}[{role}] {content}')

        # Cross-terminal capture summary
        sources = self.capture.sources()
        if sources:
            src_str = ', '.join(f'{s}({n})' for s, n in sorted(sources.items(), key=lambda x: -x[1])[:5])
            lines.append(f'\nAI terminal activity: {src_str}')

            # Show most recent external captures
            ext_captures = [c for c in self.capture.recent(10)
                            if c.get('source') not in ('neuron', '')][:3]
            if ext_captures:
                lines.append('Recent external AI activity:')
                for cap in ext_captures:
                    ts  = cap.get('timestamp', '')[:16]
                    src = cap.get('source', '?')
                    role = cap.get('role', '?')
                    content = cap.get('content', '')[:100]
                    lines.append(f'  [{ts}][{src}/{role}] {content}')

        # Relevant associations
        if query:
            assocs = self.associative.recall_links(query, min_strength=0.4)
            if assocs:
                lines.append('\nRelated concepts:')
                for a in assocs[:4]:
                    lines.append(f'  {a["concept_a"]} --[{a["relation"]}]--> {a["concept_b"]} (strength:{a["strength"]:.2f})')

        return '\n'.join(lines)

    # -------------------------------------------------------------------------
    # EXPORT / IMPORT / PERSIST
    # -------------------------------------------------------------------------

    def export(self, label: str = '') -> Path:
        ts = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        tag = f'_{label}' if label else ''
        export_path = EXPORT_DIR / f'neuron_memory_{ts}{tag}'
        export_path.mkdir(parents=True, exist_ok=True)
        self.persist()
        manifest: Dict[str, Any] = {
            'exported_at': _utcnow_iso(), 'label': label,
            'stats': self.stats(), 'files': [], 'version': 3,
        }
        for src in (EPISODIC_FILE, SEMANTIC_FILE, PROCEDURAL_FILE,
                    ASSOCIATIVE_FILE, CAPTURE_FILE):
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
            'episodic.json':    EPISODIC_FILE,
            'semantic.json':    SEMANTIC_FILE,
            'procedural.json':  PROCEDURAL_FILE,
            'associative.json': ASSOCIATIVE_FILE,
            'captures.json':    CAPTURE_FILE,
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
        self.episodic    = EpisodicMemory()
        self.semantic    = SemanticMemory()
        self.procedural  = ProceduralMemory()
        self.associative = AssociativeMemory()
        self.capture     = CaptureMemory()
        logger.info('[memory] Import complete: %s', results)
        return results

    def persist(self) -> None:
        self.episodic._save()
        self.semantic._save()
        self.procedural._save()
        self.associative._save()
        self.capture._save()
        logger.debug('[memory] All tiers persisted to disk')

    # -------------------------------------------------------------------------
    # SEED INITIAL KNOWLEDGE
    # -------------------------------------------------------------------------

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
            'cvg.neuron.memory.version': ('v3 -- 5-tier with associative + capture', 1.0, 'built-in'),
            'cvg.internal.key':         ('cvg-internal-2026', 1.0, 'built-in'),
            'cvg.ollama.host':          ('10.10.10.200:11434 (vm-451 / cvg-stormsurge-01)', 1.0, 'built-in'),
            'cvg.company':              ('Clearview Geographic, LLC', 1.0, 'built-in'),
            'cvg.principal':            ('Alex Zelenski, GISP (President and CEO)', 1.0, 'built-in'),
            'cvg.staff.support':        ('Jennifer Mounivong (client support)', 1.0, 'built-in'),
            'cvg.staff.science':        ('Dr. Jason Evans PhD (Chief Science Officer)', 1.0, 'built-in'),
            'cvg.location':             ('DeLand, FL 32720 (HQ) / New Smyrna Beach (Cluster)', 1.0, 'built-in'),
            # AI terminal integrations (new in v3)
            'cvg.ai.terminals.supported': (
                'cline, claude-cli, copilot, aider, continue, custom scripts -- '
                'all feed into Neuron capture memory via /api/memory/capture',
                1.0, 'built-in',
            ),
            'cvg.ai.capture.endpoint': (
                'POST http://localhost:8095/api/memory/capture (no auth required from localhost)',
                1.0, 'built-in',
            ),
        }
        for key, (value, confidence, source) in initial_facts.items():
            self.semantic.learn(key, value, confidence, source)
        logger.info('[memory] Seeded %d initial CVG knowledge facts (v3)', len(initial_facts))


# =============================================================================
# MODULE SINGLETON
# =============================================================================

_memory: Optional[NeuronMemory] = None


def get_memory() -> NeuronMemory:
    global _memory
    if _memory is None:
        _memory = NeuronMemory()
    return _memory
