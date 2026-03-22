# CVG Neuron — AI Intelligence Engine v1.0.0

**Clearview Geographic, LLC — Proprietary**  
*New Smyrna Beach, FL*

---

## Overview

CVG Neuron is the AI intelligence layer of the CVG infrastructure platform. It provides LLM-powered analysis, synthesis, and decision support across all CVG support engines — Version Tracking, DNS, Containerization, and Security Audit. Neuron runs locally via Ollama and exposes a FastAPI REST interface on port **8095**.

```
┌──────────────────────────────────────────────────────────────────┐
│                       CVG NEURON v1.0.0                          │
│              AI Intelligence Engine — Port 8095                  │
├──────────────────────────────────────────────────────────────────┤
│  LLM Backend: Ollama (llama3.1:8b / llama3.1:70b)               │
│  Network:     cvg-platform_cvg_net                               │
│  Host:        cvg-stormsurge-01 (10.10.10.200)                   │
│  Reverse Proxy: Caddy → neuron.cleargeo.tech                     │
└──────────────────────────────────────────────────────────────────┘
```

---

## Architecture

CVG Neuron aggregates live telemetry from all four CVG support engines, builds structured context, then routes queries to a local Ollama instance. All inference stays on-premise — no data leaves the Hive-0 cluster.

```
┌─────────────────────────────────────────────────────────┐
│                   CVG Support Engine Mesh                │
│                                                         │
│  Git Engine :8092 ──────────┐                           │
│  DNS Engine :8094 ──────────┼──► CVG Neuron :8095       │
│  Container  :8091 ──────────┤         │                 │
│  Audit VM   :8096 ──────────┘         ▼                 │
│                                  Ollama :11434           │
│                                  llama3.1:8b            │
└─────────────────────────────────────────────────────────┘
```

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Dashboard UI |
| `GET` | `/health` | Health check + model status |
| `GET` | `/api/status` | Full system status |
| `GET` | `/api/models` | List available Ollama models |
| `POST` | `/api/chat` | General AI chat (CVG context) |
| `POST` | `/api/analyze/infrastructure` | Analyze container/node telemetry |
| `POST` | `/api/analyze/git` | Analyze recent commits + AI detection |
| `POST` | `/api/analyze/dns` | Analyze DNS health + anomalies |
| `POST` | `/api/analyze/security` | Analyze audit/Wazuh alerts |
| `POST` | `/api/analyze/full` | Full cross-engine synthesis report |
| `GET` | `/api/context/live` | Pull live context from all engines |
| `GET` | `/api/history` | Recent analysis history |
| `POST` | `/api/webhook/event` | Receive events from other engines |

---

## Quick Start

```bash
# Clone and configure
cp config/neuron_config.yml.example config/neuron_config.yml
# Edit config as needed

# Deploy
docker compose up -d

# Verify
curl http://10.10.10.200:8095/health
```

---

## Configuration

`config/neuron_config.yml` controls:
- Ollama host + default model
- Engine URLs (auto-discovered on `cvg-platform_cvg_net`)
- Analysis presets and prompt tuning
- History retention

---

## Integration

CVG Neuron uses the shared internal API key `cvg-internal-2026` for all engine-to-engine calls. Each support engine can push events to Neuron's webhook endpoint (`POST /api/webhook/event`) for real-time AI analysis.

---

## Tech Stack

- **Python 3.11** / FastAPI / Uvicorn
- **Ollama** (llama3.1:8b local inference)
- **APScheduler** (background context refresh)
- **httpx** (async engine polling)
- **Docker** / `cvg-platform_cvg_net`

---

*© 2026 Clearview Geographic, LLC — All rights reserved*
