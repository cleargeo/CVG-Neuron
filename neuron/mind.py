# CVG Neuron -- Cognitive Engine v2
# (c) Clearview Geographic, LLC -- Proprietary and PRIVATE
#
# v2 improvements:
#   - Proper async 5-step RECALL->ASSESS->REASON->VERIFY->RESPOND pipeline
#   - Structured reasoning with explicit step tracking and timing
#   - Confidence scoring (0.0-1.0 float) based on context quality
#   - Fallback handling when Ollama is unavailable
#   - Streaming support via async generator
#   - Improved error handling and logging
#   - Conversation history trimming (keep last 20 turns)

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import AsyncGenerator, Dict, List, Optional

from .identity import build_neuron_system_prompt, get_identity_card, NEURON_NAME, NEURON_VERSION
from .memory import get_memory
from .cluster import get_cluster
from .ollama_client import get_ollama_client
from .context_builder import get_cached_context, build_context_string
from .forge_manager import is_forge_query, extract_forge_command, get_forge_manager, get_forge_context
from .dns_manager import is_dns_query, handle_dns_command, build_dns_context

logger = logging.getLogger('neuron.mind')

# Maximum conversation turns to keep in history to prevent context overflow
MAX_CONVERSATION_HISTORY = 20

# Minimum confidence threshold below which we flag a response
MIN_CONFIDENCE_THRESHOLD = 0.3

# Markers for extracting confidence/source from response text
_CONFIDENCE_RE = re.compile(r'\[(CONFIDENT|PROBABLE|UNCERTAIN|UNKNOWN)\]', re.IGNORECASE)
_SOURCE_RE = re.compile(r'\[(MEMORY|LIVE-DATA|INFERENCE|KNOWLEDGE-BASE)\]', re.IGNORECASE)

# Fallback message when Ollama is unavailable
_OLLAMA_FALLBACK = (
    '[UNCERTAIN] My inference substrate (Ollama) is currently unavailable. '
    'I can report from memory:\n\n{memory_summary}\n\n'
    'Please check Ollama status at http://10.10.10.200:11434. '
    'I am recording this outage in my episodic memory.'
)

def _extract_markers(text: str) -> dict:
    confidence_matches = _CONFIDENCE_RE.findall(text)
    source_matches = _SOURCE_RE.findall(text)
    return {
        'confidence_label': confidence_matches[0].upper() if confidence_matches else 'PROBABLE',
        'sources': list({m.upper() for m in source_matches}),
    }


def _label_to_score(label: str) -> float:
    return {'CONFIDENT': 0.9, 'PROBABLE': 0.65, 'UNCERTAIN': 0.35, 'UNKNOWN': 0.1}.get(label.upper(), 0.5)


def _trim_history(history: Optional[List[dict]], max_turns: int = MAX_CONVERSATION_HISTORY) -> List[dict]:
    if not history:
        return []
    # Each turn = one item; keep last max_turns items
    trimmed = history[-max_turns:]
    if len(history) > max_turns:
        logger.debug('[mind] Trimmed conversation history %d->%d turns', len(history), len(trimmed))
    return trimmed


def _step_log(step: str, detail: str = '', elapsed_ms: float = 0.0) -> None:
    if elapsed_ms:
        logger.debug('[COGNITION:%s] %s (%.1fms)', step, detail, elapsed_ms)
    else:
        logger.debug('[COGNITION:%s] %s', step, detail)


class NeuronMind:
    '''
    The cognitive engine of CVG Neuron.

    Every public method represents a cognitive act:
      think()    -- full 5-step RECALL->ASSESS->REASON->VERIFY->RESPOND pipeline
      think_stream() -- streaming version via async generator
      reflect()  -- meta-cognition: Neuron evaluates its own state
      learn()    -- explicitly inject a fact into semantic memory
      recall()   -- query memory without triggering inference
    '''

    def __init__(self) -> None:
        self.memory   = get_memory()
        self.cluster  = get_cluster()
        self.ollama   = get_ollama_client()
        self._boot_time = datetime.now(timezone.utc).isoformat()
        self._interaction_count: int = 0
        self._ollama_failures: int = 0
        logger.info('%s v%s Cognitive Engine initialized at %s',
                    NEURON_NAME, NEURON_VERSION, self._boot_time)

    # ==========================================================================
    # STEP 1 -- RECALL
    # ==========================================================================

    def _recall(self, message: str) -> dict:
        t0 = time.monotonic()
        _step_log('RECALL', f'query={repr(message[:60])}')

        mem = self.memory
        working_recent  = mem.working.recent(15)         # increased from 10
        episodic_recent = mem.episodic.recent(8)         # increased from 5
        semantic_facts  = mem.semantic.search(message, limit=12)  # increased from 8
        procedures      = mem.procedural.match(message, limit=5)  # increased from 3
        # New in v3: associative links and recent cross-terminal captures
        associations    = mem.associative.recall_links(message, min_strength=0.4)
        ext_captures    = [c for c in mem.capture.recent(10)
                           if c.get('source') not in ('neuron', '')][:5]

        elapsed = (time.monotonic() - t0) * 1000
        _step_log('RECALL',
                  f'semantic={len(semantic_facts)} episodic={len(episodic_recent)} '
                  f'captures={len(ext_captures)}', elapsed)

        return {
            'working':      working_recent,
            'episodic':     episodic_recent,
            'semantic':     semantic_facts,
            'procedural':   procedures,
            'associations': associations,
            'ext_captures': ext_captures,
            'elapsed_ms':   elapsed,
        }

    # ==========================================================================
    # STEP 2 -- ASSESS
    # ==========================================================================

    def _assess(self, message: str, recalled: dict) -> dict:
        t0 = time.monotonic()
        _step_log('ASSESS')

        semantic_hits  = recalled.get('semantic', [])
        episodic_hits  = recalled.get('episodic', [])
        procedural_hit = recalled.get('procedural', [])

        # Score context quality: more relevant memory = higher base confidence
        semantic_score  = min(1.0, len(semantic_hits) * 0.12)
        episodic_score  = min(0.3, len(episodic_hits) * 0.06)
        base_confidence = min(0.85, 0.4 + semantic_score + episodic_score)

        # Keywords that suggest live data is valuable
        live_triggers = [
            'status', 'health', 'running', 'docker', 'container',
            'dns', 'git', 'deploy', 'cluster', 'node', 'current',
            'latest', 'now', 'today', 'active', 'error', 'fail',
            'alert', 'warning', 'critical', 'online', 'offline',
        ]
        msg_lower = message.lower()
        wants_live = any(t in msg_lower for t in live_triggers)
        # Forge detection
        wants_forge = is_forge_query(message)
        forge_cmd   = extract_forge_command(message) if wants_forge else None

        # DNS detection
        wants_dns = is_dns_query(message)

        elapsed = (time.monotonic() - t0) * 1000
        _step_log('ASSESS',
                  f'base_conf={base_confidence:.2f} wants_live={wants_live} '
                  f'wants_dns={wants_dns} semantic_hits={len(semantic_hits)}', elapsed)

        return {
            'has_memory':       len(semantic_hits) > 0 or len(episodic_hits) > 0,
            'memory_facts':     len(semantic_hits),
            'wants_live_data':  wants_live or wants_forge or wants_dns,
            'wants_forge':      wants_forge,
            'forge_command':    forge_cmd,
            'wants_dns':        wants_dns,
            'has_procedure':    len(procedural_hit) > 0,
            'procedure':        procedural_hit[0] if procedural_hit else None,
            'base_confidence':  base_confidence,
            'elapsed_ms':       elapsed,
        }

    # ==========================================================================
    # STEP 3 -- REASON  (inference via Ollama substrate)
    # ==========================================================================

    async def _reason(
        self,
        message: str,
        recalled: dict,
        assessment: dict,
        context_type: str,
        conversation_history: Optional[List[dict]] = None,
    ) -> tuple:
        '''
        Core inference step. Returns (raw_response, confidence_delta).
        confidence_delta is added to base_confidence from ASSESS step.
        '''
        t0 = time.monotonic()
        _step_log('REASON', f'wants_live={assessment["wants_live_data"]} has_memory={assessment["has_memory"]}')

        # Get cluster state for context
        cluster_state = self.cluster.get_cluster_state_for_neuron()

        # Get live cluster stats for dynamic capability injection
        cluster_stats = self.cluster.get_stats()

        # Memory summary for system prompt
        memory_summary = self._build_memory_summary(recalled)

        # Knowledge snippet (top semantic facts)
        knowledge_snippet = '\n'.join(
            f'- {f.get("key", "")}: {f.get("value", "")}'
            for f in recalled.get('semantic', [])[:6]
        )

        # Build Neuron identity-aware system prompt with dynamic capabilities
        system_prompt = build_neuron_system_prompt(
            memory_summary=memory_summary,
            knowledge_snippet=knowledge_snippet,
            cluster_state=cluster_state,
            live_cluster_stats=cluster_stats,
        )

        # Live context augmentation
        live_context_block = ''
        confidence_delta = 0.0
        if assessment['wants_live_data']:
            try:
                ctx = await get_cached_context()
                live_context_block = build_context_string(ctx, context_type)
                if live_context_block and len(live_context_block) > 50:
                    confidence_delta += 0.1  # live data boosts confidence
            except Exception as exc:
                logger.warning('Could not fetch live context: %s', exc)
                live_context_block = 'Live engine data temporarily unavailable.'

        # Forge context injection — real-time status from all Forge/Queen nodes
        forge_context_block = ''
        if assessment.get('wants_forge'):
            try:
                forge_context_block = await get_forge_context()
                if forge_context_block and len(forge_context_block) > 30:
                    confidence_delta += 0.15  # forge data is highly relevant
                    logger.debug('[REASON] Forge context injected (%d chars)', len(forge_context_block))

                # If a specific forge command was extracted, execute it and prepend result
                forge_cmd = assessment.get('forge_command')
                if forge_cmd:
                    try:
                        fm = get_forge_manager()
                        forge_result = await fm.dispatch_command(forge_cmd)
                        result_text = forge_result.get('summary', '')
                        if not result_text:
                            result_text = (forge_result.get('stdout', '') or
                                           str(forge_result.get('status', '')))[:2000]
                        if result_text:
                            forge_context_block = (
                                f'[FORGE COMMAND: {forge_cmd}]\n{result_text}\n\n'
                                + forge_context_block
                            )
                    except Exception as fexc:
                        logger.debug('Forge command dispatch failed: %s', fexc)
            except Exception as exc:
                logger.warning('Could not fetch forge context: %s', exc)

        # DNS context injection — real-time DNS/migration status
        dns_context_block = ''
        if assessment.get('wants_dns'):
            try:
                dns_context_block = await build_dns_context()
                if dns_context_block and len(dns_context_block) > 30:
                    confidence_delta += 0.15
                    logger.debug('[REASON] DNS context injected (%d chars)', len(dns_context_block))
                # For direct DNS commands (status/migrate/records), also dispatch and prepend result
                dns_result = await handle_dns_command(message)
                if dns_result:
                    dns_context_block = (
                        f'[DNS COMMAND RESULT]\n{dns_result}\n\n'
                        + dns_context_block
                    )
            except Exception as exc:
                logger.warning('Could not fetch DNS context: %s', exc)

        # Compose final user message
        user_message = message
        if live_context_block:
            user_message = f'{message}\n\n[LIVE PLATFORM DATA]\n{live_context_block}'
        if forge_context_block:
            user_message = f'{user_message}\n\n[FORGE STATUS]\n{forge_context_block}'
        if dns_context_block:
            user_message = f'{user_message}\n\n[DNS STATUS]\n{dns_context_block}'

        # Trim and build message history
        history = _trim_history(conversation_history, MAX_CONVERSATION_HISTORY)
        messages = []
        for turn in history[-10:]:  # cap to last 10 turns for prompt
            messages.append({'role': turn.get('role', 'user'), 'content': turn.get('content', '')})
        messages.append({'role': 'user', 'content': user_message})

        # Log cluster routing intent
        best_node = self.cluster.get_best_inference_node(prefer_heavy=True)
        if best_node and not best_node.get('is_primary'):
            logger.info('Cluster routing intent: %s (%s)', best_node.get('name'), best_node.get('ip'))

        # Check Ollama availability before calling
        ollama_ok = await self.ollama.health()
        if not ollama_ok:
            self._ollama_failures += 1
            elapsed = (time.monotonic() - t0) * 1000
            _step_log('REASON', f'Ollama UNAVAILABLE (failure #{self._ollama_failures})', elapsed)
            # Return fallback response from memory
            fallback = _OLLAMA_FALLBACK.format(memory_summary=memory_summary or 'No memory context available.')
            return fallback, -0.5  # negative delta = low confidence

        self._ollama_failures = 0  # reset on success

        # Call Ollama as cognitive SUBSTRATE
        response = await self.ollama.chat(
            messages=messages,
            system=system_prompt,
        )

        elapsed = (time.monotonic() - t0) * 1000
        _step_log('REASON', f'response_len={len(response)} elapsed={elapsed:.0f}ms', elapsed)
        return response, confidence_delta

    # ==========================================================================
    # STEP 4 -- VERIFY
    # ==========================================================================

    def _verify(self, response: str) -> dict:
        t0 = time.monotonic()
        _step_log('VERIFY')
        flags = []

        # Hard-coded identity integrity checks
        lower = response.lower()
        if 'public model' in lower or 'available on ollama' in lower:
            flags.append('IDENTITY_VIOLATION: response implies public availability')
        if 'open source' in lower and 'neuron' in lower:
            flags.append('IDENTITY_VIOLATION: response implies open-source status')

        # Check semantic memory for contradictions
        for fact in self.memory.semantic.search('neuron identity', limit=5):
            value = str(fact.get('value', '')).lower()
            if 'private' in value and 'public' in lower and 'neuron' in lower:
                flags.append(f'CONTRADICTION with {fact.get("key")}: {fact.get("value")}')

        elapsed = (time.monotonic() - t0) * 1000
        _step_log('VERIFY', f'flags={len(flags)}', elapsed)
        return {'verified': len(flags) == 0, 'flags': flags, 'elapsed_ms': elapsed}

    # ==========================================================================
    # STEP 5 -- RESPOND  (record + package)
    # ==========================================================================

    def _respond(
        self,
        message: str,
        response: str,
        verification: dict,
        recalled: dict,
        assessment: dict,
        elapsed_ms: float,
        context_type: str,
        confidence_delta: float = 0.0,
    ) -> dict:
        t0 = time.monotonic()
        markers = _extract_markers(response)
        self._interaction_count += 1

        # Calculate final confidence score (0.0-1.0)
        base = assessment.get('base_confidence', 0.5)
        # Adjust based on response markers
        label_score = _label_to_score(markers['confidence_label'])
        final_confidence = min(1.0, max(0.0, (base + label_score) / 2 + confidence_delta))
        if not verification['verified']:
            final_confidence = min(final_confidence, 0.4)  # cap if verification failed

        # Add to working memory
        self.memory.working.add({
            'role': 'user',
            'content': message,
        })
        self.memory.working.add({
            'role': 'neuron',
            'content': response,
            'confidence': final_confidence,
        })

        # Record as episode
        self.memory.episodic.record(
            event_type='interaction',
            summary=f'Q: {message[:100]} | A: {response[:200]}',
            metadata={
                'context_type': context_type,
                'confidence': markers['confidence_label'],
                'confidence_score': final_confidence,
                'sources': markers['sources'],
                'elapsed_ms': elapsed_ms,
                'verified': verification['verified'],
                'flags': verification['flags'],
                'model': self.ollama.default_model,
            },
        )

        # Auto-learn factual statements from response
        for line in response.split('\n'):
            line = line.strip()
            if 20 < len(line) < 200:
                if line.startswith('CVG ') or 'Neuron is' in line:
                    self.memory.semantic.add_fact(
                        key=f'auto_learned.{int(time.time())}',
                        value=line,
                        confidence=0.55,
                        source='neuron_inference',
                    )

        # Persist memory
        try:
            self.memory.persist()
        except Exception as exc:
            logger.warning('Memory persist failed: %s', exc)

        respond_ms = (time.monotonic() - t0) * 1000
        _step_log('RESPOND',
                  f'confidence={final_confidence:.2f} verified={verification["verified"]} '
                  f'total_elapsed={elapsed_ms:.0f}ms', respond_ms)

        return {
            'response':            response,
            'confidence':          markers['confidence_label'],
            'confidence_score':    round(final_confidence, 3),
            'sources':             markers['sources'],
            'verified':            verification['verified'],
            'verification_flags':  verification['flags'],
            'elapsed_ms':          elapsed_ms,
            'interaction_id':      self._interaction_count,
            'context_type':        context_type,
            'memory_facts_used':   len(recalled.get('semantic', [])),
            'model':               self.ollama.default_model,
        }

    # ==========================================================================
    # PUBLIC: think() -- full 5-step cognitive protocol
    # ==========================================================================

    async def think(
        self,
        message: str,
        context_type: str = 'general',
        conversation_history: Optional[List[dict]] = None,
    ) -> dict:
        '''
        Primary cognitive interface. Runs RECALL->ASSESS->REASON->VERIFY->RESPOND.

        Args:
            message:              The input query or instruction.
            context_type:         One of: general, infrastructure, git, dns, security, synthesis.
            conversation_history: Prior chat turns [{"role":"user","content":"..."}].

        Returns:
            dict with keys: response, confidence, confidence_score, sources, verified,
                            verification_flags, elapsed_ms, interaction_id, context_type,
                            memory_facts_used, model.
        '''
        start = time.monotonic()
        logger.info('THINK: context_type=%s | msg=%.80r', context_type, message)

        try:
            # 1. RECALL
            recalled = self._recall(message)

            # 2. ASSESS
            assessment = self._assess(message, recalled)

            # 3. REASON
            raw_response, confidence_delta = await self._reason(
                message, recalled, assessment, context_type, conversation_history
            )

            # 4. VERIFY
            verification = self._verify(raw_response)

            # 5. RESPOND
            elapsed_ms = (time.monotonic() - start) * 1000
            return self._respond(
                message, raw_response, verification, recalled, assessment,
                elapsed_ms, context_type, confidence_delta,
            )

        except Exception as exc:
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.error('COGNITIVE FAILURE: %s', exc, exc_info=True)
            self.memory.episodic.record(
                event_type='cognitive_failure',
                summary=str(exc),
                metadata={'message': message[:200], 'elapsed_ms': elapsed_ms},
            )
            return {
                'response': (
                    f'[UNCERTAIN] I encountered a cognitive disruption processing your request. '
                    f'Error: {str(exc)[:200]}'
                ),
                'confidence':         'UNCERTAIN',
                'confidence_score':   0.1,
                'sources':            ['INTERNAL'],
                'verified':           False,
                'verification_flags': [f'COGNITIVE_FAILURE: {str(exc)[:100]}'],
                'elapsed_ms':         elapsed_ms,
                'interaction_id':     self._interaction_count,
                'context_type':       context_type,
                'memory_facts_used':  0,
                'model':              self.ollama.default_model,
                'error':              str(exc),
            }

    # ==========================================================================
    # PUBLIC: think_stream() -- streaming async generator
    # ==========================================================================

    async def think_stream(
        self,
        message: str,
        context_type: str = 'general',
        conversation_history: Optional[List[dict]] = None,
    ) -> AsyncGenerator[dict, None]:
        '''
        Streaming cognitive interface. Yields SSE-compatible dicts.
        Runs full RECALL->ASSESS steps synchronously, then streams REASON.
        '''
        start = time.monotonic()

        # RECALL + ASSESS (non-streaming)
        recalled = self._recall(message)
        assessment = self._assess(message, recalled)

        yield {'event': 'step', 'step': 'recall', 'data': {'facts': len(recalled.get('semantic', []))}}
        yield {'event': 'step', 'step': 'assess', 'data': {'wants_live': assessment['wants_live_data'],
                                                             'base_confidence': assessment['base_confidence']}}

        # Check Ollama first
        ollama_ok = await self.ollama.health()
        if not ollama_ok:
            memory_summary = self._build_memory_summary(recalled)
            fallback = _OLLAMA_FALLBACK.format(memory_summary=memory_summary or 'No memory context.')
            yield {'event': 'chunk', 'data': fallback}
            yield {'event': 'done', 'data': {'elapsed_ms': (time.monotonic() - start) * 1000,
                                              'confidence_score': 0.1, 'ollama_available': False}}
            return

        # Build prompt components
        cluster_state  = self.cluster.get_cluster_state_for_neuron()
        cluster_stats  = self.cluster.get_stats()
        memory_summary = self._build_memory_summary(recalled)
        knowledge_snippet = '\n'.join(
            f'- {f.get("key", "")}: {f.get("value", "")}'
            for f in recalled.get('semantic', [])[:6]
        )
        system_prompt = build_neuron_system_prompt(
            memory_summary=memory_summary,
            knowledge_snippet=knowledge_snippet,
            cluster_state=cluster_state,
            live_cluster_stats=cluster_stats,
        )

        # Live context if needed
        user_message = message
        if assessment['wants_live_data']:
            try:
                ctx = await get_cached_context()
                live_block = build_context_string(ctx, context_type)
                if live_block:
                    user_message = f'{message}\n\n[LIVE PLATFORM DATA]\n{live_block}'
                    yield {'event': 'step', 'step': 'live_context', 'data': {'chars': len(live_block)}}
            except Exception as exc:
                logger.warning('Stream live context failed: %s', exc)

        history = _trim_history(conversation_history, MAX_CONVERSATION_HISTORY)
        messages = []
        for turn in history[-10:]:
            messages.append({'role': turn.get('role', 'user'), 'content': turn.get('content', '')})
        messages.append({'role': 'user', 'content': user_message})

        yield {'event': 'step', 'step': 'reason_start', 'data': {'turns': len(messages)}}

        # Stream the response -- Ollama non-streaming for now, yield as single chunk
        # (Full streaming requires ollama_client streaming support)
        try:
            raw_response = await self.ollama.chat(messages=messages, system=system_prompt)
            verification = self._verify(raw_response)
            elapsed_ms = (time.monotonic() - start) * 1000
            result = self._respond(
                message, raw_response, verification, recalled, assessment,
                elapsed_ms, context_type,
            )
            yield {'event': 'chunk', 'data': raw_response}
            yield {'event': 'done', 'data': {
                'elapsed_ms': elapsed_ms,
                'confidence_score': result['confidence_score'],
                'confidence': result['confidence'],
                'verified': result['verified'],
                'sources': result['sources'],
                'interaction_id': result['interaction_id'],
                'ollama_available': True,
            }}
        except Exception as exc:
            yield {'event': 'error', 'data': {'error': str(exc)}}

    # ==========================================================================
    # PUBLIC: reflect() -- meta-cognition
    # ==========================================================================

    async def reflect(self) -> dict:
        _step_log('REFLECT')
        mem_stats  = self.memory.stats()
        scan       = await self.cluster.scan_cluster()
        ollama_ok  = await self.ollama.health()

        reflection_prompt = (
            'Perform a brief self-assessment. Report on:\n'
            '1. Your memory health (working/episodic/semantic/procedural counts below)\n'
            '2. Your cluster connectivity\n'
            '3. Your cognitive readiness\n'
            '4. Any concerns about your operational state\n'
            'Be concise and factual. Use [CONFIDENT] or [UNCERTAIN] markers.'
        )
        result = await self.think(reflection_prompt, context_type='general')

        return {
            'self_assessment':             result['response'],
            'memory_stats':                mem_stats,
            'cluster_online_nodes':        scan.get('online_nodes', 0),
            'cluster_total_nodes':         scan.get('total_nodes', len(self.cluster.nodes)),
            'ollama_substrate_healthy':     ollama_ok,
            'ollama_failures_since_start':  self._ollama_failures,
            'interaction_count':            self._interaction_count,
            'boot_time':                    self._boot_time,
            'confidence':                   result.get('confidence', 'UNCERTAIN'),
            'confidence_score':             result.get('confidence_score', 0.5),
        }

    # ==========================================================================
    # PUBLIC: learn() -- explicit fact injection
    # ==========================================================================

    def learn(self, key: str, value: str, source: str = 'external_feed',
              confidence: float = 0.9) -> dict:
        _step_log('LEARN', f'key={key}')
        self.memory.semantic.add_fact(key=key, value=value, confidence=confidence, source=source)
        self.memory.episodic.record(
            event_type='learned_fact',
            summary=f'Learned: {key} = {str(value)[:100]}',
            metadata={'source': source, 'confidence': confidence},
        )
        try:
            self.memory.persist()
        except Exception as exc:
            logger.warning('Memory persist after learn failed: %s', exc)
        return {'status': 'learned', 'key': key, 'source': source, 'confidence': confidence}

    # ==========================================================================
    # PUBLIC: recall() -- query memory without inference
    # ==========================================================================

    def recall(self, query: str, limit: int = 10) -> dict:
        _step_log('RECALL_QUERY', query[:60])
        return {
            'semantic': self.memory.semantic.search(query, limit=limit),
            'episodic': self.memory.episodic.recent(min(limit, 20)),
            'working':  self.memory.working.recent(min(limit, 20)),
            'stats':    self.memory.stats(),
        }

    # ==========================================================================
    # INTERNAL helpers
    # ==========================================================================

    def _build_memory_summary(self, recalled: dict) -> str:
        lines = []
        semantic = recalled.get('semantic', [])
        if semantic:
            lines.append('Known facts:')
            for f in semantic[:8]:
                lines.append(f'  - {f.get("key", "?")}: {str(f.get("value", ""))[:120]}')
        episodic = recalled.get('episodic', [])
        if episodic:
            lines.append('Recent episodes:')
            for ep in episodic[:5]:
                src = ep.get('source', '')
                src_tag = f'[{src}] ' if src and src not in ('neuron', '') else ''
                lines.append(f'  - {src_tag}{ep.get("summary", "")[:140]}')
        working = recalled.get('working', [])
        if working:
            lines.append('Working context (recent turns):')
            for w in working[-6:]:
                role = w.get('role', '?')
                content = str(w.get('content', ''))[:120]
                src = w.get('source', '')
                src_tag = f'[{src}]' if src and src not in ('neuron', '') else ''
                lines.append(f'  {src_tag}[{role}] {content}')
        # New v3: cross-terminal captures
        ext_captures = recalled.get('ext_captures', [])
        if ext_captures:
            lines.append('Cross-terminal AI activity (other tools on this machine):')
            for cap in ext_captures[:3]:
                src  = cap.get('source', '?')
                role = cap.get('role', '?')
                content = cap.get('content', '')[:120]
                ts = cap.get('timestamp', '')[:16]
                lines.append(f'  [{ts}][{src}/{role}] {content}')
        # New v3: associative links
        associations = recalled.get('associations', [])
        if associations:
            lines.append('Related concept links:')
            for a in associations[:3]:
                lines.append(f'  {a["concept_a"]} --[{a["relation"]}]--> {a["concept_b"]}')
        return '\n'.join(lines) if lines else 'No prior memory for this query.'


# ==========================================================================
# Module singleton
# ==========================================================================

_mind_instance: Optional[NeuronMind] = None


def get_mind() -> NeuronMind:
    global _mind_instance
    if _mind_instance is None:
        _mind_instance = NeuronMind()
    return _mind_instance
