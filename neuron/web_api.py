# CVG Neuron -- FastAPI Web API v2
# (c) Clearview Geographic, LLC -- Proprietary and PRIVATE
#
# v2 improvements:
#   - /api/health/deep endpoint (Ollama + memory + cluster)
#   - /api/memory/stats endpoint
#   - /api/memory/search?q= endpoint
#   - /api/memory/export and /api/memory/import endpoints
#   - /api/stream SSE streaming endpoint
#   - X-Request-ID header tracking
#   - Rate limiting (token bucket, 60 req/min default)
#   - Fixed import handling
#   - Preserved all existing endpoints

import logging
import os
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .context_builder import refresh_context
from .edge_connector import IntelligencePayload, get_edge_network
from .identity import get_identity_card
from .memory import get_memory
from .mind import get_mind
from .cluster import get_cluster

logger = logging.getLogger('neuron.api')

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

CVG_INTERNAL_KEY = os.getenv('CVG_INTERNAL_KEY', 'cvg-internal-2026')


def require_cvg_key(request: Request) -> None:
    provided = request.headers.get('X-CVG-Key', '')
    if provided != CVG_INTERNAL_KEY:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail='Invalid or missing CVG internal key')


# ---------------------------------------------------------------------------
# Rate Limiting -- Token Bucket (in-memory, per IP)
# ---------------------------------------------------------------------------

RATE_LIMIT_RPM = int(os.getenv('CVG_RATE_LIMIT_RPM', '60'))  # requests per minute
_rate_buckets: Dict[str, Dict] = defaultdict(lambda: {'tokens': float(RATE_LIMIT_RPM), 'last_refill': time.monotonic()})


def _check_rate_limit(client_ip: str) -> bool:
    '''
    Token bucket rate limiter. Returns True if request is allowed, False if rate-limited.
    Refills at RATE_LIMIT_RPM tokens per 60 seconds.
    '''
    now = time.monotonic()
    bucket = _rate_buckets[client_ip]
    elapsed = now - bucket['last_refill']
    # Refill tokens proportionally
    bucket['tokens'] = min(float(RATE_LIMIT_RPM), bucket['tokens'] + elapsed * (RATE_LIMIT_RPM / 60.0))
    bucket['last_refill'] = now
    if bucket['tokens'] >= 1.0:
        bucket['tokens'] -= 1.0
        return True
    return False


async def rate_limit(request: Request) -> None:
    client_ip = request.client.host if request.client else 'unknown'
    if not _check_rate_limit(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f'Rate limit exceeded: {RATE_LIMIT_RPM} req/min. Retry after 60s.',
            headers={'Retry-After': '60'},
        )


# ---------------------------------------------------------------------------
# Request ID Middleware
# ---------------------------------------------------------------------------

class RequestIDMiddleware:
    '''
    Adds X-Request-ID and X-Neuron-Version headers to every response.
    Uses incoming X-Request-ID if present, otherwise generates a UUID4.
    '''
    NEURON_VERSION = b'2.0.0'

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            headers = dict(scope.get('headers', []))
            request_id = headers.get(b'x-request-id', b'').decode() or str(uuid.uuid4())

            async def send_with_headers(message):
                if message['type'] == 'http.response.start':
                    new_headers = list(message.get('headers', []))
                    new_headers.append((b'x-request-id', request_id.encode()))
                    new_headers.append((b'x-neuron-version', self.NEURON_VERSION))
                    new_headers.append((b'x-neuron-id', b'CVG-NEURON-001'))
                    message = {**message, 'headers': new_headers}
                await send(message)

            await self.app(scope, receive, send_with_headers)
        else:
            await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ThinkRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    context_type: str = Field(default='general')
    history: Optional[List[Dict[str, str]]] = Field(default=None)


class LearnRequest(BaseModel):
    key: str = Field(..., min_length=1, max_length=200)
    value: Any
    source: str = Field(default='api_direct')
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)


class EdgeRegisterRequest(BaseModel):
    edge_id: str = Field(..., min_length=1, max_length=100)
    endpoint: str = Field(..., min_length=1)
    connector_type: str = Field(default='edge_feeder')
    name: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class EdgeFeedRequest(BaseModel):
    edge_id: str
    payload_type: str = Field(default='event')
    data: Dict[str, Any]
    signature: str
    timestamp: Optional[float] = None
    priority: int = Field(default=5, ge=1, le=10)


class WebhookEvent(BaseModel):
    source: str
    event_type: str
    severity: str = 'info'
    data: Dict[str, Any] = {}
    timestamp: Optional[str] = None


class AnalyzeTypeRequest(BaseModel):
    model: Optional[str] = None
    temperature: float = Field(default=0.3)
    force_refresh: bool = Field(default=False)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

scheduler = AsyncIOScheduler(timezone='UTC')


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info('=' * 60)
    logger.info('CVG NEURON -- PRIVATE AI -- BOOTING')
    logger.info('=' * 60)

    try:
        memory = get_memory()
        stats = memory.stats()
        logger.info('Memory online: semantic=%d facts, episodic=%d episodes, procedural=%d patterns',
                    stats.get('semantic_facts', 0), stats.get('episodic_episodes', 0),
                    stats.get('procedural_patterns', 0))
    except Exception as exc:
        logger.error('Memory init failed: %s', exc)

    try:
        get_mind()
        logger.info('Cognitive engine online')
    except Exception as exc:
        logger.error('Mind init failed: %s', exc)

    try:
        get_edge_network()
        logger.info('Edge network online')
    except Exception as exc:
        logger.error('Edge network init failed: %s', exc)

    try:
        import asyncio
        cluster = get_cluster()
        asyncio.create_task(_background_cluster_scan(cluster))
    except Exception as exc:
        logger.warning('Cluster scan task failed to start: %s', exc)

    scheduler.add_job(refresh_context, 'interval', minutes=5, id='ctx_refresh')
    scheduler.start()
    logger.info('Context refresh scheduler started (interval: 5 min)')
    logger.info('Rate limiting: %d req/min per IP', RATE_LIMIT_RPM)
    logger.info('CVG NEURON IS OPERATIONAL')
    yield

    logger.info('CVG Neuron shutting down -- persisting memory...')
    try:
        get_memory().persist()
        logger.info('Memory persisted successfully')
    except Exception as exc:
        logger.error('Memory persist on shutdown failed: %s', exc)
    scheduler.shutdown(wait=False)
    logger.info('CVG Neuron offline')


async def _background_cluster_scan(cluster) -> None:
    import asyncio
    await asyncio.sleep(3)
    try:
        result = await cluster.scan_cluster()
        online = result.get('online_nodes', 0)
        total  = result.get('total_nodes', 0)
        logger.info('Cluster scan complete: %d/%d nodes online', online, total)
        get_mind().learn(
            key='cluster.last_scan',
            value=f'{online}/{total} nodes online at {datetime.now(timezone.utc).isoformat()}',
            source='startup_scan', confidence=1.0,
        )
    except Exception as exc:
        logger.warning('Background cluster scan failed: %s', exc)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title='CVG Neuron',
    description='Private AI -- Clearview Geographic, LLC -- NOT a public model',
    version='2.0.0',
    docs_url='/docs',
    redoc_url=None,
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_methods=['*'],
    allow_headers=['*'],
)

# Request ID tracking middleware
app.add_middleware(RequestIDMiddleware)

# Extended routes
try:
    from .routes_extended import router as ext_router, hive0_router
    app.include_router(ext_router)
    app.include_router(hive0_router)
except ImportError as exc:
    logger.warning('routes_extended not loaded: %s', exc)


# ---------------------------------------------------------------------------
# Core health endpoints
# ---------------------------------------------------------------------------

@app.get('/health')
async def health():
    return {
        'status': 'ok',
        'service': 'cvg-neuron',
        'version': '2.0.0',
        'classification': 'PRIVATE',
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }


@app.get('/api/health/deep', dependencies=[Depends(require_cvg_key), Depends(rate_limit)])
async def health_deep():
    '''
    Deep health check: probes Ollama, memory, and cluster simultaneously.
    Returns comprehensive operational status.
    '''
    from .ollama_client import get_ollama_client
    ollama = get_ollama_client()

    import asyncio
    ollama_task  = asyncio.create_task(ollama.health_detail())
    cluster_task = asyncio.create_task(get_cluster().scan_cluster())
    ollama_detail, cluster_result = await asyncio.gather(ollama_task, cluster_task, return_exceptions=True)

    # Memory stats (synchronous)
    mem = get_memory()
    try:
        mem_stats = mem.stats()
        mem_ok = True
    except Exception as exc:
        mem_stats = {'error': str(exc)}
        mem_ok = False

    # Handle exceptions from gather
    if isinstance(ollama_detail, Exception):
        ollama_detail = {'status': 'error', 'error': str(ollama_detail)}
    if isinstance(cluster_result, Exception):
        cluster_result = {'error': str(cluster_result), 'online_nodes': 0}

    ollama_ok = isinstance(ollama_detail, dict) and ollama_detail.get('status') == 'online'
    cluster_ok = isinstance(cluster_result, dict) and cluster_result.get('online_nodes', 0) > 0

    overall = 'healthy' if (ollama_ok and mem_ok) else ('degraded' if mem_ok else 'critical')

    return {
        'status':    overall,
        'service':   'cvg-neuron',
        'version':   '2.0.0',
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'checks': {
            'ollama':  {'ok': ollama_ok,  'detail': ollama_detail},
            'memory':  {'ok': mem_ok,     'stats': mem_stats},
            'cluster': {
                'ok': cluster_ok,
                'online_nodes': cluster_result.get('online_nodes', 0) if isinstance(cluster_result, dict) else 0,
                'total_nodes':  cluster_result.get('total_nodes', 0)  if isinstance(cluster_result, dict) else 0,
                'last_scan':    cluster_result.get('timestamp')        if isinstance(cluster_result, dict) else None,
            },
        },
    }


@app.get('/identity')
async def identity():
    return get_identity_card()


# ---------------------------------------------------------------------------
# Core cognitive endpoints
# ---------------------------------------------------------------------------

@app.post('/api/think', dependencies=[Depends(require_cvg_key), Depends(rate_limit)])
async def think(req: ThinkRequest):
    valid_ctx = {'general', 'infrastructure', 'git', 'dns', 'security', 'synthesis'}
    ctx = req.context_type if req.context_type in valid_ctx else 'general'
    return await get_mind().think(message=req.message, context_type=ctx,
                                   conversation_history=req.history)


@app.post('/api/chat', dependencies=[Depends(require_cvg_key), Depends(rate_limit)])
async def chat(req: ThinkRequest):
    result = await get_mind().think(message=req.message, context_type='general',
                                     conversation_history=req.history)
    return {
        'message':        result['response'],
        'confidence':     result['confidence'],
        'confidence_score': result['confidence_score'],
        'sources':        result['sources'],
        'verified':       result['verified'],
        'interaction_id': result['interaction_id'],
        'elapsed_ms':     result['elapsed_ms'],
    }


@app.post('/api/analyze', dependencies=[Depends(require_cvg_key), Depends(rate_limit)])
async def analyze(req: ThinkRequest):
    ctx = req.context_type if req.context_type != 'general' else 'synthesis'
    return await get_mind().think(message=req.message, context_type=ctx,
                                   conversation_history=req.history)


@app.get('/api/reflect', dependencies=[Depends(require_cvg_key), Depends(rate_limit)])
async def reflect():
    return await get_mind().reflect()


# ---------------------------------------------------------------------------
# Streaming endpoint (SSE)
# ---------------------------------------------------------------------------

@app.post('/api/stream', dependencies=[Depends(require_cvg_key), Depends(rate_limit)])
async def stream_response(req: ThinkRequest):
    '''
    SSE streaming endpoint. Returns Server-Sent Events format.
    Events: {event: step|chunk|done|error, data: ...}
    '''
    import json as _json

    async def event_generator():
        mind = get_mind()
        valid_ctx = {'general', 'infrastructure', 'git', 'dns', 'security', 'synthesis'}
        ctx = req.context_type if req.context_type in valid_ctx else 'general'
        try:
            async for event in mind.think_stream(
                message=req.message,
                context_type=ctx,
                conversation_history=req.history,
            ):
                event_type = event.get('event', 'message')
                data = _json.dumps(event.get('data', ''))
                yield f'event: {event_type}\ndata: {data}\n\n'
        except Exception as exc:
            yield f'event: error\ndata: {_json.dumps({"error": str(exc)})}\n\n'

    return StreamingResponse(
        event_generator(),
        media_type='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


# ---------------------------------------------------------------------------
# Memory endpoints
# ---------------------------------------------------------------------------

@app.post('/api/memory/learn', dependencies=[Depends(require_cvg_key), Depends(rate_limit)])
async def memory_learn(req: LearnRequest):
    return get_mind().learn(key=req.key, value=req.value, source=req.source,
                             confidence=req.confidence)


@app.get('/api/memory/recall', dependencies=[Depends(require_cvg_key), Depends(rate_limit)])
async def memory_recall(query: str = '', limit: int = 10):
    if not query:
        return {'stats': get_memory().stats(), 'recent_episodes': get_memory().episodic.recent(10)}
    return get_mind().recall(query=query, limit=min(limit, 50))


@app.get('/api/memory/stats', dependencies=[Depends(require_cvg_key), Depends(rate_limit)])
async def memory_stats():
    '''
    Return detailed memory statistics across all tiers.
    Includes per-tier byte sizes, counts, and limits.
    '''
    mem = get_memory()
    return {
        'stats':           mem.stats(),
        'recent_working':  mem.working.recent(5),
        'recent_episodes': mem.episodic.recent(5),
        'top_facts':       mem.semantic.search('', limit=5),
        'timestamp':       datetime.now(timezone.utc).isoformat(),
    }


@app.get('/api/memory/search', dependencies=[Depends(require_cvg_key), Depends(rate_limit)])
async def memory_search(q: str = '', limit: int = 20):
    '''
    Cross-tier memory search. Searches working, episodic, semantic, and procedural memory.
    Returns results from all tiers simultaneously.
    '''
    if not q:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail='Query parameter q is required')
    mem = get_memory()
    results = mem.search(query=q, limit=min(limit, 100))
    total = sum(len(v) for v in results.values() if isinstance(v, list))
    return {
        'query':   q,
        'results': results,
        'total':   total,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }


@app.post('/api/memory/export', dependencies=[Depends(require_cvg_key)])
async def memory_export(label: str = ''):
    '''
    Export all memory tiers to a timestamped backup directory.
    Returns the export path and manifest summary.
    '''
    try:
        mem = get_memory()
        export_path = mem.export(label=label)
        manifest_file = export_path / 'manifest.json'
        import json
        manifest = json.loads(manifest_file.read_text(encoding='utf-8')) if manifest_file.exists() else {}
        return {
            'status':      'exported',
            'export_path': str(export_path),
            'label':       label,
            'manifest':    manifest,
        }
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f'Export failed: {exc}')


@app.post('/api/memory/import', dependencies=[Depends(require_cvg_key)])
async def memory_import(request: Request, overwrite: bool = False):
    '''
    Import memory from a previously exported backup directory.
    Restores episodic, semantic, and procedural tiers from JSON files.

    Body: {"export_dir": "/app/data/memory_exports/neuron_memory_YYYYMMDD_HHMMSS"}
    Query: overwrite=true to replace existing memory (default: false = skip existing)

    WARNING: With overwrite=true, existing memory is replaced. Use with care.
    '''
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail='Request body must be valid JSON: {"export_dir": "..."}')

    export_dir = body.get('export_dir', '').strip()
    if not export_dir:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail='export_dir is required in request body')

    try:
        from pathlib import Path as _Path
        mem = get_memory()
        results = mem.import_backup(_Path(export_dir), overwrite=overwrite)
        logger.info('Memory import complete from %s (overwrite=%s): %s',
                    export_dir, overwrite, results)
        return {
            'status':     'imported',
            'export_dir': export_dir,
            'overwrite':  overwrite,
            'results':    results,
            'stats':      mem.stats(),
            'timestamp':  datetime.now(timezone.utc).isoformat(),
        }
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except Exception as exc:
        logger.error('Memory import failed: %s', exc)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f'Import failed: {exc}')


@app.delete('/api/memory/working', dependencies=[Depends(require_cvg_key)])
async def memory_clear_working():
    get_memory().working.clear()
    return {'status': 'cleared', 'tier': 'working'}


# ---------------------------------------------------------------------------
# Webhook / event ingestion
# ---------------------------------------------------------------------------

@app.post('/api/webhook', dependencies=[Depends(require_cvg_key), Depends(rate_limit)])
async def webhook(event: WebhookEvent):
    logger.info('Webhook: source=%s type=%s severity=%s', event.source, event.event_type, event.severity)
    get_memory().episodic.record(
        event_type=f'webhook.{event.event_type}',
        summary=f'[{event.source}] {event.event_type}: {str(event.data)[:150]}',
        metadata={'source': event.source, 'severity': event.severity, 'data': event.data},
    )
    analysis_result = None
    if event.severity.lower() in {'critical', 'error', 'high', 'emergency'}:
        logger.warning('Critical webhook event -- triggering cognitive analysis')
        analysis_result = await get_mind().think(
            message=(f'Critical event from {event.source}: {event.event_type}\n'
                     f'Severity: {event.severity}\nData: {str(event.data)[:500]}\n\n'
                     f'Assess this event and recommend immediate action.'),
            context_type='infrastructure',
        )
    response: Dict[str, Any] = {
        'status': 'received', 'source': event.source,
        'event_type': event.event_type, 'recorded': True,
    }
    if analysis_result:
        response['cognitive_assessment'] = analysis_result.get('response', '')[:1000]
        response['confidence'] = analysis_result.get('confidence', 'UNCERTAIN')
        response['confidence_score'] = analysis_result.get('confidence_score', 0.5)
    return response


# ---------------------------------------------------------------------------
# Edge network endpoints
# ---------------------------------------------------------------------------

@app.post('/api/edge/register', dependencies=[Depends(require_cvg_key)])
async def edge_register(req: EdgeRegisterRequest):
    edge = get_edge_network()
    result = edge.register_connector(edge_id=req.edge_id, endpoint=req.endpoint,
                                     connector_type=req.connector_type,
                                     name=req.name, metadata=req.metadata)
    get_mind().learn(
        key=f'edge.connector.{req.edge_id}',
        value=f'type={req.connector_type}, endpoint={req.endpoint}, registered={datetime.now(timezone.utc).isoformat()}',
        source='edge_registration', confidence=1.0,
    )
    return result


@app.post('/api/edge/feed')
async def edge_feed(req: EdgeFeedRequest, request: Request):
    edge = get_edge_network()
    payload = IntelligencePayload(
        edge_id=req.edge_id, payload_type=req.payload_type, data=req.data,
        signature=req.signature, timestamp=req.timestamp, priority=req.priority,
        source_ip=request.client.host if request.client else None,
    )
    result = await edge.ingest(payload, require_signature=True)
    if result.get('status') == 'rejected':
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=result.get('reason', 'rejected'))
    return result


@app.get('/api/edge/connectors', dependencies=[Depends(require_cvg_key)])
async def edge_list_connectors():
    return {'connectors': get_edge_network().list_connectors(), 'stats': get_edge_network().stats()}


@app.get('/api/edge/feeds', dependencies=[Depends(require_cvg_key)])
async def edge_recent_feeds(limit: int = 50):
    feeds = get_edge_network().recent_feeds(min(limit, 200))
    return {'feeds': feeds, 'count': len(feeds)}


@app.delete('/api/edge/connectors/{edge_id}', dependencies=[Depends(require_cvg_key)])
async def edge_deregister(edge_id: str):
    return get_edge_network().deregister_connector(edge_id)


# ---------------------------------------------------------------------------
# Cluster endpoints
# ---------------------------------------------------------------------------

@app.get('/api/cluster/nodes', dependencies=[Depends(require_cvg_key)])
async def cluster_nodes():
    cluster = get_cluster()
    return {'nodes': cluster.get_node_status(), 'last_scan': cluster._last_scan,
            'edge_connectors': cluster.list_edge_connectors()}


@app.post('/api/cluster/scan', dependencies=[Depends(require_cvg_key)])
async def cluster_scan():
    cluster = get_cluster()
    result = await cluster.scan_cluster()
    online = result.get('online_nodes', 0)
    total  = result.get('total_nodes', 0)
    get_mind().learn(
        key='cluster.last_scan',
        value=f'{online}/{total} nodes online at {datetime.now(timezone.utc).isoformat()}',
        source='manual_scan', confidence=1.0,
    )
    return {'nodes': result.get('nodes', {}), 'online': online, 'total': total,
            'scanned_at': datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------------------
# Dashboard status (public -- no auth)
# ---------------------------------------------------------------------------

@app.get('/api/status')
async def api_status():
    from .ollama_client import get_ollama_client
    from .context_builder import get_cached_context

    client = get_ollama_client()
    ollama_detail = await client.health_detail()

    try:
        ctx = await get_cached_context()
        engines = {
            'git':            ctx.get('git', {}).get('status', 'unknown'),
            'dns':            ctx.get('dns', {}).get('status', 'unknown'),
            'container':      ctx.get('container', {}).get('status', 'unknown'),
            'audit':          ctx.get('audit', {}).get('status', 'unknown'),
            'engines_online': ctx.get('engines_online', 0),
            'last_refresh':   ctx.get('timestamp'),
        }
    except Exception as exc:
        logger.warning('api_status: context fetch failed: %s', exc)
        engines = {'engines_online': 0, 'git': 'unknown', 'dns': 'unknown',
                   'container': 'unknown', 'audit': 'unknown'}

    return {
        'status':    'operational',
        'service':   'cvg-neuron',
        'version':   '2.0.0',
        'ollama':    ollama_detail,
        'engines':   engines,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Models listing
# ---------------------------------------------------------------------------

@app.get('/api/models', dependencies=[Depends(require_cvg_key)])
async def list_models():
    from .ollama_client import get_ollama_client
    client = get_ollama_client()
    models = await client.list_models()
    return {'models': models, 'default': os.getenv('OLLAMA_MODEL', 'cvg-neuron'),
            'count': len(models)}


# ---------------------------------------------------------------------------
# History (episodic memory)
# ---------------------------------------------------------------------------

@app.get('/api/history', dependencies=[Depends(require_cvg_key)])
async def get_history(limit: int = 50):
    episodes = get_memory().episodic.recent(min(limit, 200))
    entries = []
    for ep in episodes:
        meta = ep.get('detail') or {}
        entries.append({
            'type':       ep.get('event_type', 'unknown'),
            'timestamp':  ep.get('timestamp', ''),
            'snippet':    ep.get('summary', '')[:300],
            'model':      meta.get('model', os.getenv('OLLAMA_MODEL', 'cvg-neuron')) if isinstance(meta, dict) else os.getenv('OLLAMA_MODEL', 'cvg-neuron'),
            'confidence': meta.get('confidence', '') if isinstance(meta, dict) else '',
            'confidence_score': meta.get('confidence_score', 0.5) if isinstance(meta, dict) else 0.5,
        })
    return {'entries': entries, 'count': len(entries)}


# ---------------------------------------------------------------------------
# Parametric analysis (Analyze tab in dashboard UI)
# ---------------------------------------------------------------------------

_ANALYSIS_PROMPTS: Dict[str, tuple] = {
    'infrastructure': (
        'Perform a comprehensive infrastructure analysis of the CVG Hive-0 cluster. '
        'Include: container health, resource pressure, node status across vm-451, vm-454, vm-455. '
        'Identify any errors, warnings, or capacity concerns. Be specific and actionable.',
        'infrastructure',
    ),
    'git': (
        'Analyze the current state of CVG Git repositories and version control activity. '
        'Include: recent commits, AI-generated commit detection, deploy cadence, stale branches. '
        'Reference Gitea at git.cleargeo.tech. Be specific and actionable.',
        'git',
    ),
    'dns': (
        'Analyze the CVG DNS health for both cleargeo.tech external (cPanel/WHM) and internal BIND9. '
        'Identify any resolution issues, zone misconfigurations, or propagation problems. '
        'Be specific and actionable.',
        'dns',
    ),
    'security': (
        'Analyze the CVG security posture. Review Wazuh alerts, Trivy CVE findings, '
        'and anomalies from the Audit VM at 10.10.10.220. '
        'Identify top risks and recommended mitigations. Be specific.',
        'security',
    ),
    'full': (
        'Perform a complete CVG platform synthesis analysis. Correlate data across all four '
        'support engines: Git, DNS, Container/Infrastructure, and Security/Audit. '
        'Identify cross-cutting issues, emerging patterns, and priority action items. '
        'Provide a mission briefing-style summary with clear action items.',
        'synthesis',
    ),
}


@app.post('/api/analyze/{analysis_type}', dependencies=[Depends(require_cvg_key), Depends(rate_limit)])
async def analyze_by_type(analysis_type: str, req: AnalyzeTypeRequest):
    if analysis_type not in _ANALYSIS_PROMPTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Unknown analysis type: \'{analysis_type}\'. Valid: {list(_ANALYSIS_PROMPTS.keys())}',
        )
    prompt, context_type = _ANALYSIS_PROMPTS[analysis_type]
    if req.force_refresh:
        try:
            from .context_builder import get_cached_context as _ctx
            await _ctx(force_refresh=True)
        except Exception as exc:
            logger.warning('Force-refresh failed: %s', exc)
    result = await get_mind().think(message=prompt, context_type=context_type)
    return {
        'result':          result['response'],
        'confidence':      result['confidence'],
        'confidence_score': result['confidence_score'],
        'analysis_type':   analysis_type,
        'model':           os.getenv('OLLAMA_MODEL', 'cvg-neuron'),
        'elapsed_ms':      result['elapsed_ms'],
        'verified':        result['verified'],
        'sources':         result.get('sources', []),
    }


# ---------------------------------------------------------------------------
# Static UI
# ---------------------------------------------------------------------------

try:
    app.mount('/', StaticFiles(directory='static', html=True), name='static')
except Exception:
    logger.warning('Static files not found at ./static -- UI unavailable')
