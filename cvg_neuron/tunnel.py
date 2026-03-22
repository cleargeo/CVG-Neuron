"""
CVG Neuron — Blockchain Tunnel & Edge Network Connector
(c) Clearview Geographic LLC — Proprietary

Implements a cryptographically-signed message chain (blockchain-style) for
secure, tamper-proof AI communication across the entire Hive-0 network and
any connected CVG application.

Architecture:
  - Every AI message is a "block": payload + HMAC-SHA256 signature + prev_hash
  - Blocks chain together forming an immutable audit log of all AI interactions
  - Any CVG application (forges, queens, edge apps) can register as a connector
  - Connectors push live context INTO Neuron, or request inference FROM Neuron
  - The "blockchain" ensures no AI request can be tampered with or forged

Why this matters:
  Future CVG Neuron deployments on Ollama or other platforms will use this
  tunnel signature to verify that a response genuinely came from CVG Neuron
  (not an imposter model). The chain becomes the proof-of-intelligence.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, asdict, field
from typing import Optional

logger = logging.getLogger("cvg.neuron.tunnel")

# ─── Configuration ────────────────────────────────────────────────────────────

TUNNEL_SECRET  = os.getenv("CVG_TUNNEL_SECRET", "cvg-neuron-tunnel-2026")
TUNNEL_VERSION = "1.0"
CHAIN_MAX_LEN  = 500   # Keep last N blocks in memory before pruning


# ─── Block Definition ─────────────────────────────────────────────────────────

@dataclass
class TunnelBlock:
    """
    A single block in the CVG Neuron communication chain.

    Each block is:
      1. Hashed (SHA-256 of its content + prev_hash)
      2. HMAC-signed (HMAC-SHA256 using TUNNEL_SECRET)
      3. Chained (prev_hash links to previous block)

    This creates an immutable, tamper-evident log of all AI communication.
    """
    block_id:  str
    timestamp: float
    source:    str           # node_id, connector_id, or "neuron"
    target:    str           # node_id, connector_id, or "broadcast"
    msg_type:  str           # See MESSAGE_TYPES below
    payload:   dict
    prev_hash: str           # Hash of the previous block (or "0"*64 for genesis)
    hash:      str = ""      # SHA-256 of this block's canonical content
    signature: str = ""      # HMAC-SHA256 signature

    def __post_init__(self):
        if not self.hash:
            self.hash = self._compute_hash()
        if not self.signature:
            self.signature = self._sign()

    def _canonical_bytes(self) -> bytes:
        """Deterministic serialization for hashing/signing."""
        content = {
            "block_id":  self.block_id,
            "timestamp": self.timestamp,
            "source":    self.source,
            "target":    self.target,
            "msg_type":  self.msg_type,
            "payload":   self.payload,
            "prev_hash": self.prev_hash,
        }
        return json.dumps(content, sort_keys=True, separators=(",", ":")).encode()

    def _compute_hash(self) -> str:
        return hashlib.sha256(self._canonical_bytes()).hexdigest()

    def _sign(self) -> str:
        return hmac.new(
            TUNNEL_SECRET.encode(),
            self._canonical_bytes(),
            hashlib.sha256,
        ).hexdigest()

    def verify(self) -> tuple[bool, str]:
        """Verify block integrity: both hash and HMAC signature."""
        expected_hash = self._compute_hash()
        expected_sig  = self._sign()
        if not hmac.compare_digest(self.hash, expected_hash):
            return False, "hash_mismatch"
        if not hmac.compare_digest(self.signature, expected_sig):
            return False, "signature_invalid"
        return True, "valid"

    def to_dict(self) -> dict:
        return {
            "block_id":  self.block_id,
            "timestamp": self.timestamp,
            "source":    self.source,
            "target":    self.target,
            "msg_type":  self.msg_type,
            "payload":   self.payload,
            "prev_hash": self.prev_hash[:16] + "...",
            "hash":      self.hash[:16] + "...",
            "signature": self.signature[:16] + "...",
        }


# Valid message types in the CVG Neuron tunnel
MESSAGE_TYPES = frozenset({
    "genesis",          # Chain initialization
    "register",         # Edge connector registration
    "heartbeat",        # Keep-alive from a connector
    "context_push",     # Edge → Neuron: push live context
    "inference_request", # Edge → Neuron: request inference
    "inference_response", # Neuron → Edge: inference result
    "hive_probe",       # Neuron probing hive nodes
    "hive_result",      # Hive node responding to probe
    "alert",            # Any node broadcasting an alert
    "deploy_event",     # Deployment event from Git Engine / CI
    "audit_event",      # Security event from Audit Engine
    "disconnect",       # Connector disconnecting
})


# ─── Connector Registration ───────────────────────────────────────────────────

@dataclass
class TunnelConnector:
    """A registered edge connector (CVG application or external system)."""
    connector_id:     str
    ip:               str
    capabilities:     dict  = field(default_factory=dict)
    registered_at:    float = field(default_factory=time.time)
    last_heartbeat:   float = field(default_factory=time.time)
    blocks_sent:      int   = 0
    blocks_received:  int   = 0
    context_pushes:   int   = 0
    inference_requests: int = 0

    @property
    def online(self) -> bool:
        return (time.time() - self.last_heartbeat) < 120  # 2 min timeout

    def to_dict(self) -> dict:
        return {
            "connector_id":      self.connector_id,
            "ip":                self.ip,
            "capabilities":      self.capabilities,
            "registered_at":     self.registered_at,
            "last_heartbeat":    self.last_heartbeat,
            "online":            self.online,
            "blocks_sent":       self.blocks_sent,
            "context_pushes":    self.context_pushes,
            "inference_requests": self.inference_requests,
        }


# ─── NeuronChain ──────────────────────────────────────────────────────────────

class NeuronChain:
    """
    The CVG Neuron blockchain — an in-memory chain of cryptographically-signed blocks.

    Every AI interaction is recorded as a block.
    The chain is the immutable proof that CVG Neuron has been operating.
    When Neuron is eventually published as an Ollama model, the chain's
    genesis hash will be embedded in the model's identity.
    """

    def __init__(self):
        self._chain: list[TunnelBlock] = [self._make_genesis()]
        self._connectors: dict[str, TunnelConnector] = {}
        self._pending_context: dict[str, list[dict]] = {}  # connector_id → [context_dicts]
        logger.info(
            "[tunnel] NeuronChain initialized — genesis: %s...",
            self._chain[0].hash[:16],
        )

    def _make_genesis(self) -> TunnelBlock:
        return TunnelBlock(
            block_id  = "genesis-" + str(uuid.uuid4())[:8],
            timestamp = time.time(),
            source    = "neuron",
            target    = "broadcast",
            msg_type  = "genesis",
            prev_hash = "0" * 64,
            payload   = {
                "message": "CVG Neuron blockchain initialized",
                "version": TUNNEL_VERSION,
                "identity": "CVG Neuron — Clearview Geographic LLC",
            },
        )

    # ── Chain State ───────────────────────────────────────────────────────────

    @property
    def latest_hash(self) -> str:
        return self._chain[-1].hash

    @property
    def length(self) -> int:
        return len(self._chain)

    @property
    def genesis_hash(self) -> str:
        return self._chain[0].hash

    # ── Block Operations ──────────────────────────────────────────────────────

    def add_block(
        self,
        source:   str,
        target:   str,
        msg_type: str,
        payload:  dict,
    ) -> TunnelBlock:
        """Create, sign, and append a new block to the chain."""
        if msg_type not in MESSAGE_TYPES:
            logger.warning("[tunnel] Unknown msg_type: %s — recording anyway", msg_type)

        block = TunnelBlock(
            block_id  = str(uuid.uuid4()),
            timestamp = time.time(),
            source    = source,
            target    = target,
            msg_type  = msg_type,
            payload   = payload,
            prev_hash = self.latest_hash,
        )
        self._chain.append(block)

        # Prune chain if too long (keep genesis + recent)
        if len(self._chain) > CHAIN_MAX_LEN:
            self._chain = [self._chain[0]] + self._chain[-(CHAIN_MAX_LEN - 1):]

        logger.debug(
            "[tunnel] Block %s [%s → %s] %s",
            block.block_id[:8], source, target, msg_type,
        )
        return block

    def verify_chain(self) -> tuple[bool, str]:
        """
        Verify the entire chain's integrity.
        Returns (True, "chain_valid") or (False, "reason").
        """
        for i in range(1, len(self._chain)):
            block = self._chain[i]
            prev  = self._chain[i - 1]
            if block.prev_hash != prev.hash:
                return False, f"chain_broken_at_block_{i}: prev_hash_mismatch"
            ok, reason = block.verify()
            if not ok:
                return False, f"block_{i}_invalid: {reason}"
        return True, "chain_valid"

    def get_recent_blocks(
        self,
        limit:    int = 20,
        msg_type: Optional[str] = None,
        source:   Optional[str] = None,
    ) -> list[dict]:
        """Return recent blocks, optionally filtered."""
        blocks = list(reversed(self._chain))
        if msg_type:
            blocks = [b for b in blocks if b.msg_type == msg_type]
        if source:
            blocks = [b for b in blocks if b.source == source]
        return [b.to_dict() for b in blocks[:limit]]

    def get_chain_info(self) -> dict:
        valid, msg = self.verify_chain()
        return {
            "chain_length":       self.length,
            "genesis_hash":       self.genesis_hash[:16] + "...",
            "latest_hash":        self.latest_hash[:16] + "...",
            "chain_valid":        valid,
            "chain_status":       msg,
            "connectors_total":   len(self._connectors),
            "connectors_online":  sum(1 for c in self._connectors.values() if c.online),
            "tunnel_version":     TUNNEL_VERSION,
        }

    # ── Connector Management ──────────────────────────────────────────────────

    def generate_token(self, connector_id: str) -> str:
        """Generate an auth token for a connector_id (HMAC-SHA256)."""
        return hmac.new(
            TUNNEL_SECRET.encode(),
            connector_id.encode(),
            hashlib.sha256,
        ).hexdigest()

    def validate_token(self, connector_id: str, token: str) -> bool:
        """Validate a connector's auth token."""
        expected = self.generate_token(connector_id)
        return hmac.compare_digest(token, expected)

    def register_connector(
        self,
        connector_id: str,
        ip:           str,
        capabilities: dict,
        token:        Optional[str] = None,
    ) -> dict:
        """
        Register an edge connector.
        Any CVG application (a forge, a deployable service, an external tool)
        can register to push context to or request inference from Neuron.
        """
        if token and not self.validate_token(connector_id, token):
            raise PermissionError(f"Invalid tunnel token for connector '{connector_id}'")

        connector = TunnelConnector(
            connector_id = connector_id,
            ip           = ip,
            capabilities = capabilities,
        )
        self._connectors[connector_id] = connector
        self.add_block(
            source   = connector_id,
            target   = "neuron",
            msg_type = "register",
            payload  = {"ip": ip, "capabilities": capabilities},
        )

        # Also register as hive edge node if it has an Ollama port
        if capabilities.get("ollama"):
            from cvg_neuron import hive
            hive.register_edge_node(
                node_id     = connector_id,
                ip          = ip,
                description = capabilities.get("description", "Edge connector"),
                ollama_port = capabilities.get("ollama_port", 11434),
                capabilities = capabilities,
            )

        logger.info("[tunnel] Connector registered: %s @ %s", connector_id, ip)
        return {
            "status":       "registered",
            "connector_id": connector_id,
            "genesis_hash": self.genesis_hash[:16] + "...",
            "chain_length": self.length,
        }

    def heartbeat(self, connector_id: str) -> dict:
        """Update connector last-heartbeat timestamp."""
        if connector_id not in self._connectors:
            return {"status": "unknown_connector"}
        self._connectors[connector_id].last_heartbeat = time.time()
        self.add_block(connector_id, "neuron", "heartbeat", {"ts": time.time()})
        return {"status": "ok", "chain_length": self.length}

    def disconnect_connector(self, connector_id: str) -> dict:
        """Gracefully disconnect a connector."""
        if connector_id in self._connectors:
            self.add_block(connector_id, "neuron", "disconnect", {})
            del self._connectors[connector_id]
            from cvg_neuron import hive
            hive.deregister_edge_node(connector_id)
            logger.info("[tunnel] Connector disconnected: %s", connector_id)
            return {"status": "disconnected"}
        return {"status": "not_found"}

    # ── Context Push ──────────────────────────────────────────────────────────

    def push_context(self, connector_id: str, context: dict) -> TunnelBlock:
        """
        An edge connector pushes live context into the Neuron chain.
        This context will be available to Neuron's next inference cycle.
        """
        if connector_id in self._connectors:
            self._connectors[connector_id].context_pushes += 1
            # Accumulate context for this connector
            if connector_id not in self._pending_context:
                self._pending_context[connector_id] = []
            self._pending_context[connector_id].append({
                "ts": time.time(),
                **context,
            })
            # Keep only last 5 context pushes per connector
            self._pending_context[connector_id] = \
                self._pending_context[connector_id][-5:]

        block = self.add_block(connector_id, "neuron", "context_push", context)
        logger.debug("[tunnel] Context push from %s — %d keys", connector_id, len(context))
        return block

    def get_pending_context(self, connector_id: Optional[str] = None) -> dict:
        """Get pending context from one or all connectors."""
        if connector_id:
            return {connector_id: self._pending_context.get(connector_id, [])}
        return dict(self._pending_context)

    def consume_pending_context(self) -> list[dict]:
        """
        Consume all pending context pushes for use in next inference.
        Clears the pending queue after return.
        """
        all_context = []
        for connector_id, contexts in self._pending_context.items():
            for ctx in contexts:
                all_context.append({
                    "source":  connector_id,
                    "context": ctx,
                })
        self._pending_context.clear()
        return all_context

    # ── Inference Routing ─────────────────────────────────────────────────────

    def record_inference_request(
        self,
        connector_id: str,
        model:        str,
        prompt_len:   int,
    ) -> TunnelBlock:
        """Record that an inference was requested via the tunnel."""
        if connector_id in self._connectors:
            self._connectors[connector_id].inference_requests += 1
        return self.add_block(
            source   = connector_id,
            target   = "neuron",
            msg_type = "inference_request",
            payload  = {"model": model, "prompt_len": prompt_len},
        )

    def record_inference_response(
        self,
        connector_id: str,
        model:        str,
        elapsed_ms:   float,
        node_id:      str = "vm-451",
    ) -> TunnelBlock:
        """Record that an inference response was returned via the tunnel."""
        return self.add_block(
            source   = "neuron",
            target   = connector_id,
            msg_type = "inference_response",
            payload  = {
                "model":      model,
                "elapsed_ms": elapsed_ms,
                "node_id":    node_id,
            },
        )

    # ── Event Broadcasting ────────────────────────────────────────────────────

    def broadcast_alert(
        self,
        severity: str,
        subject:  str,
        message:  str,
        source:   str = "neuron",
    ) -> TunnelBlock:
        """Broadcast an alert to all connected connectors via the chain."""
        return self.add_block(
            source   = source,
            target   = "broadcast",
            msg_type = "alert",
            payload  = {
                "severity": severity,
                "subject":  subject,
                "message":  message,
            },
        )

    def record_deploy_event(
        self,
        service:     str,
        version:     str,
        source:      str = "git-engine",
        extra:       Optional[dict] = None,
    ) -> TunnelBlock:
        return self.add_block(
            source   = source,
            target   = "neuron",
            msg_type = "deploy_event",
            payload  = {"service": service, "version": version, **(extra or {})},
        )

    def record_audit_event(
        self,
        severity: str,
        target:   str,
        detail:   str,
        source:   str = "audit-engine",
    ) -> TunnelBlock:
        return self.add_block(
            source   = source,
            target   = "neuron",
            msg_type = "audit_event",
            payload  = {"severity": severity, "target": target, "detail": detail},
        )

    # ── Status ────────────────────────────────────────────────────────────────

    def get_connectors(self) -> list[dict]:
        return [c.to_dict() for c in self._connectors.values()]

    def get_connector(self, connector_id: str) -> Optional[dict]:
        c = self._connectors.get(connector_id)
        return c.to_dict() if c else None

    def get_full_status(self) -> dict:
        chain_info  = self.get_chain_info()
        recent      = self.get_recent_blocks(10)
        connectors  = self.get_connectors()
        valid, msg  = self.verify_chain()
        return {
            "tunnel_version":    TUNNEL_VERSION,
            "chain":             chain_info,
            "connectors":        connectors,
            "pending_contexts":  sum(len(v) for v in self._pending_context.values()),
            "recent_blocks":     recent,
            "integrity":         {"valid": valid, "message": msg},
        }


# ─── Module-Level Singleton ───────────────────────────────────────────────────

_chain: Optional[NeuronChain] = None


def get_chain() -> NeuronChain:
    """Get or create the module-level NeuronChain singleton."""
    global _chain
    if _chain is None:
        _chain = NeuronChain()
    return _chain
