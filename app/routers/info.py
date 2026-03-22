"""
CVG Neuron AI Orchestration System — /api/info Router
Version: 2.0.0 | Clearview Geographic LLC

System identity and capability information.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.core.config import settings
from app.models.response import NeuronResponse

router = APIRouter(prefix="/api/info", tags=["Info"])


@router.get(
    "",
    response_model=NeuronResponse,
    summary="CVG Neuron system information",
    description="Returns system identity, capabilities, version, and ecosystem integration info.",
)
async def get_info(request: Request) -> NeuronResponse:
    """
    Returns comprehensive CVG Neuron system information:
    - Neuron identity (ID, employee ID, queen, hive)
    - Version and environment
    - Registered capabilities
    - CVG ecosystem integration status
    - AI provider configuration
    - Cognitive levels available
    """
    orchestrator = request.app.state.orchestrator
    info = await orchestrator.get_info()

    # Add extra static info
    info.update({
        "system": "CVG Neuron AI Orchestration System",
        "author": "Clearview Geographic LLC",
        "description": (
            "Central AI orchestration engine for the CVG ecosystem. "
            "Self-optimizing, multi-agent, cognitive processing platform."
        ),
        "cognitive_levels": {
            "basic": "Single-agent direct response",
            "advanced": "Multi-agent parallel synthesis",
            "neural": "Deep processing with memory (NeuroCache + COMB)",
            "autonomous": "Self-directed execution with learning loop",
        },
        "input_methods": [
            "chat", "map_input", "file_upload", "calculators", "wizards",
            "video_consultation", "api_integrations", "email_parser",
            "sms_service", "qr_code_system", "mobile_field_collection", "direct_api",
        ],
        "ecosystem": {
            "hive": {
                "description": "CVG Hive distributed compute cluster",
                "endpoint": settings.hive_endpoint,
                "nodes": settings.hive_nodes,
            },
            "comb": {
                "description": "CVG COMB 6-tier memory management",
                "endpoint": settings.comb_endpoint,
                "tiers": ["pollenstore", "bithive", "waxcell", "entangle", "quantumcell", "neurocache"],
            },
            "observability": {
                "description": "CVG Observability monitoring",
                "endpoint": settings.observability_endpoint,
            },
            "network": {
                "description": "CVG Network infrastructure management",
                "endpoint": settings.network_endpoint,
            },
        },
        "api_endpoints": [
            {"path": "/api/process", "method": "POST", "description": "Submit AI task"},
            {"path": "/api/status",  "method": "GET",  "description": "System health"},
            {"path": "/api/dashboard","method": "GET",  "description": "Real-time stats"},
            {"path": "/api/predict", "method": "POST", "description": "Direct inference"},
            {"path": "/api/train",   "method": "POST", "description": "Submit training data"},
            {"path": "/api/settings","method": "GET,PUT","description": "Runtime settings"},
            {"path": "/api/users",   "method": "CRUD", "description": "User management"},
            {"path": "/api/permissions","method": "GET","description": "RBAC permissions"},
            {"path": "/api/info",    "method": "GET",  "description": "System info"},
        ],
    })

    return NeuronResponse.ok(
        data=info,
        message=f"CVG Neuron v{settings.app_version} — {settings.environment}",
    )
