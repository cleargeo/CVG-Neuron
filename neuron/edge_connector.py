"""
neuron/edge_connector.py — CVG Neuron Edge Network & Blockchain Tunnel Interface

Handles intelligence feeds arriving from external CVG deployments through:
  - Blockchain tunnel endpoints (cryptographically signed feeds)
  - Edge network connectors (external CVG application nodes)
  - Feeder nodes (queens/forges deployed outside Hive-0)

External nodes that connect through the edge tunnel can:
  1. Register themselves with Neuron
  2. Submit intelligence payloads (facts, events, observations)
  3. Request cognitive processing (Neuron thinks on their behalf)
  4. Receive Neuron's knowledge as structured responses

All edge traffic is authenticated via the CVG internal key + optional
payload signature. Neuron absorbs intelligence from all edge feeds into
its persistent memory, accumulating distributed knowledge across the
entire CVG ecosystem.
"""

import hashlib
import hmac
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("neuron.edge")

# ---------------------------------------------------------------------------
# Edge connector types
# ---------------------------------------------------------------------------

CONNECTOR_TYPES = {
    "blockchain_tunnel": "Cryptographically authenticated blockchain relay",
    "edge_feeder":       "External CVG application intelligence feed",
    "queen_remote":      "Remote Hive queen node",
    "forge_remote":      "Remote Hive forge node",
    "worker_remote":     "Remote Hive worker node",
    "deployment_hook":   "CVG deployment event hook",
    "audit_relay":       "External audit data relay",
}

# ---------------------------------------------------------------------------
# Intelligence payload schema
# ---------------------------------------------------------------------------

class IntelligencePayload:
    """
    A structured intelligence package submitted by an edge node.

    Fields:
      edge_id:      Registered edge connector ID
      payload_type: "fact" | "event" | "observation" | "alert" | "deployment"
      data:         Dict of intelligence data
      signature:    HMAC-SHA256 of (edge_id + payload_type + timestamp) w/ CVG key
      timestamp:    Unix timestamp of submission
      priority:     1 (low) – 10 (critical)
    """

    def __init__(
        self,
        edge_id: str,
        payload_type: str,
        data: Dict[str, Any],
        signature: str,
        timestamp: Optional[float] = None,
        priority: int = 5,
        source_ip: Optional[str] = None,
    ) -> None:
        self.edge_id = edge_id
        self.payload_type = payload_type
        self.data = data
        self.signature = signature
        self.timestamp = timestamp or time.time()
        self.priority = max(1, min(10, priority))
        self.source_ip = source_ip
        self.received_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return {
            "edge_id": self.edge_id,
            "payload_type": self.payload_type,
            "data": self.data,
            "timestamp": self.timestamp,
            "priority": self.priority,
            "source_ip": self.source_ip,
            "received_at": self.received_at,
        }


# ---------------------------------------------------------------------------
# Edge connector registry entry
# ---------------------------------------------------------------------------

class EdgeConnector:
    """Represents a registered external node connected to Neuron via edge tunnel."""

    def __init__(
        self,
        edge_id: str,
        endpoint: str,
        connector_type: str = "edge_feeder",
        name: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> None:
        self.edge_id = edge_id
        self.endpoint = endpoint
        self.connector_type = connector_type
        self.name = name or edge_id
        self.metadata = metadata or {}
        self.registered_at = datetime.now(timezone.utc).isoformat()
        self.last_seen: Optional[str] = None
        self.payload_count: int = 0
        self.active: bool = True

    def touch(self) -> None:
        self.last_seen = datetime.now(timezone.utc).isoformat()
        self.payload_count += 1

    def to_dict(self) -> dict:
        return {
            "edge_id": self.edge_id,
            "endpoint": self.endpoint,
            "connector_type": self.connector_type,
            "name": self.name,
            "metadata": self.metadata,
            "registered_at": self.registered_at,
            "last_seen": self.last_seen,
            "payload_count": self.payload_count,
            "active": self.active,
            "type_description": CONNECTOR_TYPES.get(self.connector_type, "Unknown"),
        }


# ---------------------------------------------------------------------------
# EdgeNetwork — the main edge interface
# ---------------------------------------------------------------------------

class EdgeNetwork:
    """
    Manages all edge connectors and processes inbound intelligence feeds.

    This is Neuron's interface to the wider CVG ecosystem beyond Hive-0:
    - Remote queens and forges at other sites
    - Blockchain tunnel relays
    - External deployment hooks
    - Field edge nodes connecting through CVG edge network
    """

    def __init__(self, cvg_key: str) -> None:
        self._cvg_key = cvg_key
        self._connectors: Dict[str, EdgeConnector] = {}
        self._feed_log: List[dict] = []  # recent feeds in memory (last 500)
        self._max_log = 500
        logger.info("EdgeNetwork initialised")

    # ------------------------------------------------------------------
    # Connector registration
    # ------------------------------------------------------------------

    def register_connector(
        self,
        edge_id: str,
        endpoint: str,
        connector_type: str = "edge_feeder",
        name: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        """Register a new edge connector with Neuron."""
        if connector_type not in CONNECTOR_TYPES:
            connector_type = "edge_feeder"

        connector = EdgeConnector(
            edge_id=edge_id,
            endpoint=endpoint,
            connector_type=connector_type,
            name=name,
            metadata=metadata,
        )
        self._connectors[edge_id] = connector

        # Also register with cluster if it's a remote node
        if connector_type in ("queen_remote", "forge_remote", "worker_remote"):
            try:
                from .cluster import get_cluster
                node_type = connector_type.replace("_remote", "")
                get_cluster().add_node(
                    name=edge_id,
                    ip=self._endpoint_to_ip(endpoint),
                    node_type=node_type,
                    ollama_port=11434,
                )
                logger.info("Auto-registered edge node %s (%s) with cluster", edge_id, node_type)
            except Exception as exc:
                logger.warning("Could not register edge node with cluster: %s", exc)

        logger.info(
            "Edge connector registered: %s (%s) → %s",
            edge_id, connector_type, endpoint,
        )
        return {"status": "registered", "edge_id": edge_id, "connector_type": connector_type}

    def deregister_connector(self, edge_id: str) -> dict:
        """Remove an edge connector."""
        if edge_id in self._connectors:
            self._connectors[edge_id].active = False
            logger.info("Edge connector deregistered: %s", edge_id)
            return {"status": "deregistered", "edge_id": edge_id}
        return {"status": "not_found", "edge_id": edge_id}

    def list_connectors(self) -> List[dict]:
        """Return all registered connectors."""
        return [c.to_dict() for c in self._connectors.values()]

    def get_connector(self, edge_id: str) -> Optional[dict]:
        """Get a specific connector's info."""
        c = self._connectors.get(edge_id)
        return c.to_dict() if c else None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def verify_signature(
        self,
        edge_id: str,
        payload_type: str,
        timestamp: float,
        provided_signature: str,
    ) -> bool:
        """
        Verify HMAC-SHA256 signature on an inbound payload.

        Signature = HMAC-SHA256(key=cvg_key, msg=f"{edge_id}:{payload_type}:{int(timestamp)}")
        """
        try:
            message = f"{edge_id}:{payload_type}:{int(timestamp)}".encode()
            expected = hmac.new(
                self._cvg_key.encode(),
                message,
                hashlib.sha256,
            ).hexdigest()
            return hmac.compare_digest(expected, provided_signature)
        except Exception as exc:
            logger.warning("Signature verification error: %s", exc)
            return False

    def generate_signature(self, edge_id: str, payload_type: str, timestamp: Optional[float] = None) -> str:
        """
        Generate a signature for an outbound payload (for Neuron→edge comms).
        """
        ts = int(timestamp or time.time())
        message = f"{edge_id}:{payload_type}:{ts}".encode()
        return hmac.new(
            self._cvg_key.encode(),
            message,
            hashlib.sha256,
        ).hexdigest()

    # ------------------------------------------------------------------
    # Intelligence ingestion
    # ------------------------------------------------------------------

    async def ingest(
        self,
        payload: IntelligencePayload,
        require_signature: bool = True,
    ) -> dict:
        """
        Process an inbound intelligence payload from an edge connector.

        1. Authenticate the connector
        2. Verify signature (if required)
        3. Route payload to appropriate handler
        4. Inject intelligence into Neuron's memory via mind.learn()

        Returns dict with processing result.
        """
        edge_id = payload.edge_id

        # Check connector is registered
        connector = self._connectors.get(edge_id)
        if not connector:
            logger.warning("Intelligence from unregistered edge: %s", edge_id)
            return {
                "status": "rejected",
                "reason": "unregistered_connector",
                "edge_id": edge_id,
            }

        if not connector.active:
            return {"status": "rejected", "reason": "connector_inactive", "edge_id": edge_id}

        # Verify signature if required
        if require_signature:
            valid = self.verify_signature(
                edge_id=edge_id,
                payload_type=payload.payload_type,
                timestamp=payload.timestamp,
                provided_signature=payload.signature,
            )
            if not valid:
                logger.warning("Invalid signature from edge connector: %s", edge_id)
                return {
                    "status": "rejected",
                    "reason": "invalid_signature",
                    "edge_id": edge_id,
                }

        # Check timestamp freshness (reject payloads older than 5 minutes)
        age_seconds = time.time() - payload.timestamp
        if age_seconds > 300:
            logger.warning("Stale payload from %s: age=%.0fs", edge_id, age_seconds)
            return {
                "status": "rejected",
                "reason": "stale_payload",
                "age_seconds": age_seconds,
                "edge_id": edge_id,
            }

        # Update connector stats
        connector.touch()

        # Route to handler
        result = await self._handle_payload(payload, connector)

        # Log the feed
        log_entry = {
            **payload.to_dict(),
            "processing_result": result,
        }
        self._feed_log.append(log_entry)
        if len(self._feed_log) > self._max_log:
            self._feed_log = self._feed_log[-self._max_log:]

        return result

    async def _handle_payload(self, payload: IntelligencePayload, connector: EdgeConnector) -> dict:
        """Route payload to appropriate handler based on type."""
        handlers = {
            "fact":       self._handle_fact,
            "event":      self._handle_event,
            "observation": self._handle_observation,
            "alert":      self._handle_alert,
            "deployment": self._handle_deployment,
        }

        handler = handlers.get(payload.payload_type, self._handle_generic)
        try:
            return await handler(payload, connector)
        except Exception as exc:
            logger.error("Payload handler error (%s): %s", payload.payload_type, exc)
            return {"status": "error", "reason": str(exc), "edge_id": payload.edge_id}

    async def _handle_fact(self, payload: IntelligencePayload, connector: EdgeConnector) -> dict:
        """Inject a fact directly into Neuron's semantic memory."""
        data = payload.data
        key = data.get("key", f"edge.{connector.edge_id}.fact.{int(time.time())}")
        value = data.get("value", str(data))
        confidence = float(data.get("confidence", 0.8))

        from .mind import get_mind
        result = get_mind().learn(
            key=key,
            value=value,
            source=f"edge:{connector.edge_id}",
            confidence=confidence,
        )
        logger.info("Fact learned from edge %s: %s", connector.edge_id, key)
        return {"status": "absorbed", "type": "fact", "key": key, **result}

    async def _handle_event(self, payload: IntelligencePayload, connector: EdgeConnector) -> dict:
        """Record an event from an edge node into episodic memory."""
        from .memory import get_memory
        data = payload.data
        event_type = data.get("event_type", "edge_event")
        summary = data.get("summary", str(data)[:200])

        get_memory().episodic.record(
            event_type=f"edge.{event_type}",
            summary=f"[{connector.name}] {summary}",
            metadata={
                "edge_id": connector.edge_id,
                "edge_type": connector.connector_type,
                "priority": payload.priority,
                "raw": data,
            },
        )
        logger.info("Event recorded from edge %s: %s", connector.edge_id, event_type)
        return {"status": "recorded", "type": "event", "event_type": event_type}

    async def _handle_observation(self, payload: IntelligencePayload, connector: EdgeConnector) -> dict:
        """
        An observation is a raw data point requiring Neuron to think about it.
        High-priority observations trigger immediate cognitive processing.
        """
        data = payload.data
        observation_text = data.get("text", str(data)[:500])

        result = {"status": "noted", "type": "observation"}

        # High-priority observations (>= 7) trigger immediate Neuron thought
        if payload.priority >= 7:
            logger.info("High-priority observation from %s — triggering cognition", connector.edge_id)
            try:
                from .mind import get_mind
                think_result = await get_mind().think(
                    message=f"Observation from {connector.name}: {observation_text}",
                    context_type="general",
                )
                result["cognitive_response"] = think_result.get("response", "")[:500]
                result["confidence"] = think_result.get("confidence", "UNCERTAIN")
            except Exception as exc:
                logger.warning("Cognition on observation failed: %s", exc)

        # Record regardless
        from .memory import get_memory
        get_memory().episodic.record(
            event_type="edge.observation",
            summary=f"[{connector.name}] {observation_text[:150]}",
            metadata={"edge_id": connector.edge_id, "priority": payload.priority},
        )

        return result

    async def _handle_alert(self, payload: IntelligencePayload, connector: EdgeConnector) -> dict:
        """Alerts always trigger immediate cognitive processing."""
        data = payload.data
        alert_text = data.get("message", str(data)[:500])
        alert_severity = data.get("severity", "warning").upper()

        logger.warning("ALERT from edge %s [%s]: %s", connector.edge_id, alert_severity, alert_text[:200])

        # Record in episodic memory
        from .memory import get_memory
        get_memory().episodic.record(
            event_type=f"edge.alert.{alert_severity.lower()}",
            summary=f"[{connector.name}] ALERT: {alert_text[:150]}",
            metadata={
                "edge_id": connector.edge_id,
                "severity": alert_severity,
                "raw": data,
            },
        )

        # Always trigger cognition for alerts
        cognitive_response = ""
        try:
            from .mind import get_mind
            think_result = await get_mind().think(
                message=(
                    f"ALERT received from {connector.name} (type: {connector.connector_type}):\n"
                    f"Severity: {alert_severity}\n"
                    f"Message: {alert_text}\n\n"
                    f"Assess this alert and recommend action."
                ),
                context_type="infrastructure",
            )
            cognitive_response = think_result.get("response", "")
        except Exception as exc:
            logger.error("Alert cognition failed: %s", exc)

        return {
            "status": "processed",
            "type": "alert",
            "severity": alert_severity,
            "cognitive_assessment": cognitive_response[:500] if cognitive_response else "Cognition unavailable",
        }

    async def _handle_deployment(self, payload: IntelligencePayload, connector: EdgeConnector) -> dict:
        """Process a deployment event from an edge node."""
        data = payload.data
        app_name = data.get("app_name", "unknown")
        deploy_status = data.get("status", "unknown")
        deploy_env = data.get("environment", "unknown")

        # Learn the deployment fact
        from .mind import get_mind
        get_mind().learn(
            key=f"deployment.{app_name}.{deploy_env}",
            value=f"Status: {deploy_status} at {datetime.now(timezone.utc).isoformat()}",
            source=f"edge:{connector.edge_id}",
            confidence=0.95,
        )
        logger.info("Deployment event from %s: %s → %s (%s)", connector.edge_id, app_name, deploy_status, deploy_env)

        return {
            "status": "recorded",
            "type": "deployment",
            "app_name": app_name,
            "deploy_status": deploy_status,
            "environment": deploy_env,
        }

    async def _handle_generic(self, payload: IntelligencePayload, connector: EdgeConnector) -> dict:
        """Fallback handler for unknown payload types."""
        from .memory import get_memory
        get_memory().episodic.record(
            event_type=f"edge.{payload.payload_type}",
            summary=f"[{connector.name}] {str(payload.data)[:200]}",
            metadata={"edge_id": connector.edge_id, "payload_type": payload.payload_type},
        )
        return {"status": "stored", "type": payload.payload_type, "handler": "generic"}

    # ------------------------------------------------------------------
    # Feed log / stats
    # ------------------------------------------------------------------

    def recent_feeds(self, limit: int = 50) -> List[dict]:
        """Return the most recent intelligence feeds received."""
        return self._feed_log[-limit:]

    def stats(self) -> dict:
        """Return EdgeNetwork statistics."""
        active = sum(1 for c in self._connectors.values() if c.active)
        total_payloads = sum(c.payload_count for c in self._connectors.values())
        type_breakdown: Dict[str, int] = {}
        for c in self._connectors.values():
            t = c.connector_type
            type_breakdown[t] = type_breakdown.get(t, 0) + 1

        return {
            "total_connectors": len(self._connectors),
            "active_connectors": active,
            "total_payloads_received": total_payloads,
            "connector_type_breakdown": type_breakdown,
            "recent_feed_count": len(self._feed_log),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _endpoint_to_ip(endpoint: str) -> str:
        """Extract IP from endpoint URL like http://10.10.10.x:port."""
        try:
            host = endpoint.split("//")[-1].split(":")[0].split("/")[0]
            return host
        except Exception:
            return endpoint


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

import os

_edge_network_instance: Optional[EdgeNetwork] = None


def get_edge_network() -> EdgeNetwork:
    """Return the module-level EdgeNetwork singleton."""
    global _edge_network_instance
    if _edge_network_instance is None:
        cvg_key = os.getenv("CVG_INTERNAL_KEY", "cvg-internal-2026")
        _edge_network_instance = EdgeNetwork(cvg_key=cvg_key)
    return _edge_network_instance
