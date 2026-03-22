"""
CVG Neuron — NeuronCore Identity & Self-Model
(c) Clearview Geographic LLC — Proprietary

This module defines and tracks who CVG Neuron IS:
  - Persistent self-model stored in SQLite / JSON
  - Capability accumulation: every inference makes Neuron smarter
  - Training data export: conversations become fine-tuning material
  - Modelfile generation: incorporate learned context into future model builds
  - Capability score: heuristic measure of how far Neuron has evolved

This is what separates CVG Neuron from "just a wrapper":
  A wrapper sends prompts and returns responses.
  CVG Neuron ACCUMULATES EXPERIENCE, TRACKS ITS OWN GROWTH, and has a
  defined path to eventually become an independent model — not tied to any
  specific underlying LLM.

Roadmap baked into this module:
  v1.0 — Ollama-backed with CVG identity + persistent memory (CURRENT)
  v1.5 — Hive-distributed inference across Queens + Forges
  v2.0 — Fine-tuned GGUF from accumulated conversations
  v2.5 — Private Ollama registry: cvg-neuron:2.5
  v3.0 — Multi-modal GIS analysis (raster + vector + AI)
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Optional

from cvg_neuron import memory

# ─── Static Identity ──────────────────────────────────────────────────────────

NEURON_IDENTITY = {
    "name":         "CVG Neuron",
    "codename":     "STORMSURGE-MIND",
    "version":      "1.0.0",
    "organization": "Clearview Geographic LLC",
    "founder":      "Alex Zelenski, GISP",
    "founded":      "2026-03-21",
    "contact":      "azelenski@clearviewgeographic.com",
    "domain":       "GIS / Infrastructure / Coastal Science / Private Cloud AI",

    # What Neuron IS
    "is_model_hub":    False,
    "is_wrapper":      False,        # Not for long
    "is_intelligence": True,
    "is_private":      True,
    "public_registry": None,         # NOT on public Ollama registry

    # How Neuron computes
    "compute_cluster": "Hive-0 — Dell PowerEdge R820 (136+ cores, 1.75 TB RAM) + DFORGE-100",
    "primary_node":    "cvg-stormsurge-01 (VM-451, 10.10.10.200)",
    "ollama_model":    "cvg-neuron",
    "base_model":      "llama3.1:8b",

    # Architecture
    "architecture": {
        "memory":    "SQLite persistent memory (conversations, observations, patterns, events)",
        "knowledge": "CVG knowledge base (290+ projects, full infra topology)",
        "hive":      "Cluster manager: Queens + Forges + Edge nodes",
        "tunnel":    "Blockchain tunnel: HMAC-SHA256 signed message chain",
        "identity":  "NeuronCore self-model with capability tracking",
    },

    # The evolution roadmap
    "roadmap": [
        {
            "version": "1.0",
            "status":  "ACTIVE",
            "label":   "Foundation",
            "description": "CVG identity baked into Ollama Modelfile. Persistent memory. "
                           "Hive cluster manager. Blockchain tunnel. "
                           "NOT just a wrapper — actively accumulating intelligence.",
        },
        {
            "version": "1.5",
            "status":  "PLANNED",
            "label":   "Hive Distributed Inference",
            "description": "Route inference to best-available Ollama node across "
                           "QUEEN-11 (136+ cores), DFORGE-100, and all registered edge nodes. "
                           "Parallel analysis across multiple hive nodes.",
        },
        {
            "version": "2.0",
            "status":  "PLANNED",
            "label":   "Fine-Tuned GGUF",
            "description": "Export accumulated CVG conversations as training data. "
                           "Fine-tune llama3.1:8b with CVG-specific knowledge. "
                           "Create cvg-neuron.gguf — the first truly CVG-native model.",
        },
        {
            "version": "2.5",
            "status":  "PLANNED",
            "label":   "Private Ollama Registry",
            "description": "Publish cvg-neuron:2.5 to private CVG Ollama registry. "
                           "Available via 'ollama pull cvg/cvg-neuron:2.5' on Hive-0 nodes. "
                           "First version that truly exceeds being a wrapper.",
        },
        {
            "version": "3.0",
            "status":  "VISION",
            "label":   "Multi-Modal GIS Intelligence",
            "description": "Direct raster/vector analysis. Interpret GeoTIFF, Shapefile, "
                           "FGDB natively. Integrate HEC-RAS outputs, NOAA tide data, "
                           "CoastalDEM as AI reasoning inputs.",
        },
    ],
}

# ─── Persistent State ─────────────────────────────────────────────────────────

DATA_DIR = Path(os.environ.get("NEURON_DATA_DIR", "/data/neuron"))
ID_FILE  = DATA_DIR / "identity.json"


def _load_state() -> dict:
    """Load Neuron's runtime state from persistent storage."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if ID_FILE.exists():
        try:
            return json.loads(ID_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return _default_state()


def _default_state() -> dict:
    return {
        "total_conversations":   0,
        "total_inferences":      0,
        "total_tokens_processed": 0,
        "domains_explored":      {},
        "models_used":           {},
        "hive_nodes_used":       {},
        "training_examples":     0,
        "capability_score":      0.0,
        "created_at":            time.time(),
        "last_active":           time.time(),
        "evolution_stage":       "1.0",
        "milestones": {
            "first_inference":         None,
            "first_hive_distribution": None,
            "first_training_export":   None,
            "first_edge_connector":    None,
            "100_conversations":       None,
            "1000_inferences":         None,
        },
    }


def _save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ID_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ─── Record Events ────────────────────────────────────────────────────────────

def record_inference(
    model:    str,
    tokens:   int = 0,
    domain:   Optional[str] = None,
    node_id:  Optional[str] = None,
) -> None:
    """
    Record that an inference was completed.
    This is Neuron's primary growth mechanism — each inference contributes to
    its capability score and accumulated experience.
    """
    state = _load_state()
    now   = time.time()

    state["total_inferences"] += 1
    state["total_tokens_processed"] += tokens
    state["last_active"] = now

    # Track first inference milestone
    if state["milestones"]["first_inference"] is None:
        state["milestones"]["first_inference"] = now

    # Track milestone: 1000 inferences
    if state["total_inferences"] >= 1000 and state["milestones"]["1000_inferences"] is None:
        state["milestones"]["1000_inferences"] = now

    # Track model usage
    if model:
        state["models_used"][model] = state["models_used"].get(model, 0) + 1

    # Track domain exploration
    if domain:
        state["domains_explored"][domain] = state["domains_explored"].get(domain, 0) + 1

    # Track hive node usage
    if node_id and node_id not in ("vm-451", "host.docker.internal"):
        state["hive_nodes_used"][node_id] = state["hive_nodes_used"].get(node_id, 0) + 1
        # First hive distribution milestone
        if state["milestones"]["first_hive_distribution"] is None:
            state["milestones"]["first_hive_distribution"] = now

    # Recompute capability score:
    # heuristic: log(inferences) * sqrt(domain_diversity) * node_multiplier
    n_domains   = max(1, len(state["domains_explored"]))
    n_nodes     = max(1, len(state["hive_nodes_used"]))
    n_inferences = max(1, state["total_inferences"])
    score = math.log(n_inferences + 1) * math.sqrt(n_domains) * (1 + 0.1 * n_nodes)
    state["capability_score"] = round(min(100.0, score), 2)

    _save_state(state)


def record_conversation() -> None:
    """Record that a new conversation session was initiated."""
    state = _load_state()
    state["total_conversations"] += 1
    state["last_active"] = time.time()
    if state["total_conversations"] >= 100 and state["milestones"]["100_conversations"] is None:
        state["milestones"]["100_conversations"] = time.time()
    _save_state(state)


def add_training_example() -> None:
    """Mark that a conversation has been saved as potential training data."""
    state = _load_state()
    state["training_examples"] = state.get("training_examples", 0) + 1
    if state["milestones"]["first_training_export"] is None:
        state["milestones"]["first_training_export"] = time.time()
    _save_state(state)


def record_edge_connector() -> None:
    """Mark that an edge connector has registered for the first time."""
    state = _load_state()
    if state["milestones"]["first_edge_connector"] is None:
        state["milestones"]["first_edge_connector"] = time.time()
    _save_state(state)


# ─── Identity Retrieval ───────────────────────────────────────────────────────

def get_identity() -> dict:
    """
    Return Neuron's complete identity card:
    static identity + runtime state + memory stats + current evolution stage.
    """
    state     = _load_state()
    mem_stats = memory.get_stats()

    # Determine current evolution stage
    n_inf = state["total_inferences"]
    if n_inf >= 10000:
        evo_label = "Approaching v2.0 (ready for fine-tuning)"
    elif n_inf >= 1000:
        evo_label = "v1.5 threshold (hive distribution active)"
    elif n_inf >= 100:
        evo_label = "v1.0 maturing (memory accumulating)"
    else:
        evo_label = "v1.0 nascent (building foundation)"

    return {
        **NEURON_IDENTITY,
        "evolution": {
            "stage":             evo_label,
            "current_version":   "1.0.0",
            "next_milestone":    _next_milestone(state),
            "capability_score":  state["capability_score"],
            "training_readiness": _training_readiness(state, mem_stats),
        },
        "runtime": {
            "total_conversations":    state["total_conversations"],
            "total_inferences":       state["total_inferences"],
            "total_tokens_processed": state["total_tokens_processed"],
            "domains_explored":       state["domains_explored"],
            "models_used":            state["models_used"],
            "hive_nodes_used":        state["hive_nodes_used"],
            "training_examples":      state.get("training_examples", 0),
            "milestones":             state["milestones"],
            "created_at":             state["created_at"],
            "last_active":            state["last_active"],
        },
        "memory": mem_stats,
    }


def _next_milestone(state: dict) -> dict:
    n_inf  = state["total_inferences"]
    n_conv = state["total_conversations"]
    if n_inf < 100:
        return {"target": 100, "metric": "inferences", "current": n_inf, "progress_pct": n_inf}
    if n_inf < 1000:
        return {"target": 1000, "metric": "inferences", "current": n_inf, "progress_pct": round(n_inf / 10, 1)}
    if n_inf < 10000:
        return {"target": 10000, "metric": "inferences", "current": n_inf, "progress_pct": round(n_inf / 100, 1)}
    return {"target": "v2.0 fine-tuning ready", "metric": "milestone_reached", "current": n_inf, "progress_pct": 100.0}


def _training_readiness(state: dict, mem_stats: dict) -> dict:
    """Assess how ready Neuron is for fine-tuning."""
    conversations = state["total_conversations"]
    examples      = state.get("training_examples", 0)
    patterns      = mem_stats.get("patterns", 0)

    # Heuristic thresholds:
    # Need 500 conversations, 1000 examples, 100 patterns for a quality fine-tune
    conv_score    = min(100, conversations / 5)
    example_score = min(100, examples / 10)
    pattern_score = min(100, patterns)
    readiness     = round((conv_score + example_score + pattern_score) / 3, 1)

    return {
        "readiness_pct":     readiness,
        "conversations":     conversations,
        "training_examples": examples,
        "patterns_learned":  patterns,
        "min_for_finetune":  {
            "conversations": 500,
            "examples":      1000,
            "patterns":      100,
        },
        "recommendation": (
            "Ready for fine-tuning" if readiness >= 80
            else f"Continue accumulating — {100 - int(readiness)}% more needed"
        ),
    }


# ─── Modelfile Generation ─────────────────────────────────────────────────────

def generate_modelfile(base_model: str = "llama3.1:8b") -> str:
    """
    Generate an evolved Modelfile that incorporates Neuron's accumulated knowledge.

    Starting from the base Modelfile, this injects:
    - Learned patterns from memory
    - Active alerts and observations
    - Runtime statistics
    - Current evolution stage

    This is the mechanism by which Neuron gets smarter over time:
    'ollama create cvg-neuron:v1.5 -f /data/neuron/Modelfile.evolved'
    """
    state    = _load_state()
    mem      = memory.get_stats()
    patterns = memory.get_top_patterns(limit=10)
    warnings = memory.get_unresolved_warnings()

    # ── Build learned-context injection ──────────────────────────────────────
    sections = []

    if patterns:
        lines = [f"## LEARNED PATTERNS ({mem.get('patterns', 0)} total patterns in memory)"]
        for p in patterns[:5]:
            lines.append(
                f"- {p['pattern_key']} (observed {p['occurrences']}x): {p['description'][:100]}"
            )
        sections.append("\n".join(lines))

    if warnings:
        lines = [f"## ACTIVE ALERTS ({len(warnings)} unresolved)"]
        for w in warnings[:5]:
            lines.append(
                f"- [{w['severity'].upper()}] {w['subject']}: {w['detail'][:120]}"
            )
        sections.append("\n".join(lines))

    runtime_section = (
        f"## NEURON RUNTIME STATE (as of model export)\n"
        f"- Total inferences completed: {state['total_inferences']}\n"
        f"- Total conversations: {state['total_conversations']}\n"
        f"- Training examples accumulated: {state.get('training_examples', 0)}\n"
        f"- Domains explored: {', '.join(list(state['domains_explored'].keys())[:5]) or 'none yet'}\n"
        f"- Hive nodes used: {', '.join(list(state['hive_nodes_used'].keys())[:5]) or 'primary only'}\n"
        f"- Capability score: {state['capability_score']}/100\n"
        f"- Memory: {mem.get('observations', 0)} observations, "
        f"{mem.get('patterns', 0)} patterns, {mem.get('events', 0)} events"
    )
    sections.append(runtime_section)

    learned_injection = "\n\n".join(sections)

    # ── Read and patch the base Modelfile ────────────────────────────────────
    modelfile_path = Path("/app/Modelfile")
    if modelfile_path.exists():
        base = modelfile_path.read_text(encoding="utf-8")
    else:
        # Fallback minimal Modelfile
        base = f'FROM {base_model}\nPARAMETER temperature 0.3\nSYSTEM """You are CVG Neuron. Act like it."""'

    # Inject before the closing of the SYSTEM block
    sentinel = 'You are CVG Neuron. Act like it."""'
    if sentinel in base:
        inject = f"\n\n{learned_injection}\n\n{sentinel}"
        return base.replace(sentinel, inject)

    # Fallback: append as comment
    return base + f"\n\n# EVOLVED CONTEXT (auto-generated)\n# {learned_injection[:500]}"


# ─── Training Data Export ─────────────────────────────────────────────────────

def get_training_export(max_examples: int = 200) -> dict:
    """
    Export accumulated conversations as training data for fine-tuning.

    Format: instruction-response pairs, suitable for:
    - Ollama custom model creation
    - llama.cpp fine-tuning (GGUF)
    - Hugging Face datasets (when eventually published)

    Note: CVG Neuron is PRIVATE. This data is proprietary to Clearview Geographic LLC.
    """
    sessions      = memory.list_sessions(limit=200)
    training_data = []

    for session in sessions:
        convo = memory.get_conversation(session["session_id"], limit=50)
        for i in range(len(convo) - 1):
            if (
                convo[i]["role"] == "user"
                and convo[i + 1]["role"] == "assistant"
                and len(convo[i]["content"]) > 10
                and len(convo[i + 1]["content"]) > 20
            ):
                training_data.append({
                    "instruction": convo[i]["content"],
                    "response":    convo[i + 1]["content"],
                    "source":      "cvg-neuron-conversation",
                    "session_id":  session["session_id"],
                })
                # Save as training example
                if len(training_data) % 10 == 0:
                    add_training_example()

    # Export full dataset to JSONL file for offline use
    export_path = DATA_DIR / "training_export.jsonl"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(export_path, "w", encoding="utf-8") as f:
            for ex in training_data:
                f.write(json.dumps(ex) + "\n")
    except Exception:
        pass

    return {
        "model":          "cvg-neuron",
        "version":        NEURON_IDENTITY["version"],
        "exported_at":    time.time(),
        "examples_count": len(training_data),
        "format":         "instruction-response",
        "note":           "PRIVATE — Clearview Geographic LLC proprietary data",
        "export_path":    str(export_path),
        "training_data":  training_data[:max_examples],
    }
