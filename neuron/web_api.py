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


class CaptureRequest(BaseModel):
    source: str = Field(..., min_length=1, max_length=100,
                        description='AI tool name: cline, claude-cli, aider, copilot, custom, etc.')
    content: str = Field(..., min_length=1, max_length=8000)
    role: str = Field(default='assistant', pattern='^(user|assistant|system)$')
    model: Optional[str] = Field(default=None, max_length=100)
    terminal_id: Optional[str] = Field(default=None, max_length=200)
    session_id: Optional[str] = Field(default=None, max_length=200)
    metadata: Optional[Dict[str, Any]] = Field(default=None)


class CaptureBatchRequest(BaseModel):
    captures: List[CaptureRequest] = Field(..., min_length=1, max_length=50)


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

    # Start embedded capture daemon (universal terminal capture on port 8098)
    try:
        from .memory_capture import start_embedded_capture_server
        start_embedded_capture_server()
    except Exception as exc:
        logger.warning('Capture daemon startup failed (non-fatal): %s', exc)

    # Start project feeder — autonomous feed from all CVG support engine directories
    try:
        from .project_feeder import start_project_feeder, get_feeder_stats
        start_project_feeder()
        fstats = get_feeder_stats()
        active = sum(1 for p in fstats.get('projects', []) if p.get('exists'))
        logger.info('Project feeder started — %d/%d project dirs active',
                    active, len(fstats.get('projects', [])))
    except Exception as exc:
        logger.warning('Project feeder startup failed (non-fatal): %s', exc)

    # Start history harvester — reads Cline, Claude Desktop, Copilot, Aider + hive nodes
    try:
        from .history_harvester import start_history_harvester
        start_history_harvester()
        logger.info('History harvester started (Cline/Claude/Copilot/Aider + Hive SSH)')
    except Exception as exc:
        logger.warning('History harvester startup failed (non-fatal): %s', exc)

    # Start hive preloader — deep one-time memory preload from all Queens/VMs/CTs/Forges
    try:
        from .hive_preloader import start_hive_preload_async, get_preload_status
        start_hive_preload_async(force=False)  # skips already-preloaded nodes
        pstatus = get_preload_status()
        logger.info('Hive preloader started — %d/%d nodes already preloaded, %d pending',
                    pstatus.get('preloaded', 0), pstatus.get('total_nodes', 0),
                    pstatus.get('pending', 0))
    except Exception as exc:
        logger.warning('Hive preloader startup failed (non-fatal): %s', exc)

    # Schedule memory consolidation every 15 minutes
    try:
        from .memory import get_memory as _get_memory
        def _run_consolidation():
            import asyncio
            try:
                mem = _get_memory()
                actions = mem.consolidate()
                logger.info('[scheduler] Consolidation: %s', actions)
            except Exception as exc:
                logger.warning('[scheduler] Consolidation failed: %s', exc)
        scheduler.add_job(_run_consolidation, 'interval', minutes=15, id='mem_consolidate')
    except Exception as exc:
        logger.warning('Consolidation scheduler failed to register: %s', exc)

    scheduler.add_job(refresh_context, 'interval', minutes=5, id='ctx_refresh')
    scheduler.start()
    logger.info('Context refresh scheduler started (interval: 5 min)')
    logger.info('Memory consolidation scheduler started (interval: 15 min)')
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
# Universal memory capture endpoints (v3)
# ---------------------------------------------------------------------------

def _is_localhost(request: Request) -> bool:
    host = request.client.host if request.client else ''
    return host in ('127.0.0.1', '::1', 'localhost')


@app.post('/api/memory/capture')
async def memory_capture(req: CaptureRequest, request: Request):
    '''
    Universal AI terminal capture endpoint.
    Accepts captures from any AI tool: Cline, Claude CLI, Aider, Copilot, etc.

    No auth required from localhost (127.0.0.1) — use X-CVG-Key from external IPs.
    Sources: cline, claude-cli, aider, copilot, llm-cli, sgpt, custom
    '''
    # Auth: localhost allowed without key; external IPs require CVG key
    if not _is_localhost(request):
        provided = request.headers.get('X-CVG-Key', '')
        if provided != CVG_INTERNAL_KEY:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail='X-CVG-Key required from non-localhost')

    mem = get_memory()
    cap_id = mem.capture.ingest(
        source=req.source,
        content=req.content,
        role=req.role,
        model=req.model,
        metadata=req.metadata,
        terminal_id=req.terminal_id,
        session_id=req.session_id,
    )
    logger.info('[capture] API: %s/%s from %s | %d chars', req.source, req.role,
                req.terminal_id or req.source, len(req.content))

    # Immediately feed high-value captures into working memory
    if req.role in ('assistant', 'system') and len(req.content) > 50:
        mem.working.add({
            'role': req.role,
            'content': req.content[:500],
            'source': req.source,
            'model': req.model,
        })

    return {
        'status': 'captured',
        'id': cap_id,
        'source': req.source,
        'unprocessed_total': mem.capture.unprocessed_count,
    }


@app.post('/api/memory/capture/batch')
async def memory_capture_batch(req: CaptureBatchRequest, request: Request):
    '''
    Batch capture from any AI tool. Accepts up to 50 captures in one request.
    '''
    if not _is_localhost(request):
        provided = request.headers.get('X-CVG-Key', '')
        if provided != CVG_INTERNAL_KEY:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail='X-CVG-Key required from non-localhost')

    mem = get_memory()
    ids = []
    for cap in req.captures:
        cap_id = mem.capture.ingest(
            source=cap.source,
            content=cap.content,
            role=cap.role,
            model=cap.model,
            metadata=cap.metadata,
            terminal_id=cap.terminal_id,
            session_id=cap.session_id,
        )
        ids.append(cap_id)

    return {
        'status': 'captured',
        'count': len(ids),
        'ids': ids,
        'unprocessed_total': mem.capture.unprocessed_count,
    }


@app.get('/api/memory/captures', dependencies=[Depends(require_cvg_key)])
async def memory_captures(limit: int = 50, source: Optional[str] = None):
    '''
    View recent AI terminal captures.
    Filter by source: ?source=cline, ?source=claude-cli, etc.
    '''
    mem = get_memory()
    captures = mem.capture.recent(n=min(limit, 200), source=source)
    return {
        'captures':    captures,
        'count':       len(captures),
        'sources':     mem.capture.sources(),
        'total':       mem.capture.total,
        'unprocessed': mem.capture.unprocessed_count,
        'timestamp':   datetime.now(timezone.utc).isoformat(),
    }


@app.post('/api/memory/consolidate', dependencies=[Depends(require_cvg_key)])
async def memory_consolidate():
    '''
    Trigger memory consolidation:
      - Process pending captures from external AI terminals → episodic + semantic
      - Promote repeated episodic patterns → semantic memory
      - Create associative links between co-active sources

    Called automatically every 15 minutes by the scheduler.
    Can also be triggered manually or by the capture daemon.
    '''
    import asyncio as _asyncio
    try:
        mem = get_memory()
        # Run blocking consolidation in thread pool to avoid blocking the event loop
        actions = await _asyncio.to_thread(mem.consolidate)
        return {
            'status':  'consolidated',
            'actions': actions,
            'stats':   mem.stats(),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        logger.error('Manual consolidation failed: %s', exc)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f'Consolidation failed: {exc}')


@app.get('/api/memory/context', dependencies=[Depends(require_cvg_key)])
async def memory_rich_context(query: str = ''):
    '''
    Return Neuron's full rich memory context, optionally filtered by query.
    Includes semantic facts, recent episodes, working state, and cross-terminal captures.
    Useful for debugging what Neuron "knows" right now.
    '''
    mem = get_memory()
    return {
        'context_text': mem.build_rich_context(query=query),
        'query':        query,
        'stats':        mem.stats(),
        'timestamp':    datetime.now(timezone.utc).isoformat(),
    }


@app.get('/api/memory/feeder', dependencies=[Depends(require_cvg_key)])
async def memory_feeder_stats():
    '''
    Return status and statistics for the autonomous project feeder.
    Shows which CVG support engine directories are being monitored and
    how many file/API/git captures have been fed into memory.
    '''
    try:
        from .project_feeder import get_feeder_stats
        stats = get_feeder_stats()
    except Exception:
        stats = {'running': False, 'error': 'project_feeder not loaded'}
    return {
        'feeder':    stats,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }


@app.post('/api/memory/feeder/scan', dependencies=[Depends(require_cvg_key)])
async def memory_feeder_scan():
    '''
    Trigger an immediate file/API/git scan cycle across all watched project directories.
    Equivalent to waiting for the next poll cycle, but on-demand.
    '''
    import asyncio as _asyncio
    try:
        from .project_feeder import get_project_feeder
        feeder = get_project_feeder()

        def _do_scan():
            results = {}
            for ps in feeder._projects:
                if not ps.exists():
                    results[ps.name] = {'exists': False}
                    continue
                changes = ps.scan_files()
                api_content = ps.check_api()
                git_log = ps.check_git()
                results[ps.name] = {
                    'exists':       True,
                    'files_changed': len(changes),
                    'api_updated':   api_content is not None,
                    'git_changed':   git_log is not None,
                }
            return results

        scan_results = await _asyncio.to_thread(_do_scan)
        return {
            'status':  'scanned',
            'results': scan_results,
            'stats':   feeder.stats,
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f'Feeder scan failed: {exc}')


@app.get('/api/memory/harvester', dependencies=[Depends(require_cvg_key)])
async def memory_harvester_stats():
    '''
    Return status and statistics for the AI history harvester.
    Shows which local AI tool history directories are being read (Cline, Claude Desktop,
    Copilot, Aider, LLM CLI) and which remote Hive nodes are being polled via SSH.
    '''
    try:
        from .history_harvester import get_harvester_stats
        stats = get_harvester_stats()
    except Exception:
        stats = {'running': False, 'error': 'history_harvester not loaded'}
    return {
        'harvester': stats,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }


@app.post('/api/memory/harvester/run', dependencies=[Depends(require_cvg_key)])
async def memory_harvester_run(include_hive: bool = False):
    '''
    Trigger an immediate harvest cycle.
    include_hive=true to also poll all remote Hive nodes via SSH (slower).
    '''
    import asyncio as _asyncio
    try:
        from .history_harvester import get_harvester

        def _do_harvest():
            h = get_harvester()
            local_n = h.harvest_local_once()
            hive_n  = h.harvest_hive_once() if include_hive else 0
            return {'local': local_n, 'hive': hive_n, 'total': local_n + hive_n}

        results = await _asyncio.to_thread(_do_harvest)
        return {
            'status':      'harvested',
            'results':     results,
            'include_hive': include_hive,
            'timestamp':   datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f'Harvest failed: {exc}')


@app.get('/api/memory/preload', dependencies=[Depends(require_cvg_key)])
async def memory_preload_status():
    '''
    Return preload status for all Hive-0 nodes.
    Shows which nodes have been memory-preloaded and how many items were loaded.
    '''
    try:
        from .hive_preloader import get_preload_status
        return {
            'preload':   get_preload_status(),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f'Preload status failed: {exc}')


@app.post('/api/memory/preload', dependencies=[Depends(require_cvg_key)])
async def memory_preload_run(force: bool = False, node_ip: Optional[str] = None):
    '''
    Trigger a hive memory preload.
    - force=true: re-preload even nodes already done
    - node_ip: preload a single node (e.g. ?node_ip=10.10.10.200)
    - default: preload all pending nodes (skips already-done)

    Runs async in background — returns immediately.
    '''
    import asyncio as _asyncio
    try:
        from .hive_preloader import preload_all_nodes, preload_node, _ALL_HIVE_NODES

        def _do_preload():
            if node_ip:
                if node_ip not in _ALL_HIVE_NODES:
                    return {'error': f'Unknown node IP: {node_ip}',
                            'known': list(_ALL_HIVE_NODES.keys())}
                n = preload_node(node_ip, _ALL_HIVE_NODES[node_ip], force=force)
                return {node_ip: n, 'total': n}
            else:
                results = preload_all_nodes(force=force)
                return {**results, 'total': sum(results.values())}

        # Run in background thread (non-blocking for the API)
        import threading as _threading
        t = _threading.Thread(target=_do_preload, daemon=True)
        t.start()

        return {
            'status':    'preload_started',
            'force':     force,
            'node_ip':   node_ip or 'all',
            'message':   'Preload running in background — check /api/memory/preload for status',
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                            detail=f'Preload failed: {exc}')


# ---------------------------------------------------------------------------
# Forge visibility and control endpoints
# ---------------------------------------------------------------------------

class ForgeCommandRequest(BaseModel):
    command:  str = Field(..., min_length=1, max_length=500,
                          description='Forge command: status, containers, restart <name>, ollama list, exec <cmd>...')
    target:   Optional[str] = Field(default=None, max_length=50,
                                     description='Target node: IP, name (vm-451), or role (primary)')
    node_ip:  Optional[str] = Field(default=None, max_length=20)


@app.get('/api/forge/status', dependencies=[Depends(require_cvg_key), Depends(rate_limit)])
async def forge_status(node: Optional[str] = None, force: bool = False):
    '''
    Get real-time status of all Forge/Queen/VM nodes, or a specific node.
    Collects: containers, memory/CPU/disk, Ollama models, processes, ports.

    ?node=10.10.10.200 or ?node=vm-451 for single node.
    ?force=true to bypass 60s cache.
    '''
    from .forge_manager import get_forge_manager
    fm = get_forge_manager()
    if node:
        status = await fm.get_node_status(node, force=force)
        if status is None:
            raise HTTPException(status_code=404, detail=f'Node not found: {node}')
        return {
            'node': node,
            'status': status,
            'summary': fm.nodes.get(status.get('ip', ''), next(iter(fm.nodes.values()))).format_summary(status),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
    else:
        all_status = await fm.get_all_status(force=force)
        return {
            'status':    all_status,
            'summary':   fm.format_forge_summary(all_status),
            'nodes':     list(all_status.keys()),
            'reachable': sum(1 for s in all_status.values() if s.get('reachable')),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }


@app.post('/api/forge/command', dependencies=[Depends(require_cvg_key), Depends(rate_limit)])
async def forge_command(req: ForgeCommandRequest):
    '''
    Execute a forge command on one or all nodes.

    Examples:
      {"command": "forge status"}
      {"command": "forge containers", "target": "vm-451"}
      {"command": "forge restart cvg-neuron-v1", "target": "10.10.10.200"}
      {"command": "forge ollama list"}
      {"command": "forge exec df -h", "target": "vm-454"}
      {"command": "forge compose status"}
      {"command": "forge logs cvg-neuron-v1"}
    '''
    from .forge_manager import get_forge_manager
    fm = get_forge_manager()
    target = req.target or req.node_ip
    result = await fm.dispatch_command(req.command, target=target)
    return {
        'command':   req.command,
        'target':    target or 'auto',
        'result':    result,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }


@app.post('/api/forge/docker/{action}', dependencies=[Depends(require_cvg_key), Depends(rate_limit)])
async def forge_docker(action: str, container: str, node: Optional[str] = None):
    '''
    Docker container action on a forge node.
    action: start | stop | restart | logs | inspect | stats | pull
    ?container=cvg-neuron-v1  (required for most actions)
    ?node=vm-451  (optional, defaults to primary)
    '''
    valid_actions = {'start', 'stop', 'restart', 'logs', 'inspect', 'stats', 'pull', 'ps'}
    if action not in valid_actions:
        raise HTTPException(400, detail=f'Invalid action: {action}. Valid: {valid_actions}')

    from .forge_manager import get_forge_manager
    fm = get_forge_manager()
    forge_node = fm._resolve_target(node) if node else fm._primary_forge()
    if not forge_node:
        raise HTTPException(404, detail=f'Forge node not found: {node}')

    result = await forge_node.docker_action(action, container)
    return {
        'action':    action,
        'container': container,
        'node':      forge_node.name,
        'result':    result,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }


@app.post('/api/forge/ollama/{action}', dependencies=[Depends(require_cvg_key), Depends(rate_limit)])
async def forge_ollama(action: str, model: Optional[str] = None,
                        node: Optional[str] = None):
    '''
    Ollama model management on a forge node.
    action: list | ps | pull | rm | show
    ?model=cvg-neuron  (required for pull/rm/show)
    ?node=10.10.10.200  (optional, defaults to Ollama host)
    '''
    valid_actions = {'list', 'ps', 'pull', 'rm', 'show', 'run'}
    if action not in valid_actions:
        raise HTTPException(400, detail=f'Invalid action: {action}. Valid: {valid_actions}')

    from .forge_manager import get_forge_manager
    fm = get_forge_manager()
    forge_node = fm._resolve_target(node) if node else fm._ollama_forge()
    if not forge_node:
        raise HTTPException(404, detail='No Ollama forge node found')

    result = await forge_node.ollama_action(action, model or '')
    return {
        'action':    action,
        'model':     model or '',
        'node':      forge_node.name,
        'result':    result,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }


@app.post('/api/forge/exec', dependencies=[Depends(require_cvg_key), Depends(rate_limit)])
async def forge_exec(command: str, node: Optional[str] = None):
    '''
    Execute an arbitrary shell command on a forge node.
    ?command=df+-h  (the command to run)
    ?node=vm-451   (optional, defaults to primary)

    WARNING: Full shell access. Requires CVG internal key auth.
    '''
    from .forge_manager import get_forge_manager
    fm = get_forge_manager()
    forge_node = fm._resolve_target(node) if node else fm._primary_forge()
    if not forge_node:
        raise HTTPException(404, detail=f'Forge node not found: {node}')

    logger.warning('[forge/exec] COMMAND on %s by API: %s', forge_node.name, command[:100])
    result = await forge_node.exec_command(command)
    return {
        'node':      forge_node.name,
        'ip':        forge_node.ip,
        'command':   command,
        'result':    result,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }


@app.get('/api/forge/nodes', dependencies=[Depends(require_cvg_key)])
async def forge_nodes():
    '''List all configured forge nodes with their roles and descriptions.'''
    from .forge_manager import FORGE_NODES, get_forge_manager
    fm = get_forge_manager()
    nodes = []
    for ip, cfg in FORGE_NODES.items():
        forge = fm.nodes.get(ip)
        cache = forge._cache if forge else None
        nodes.append({
            'ip':         ip,
            'name':       cfg['name'],
            'hostname':   cfg['hostname'],
            'type':       cfg['type'],
            'role':       cfg['role'],
            'desc':       cfg['desc'],
            'has_docker': cfg.get('has_docker', False),
            'has_ollama': cfg.get('has_ollama', False),
            'has_proxmox': cfg.get('has_proxmox', False),
            'last_seen':  cache.get('timestamp') if cache else None,
            'reachable':  cache.get('reachable') if cache else None,
        })
    return {'nodes': nodes, 'count': len(nodes),
            'timestamp': datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------------------
# DNS visibility and migration endpoints
# ---------------------------------------------------------------------------

class DnsCommandRequest(BaseModel):
    command: str = Field(..., min_length=1, max_length=500,
                         description='DNS command: status, migrate, records, zone, help, or natural language')


@app.get('/api/dns/status', dependencies=[Depends(require_cvg_key), Depends(rate_limit)])
async def dns_status():
    '''
    Real-time DNS status for cleargeo.tech.
    Shows current nameservers, migration state, A/MX records, and DNS Engine health.
    '''
    from .dns_manager import get_dns_status, check_subdomain_resolution, dns_health_check
    status = await get_dns_status()
    subdomains = await check_subdomain_resolution()
    health = await dns_health_check()
    return {
        'domain':              status.domain,
        'nameservers':         status.ns_records,
        'a_record':            status.a_record,
        'mx_records':          status.mx_records,
        'migration_complete':  status.migration_complete,
        'using_hostgator':     status.using_hostgator,
        'using_selfhosted':    status.using_selfhosted,
        'dns_engine_online':   status.bind9_engine_online,
        'subdomains':          subdomains,
        'health':              health,
        'playbook':            'docs/DNS_MIGRATION_PLAYBOOK.md',
        'timestamp':           datetime.now(timezone.utc).isoformat(),
    }


@app.post('/api/dns/command', dependencies=[Depends(require_cvg_key), Depends(rate_limit)])
async def dns_command(req: DnsCommandRequest):
    '''
    Execute a DNS management command.

    Commands:
      status    — current nameservers, A record, MX, migration state
      migrate   — migration checklist and remaining steps
      records   — all zone records (from DNS Engine if online)
      zone      — BIND9 zone management info
      help      — full playbook summary and audit commands

    Or ask in natural language:
      "what DNS nameservers are we using?"
      "how do I complete the HostGator migration?"
      "show me all DNS records"
    '''
    from .dns_manager import handle_dns_command as _dns_cmd
    result = await _dns_cmd(req.command)
    return {
        'command':   req.command,
        'result':    result,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }


@app.get('/api/dns/health', dependencies=[Depends(require_cvg_key)])
async def dns_health():
    '''
    Lightweight DNS health check — nameserver state andd DNS Engine reachability.
    '''
    from .dns_manager import dns_health_check
    result = await dns_health_check()
    return {**result, 'timestamp': datetime.now(timezone.utc).isoformat()}


@app.get('/api/dns/records', dependencies=[Depends(require_cvg_key), Depends(rate_limit)])
async def dns_records(zone: str = 'cleargeo.tech'):
    '''
    Fetch DNS records for a zone from the CVG DNS Engine (port 8810).
    Returns empty list if DNS Engine is not yet online.
    '''
    from .dns_manager import get_zone_records, _engine_health
    records = await get_zone_records(zone)
    engine_up = await _engine_health()
    return {
        'zone':         zone,
        'records':      [{'name': r.name, 'type': r.rtype, 'value': r.value, 'ttl': r.ttl}
                         for r in records],
        'count':        len(records),
        'engine_online': engine_up,
        'engine_url':   'http://10.10.10.200:8810',
        'timestamp':    datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Static UI
# ---------------------------------------------------------------------------

try:
    app.mount('/', StaticFiles(directory='static', html=True), name='static')
except Exception:
    logger.warning('Static files not found at ./static -- UI unavailable')
