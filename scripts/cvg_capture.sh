#!/usr/bin/env bash
# CVG Neuron -- Universal Memory Capture Hook for Bash/Zsh
# (c) Clearview Geographic, LLC -- Proprietary and PRIVATE
#
# SOURCE THIS FILE in your shell profile to enable automatic
# capture of ALL AI tool interactions into Neuron's memory.
#
# Installation (Bash):
#   Add to ~/.bashrc or ~/.bash_profile:
#     source /path/to/CVG_Neuron/scripts/cvg_capture.sh
#
# Installation (Zsh):
#   Add to ~/.zshrc:
#     source /path/to/CVG_Neuron/scripts/cvg_capture.sh
#
# What this does:
#   - Wraps 'claude', 'aider', 'llm', 'sgpt' commands to capture output
#   - Provides cvg_capture() function for manual submission
#   - Hooks $PROMPT_COMMAND (bash) / precmd (zsh) to track AI command usage
#   - Runs silently — errors are non-fatal

# ── Configuration ─────────────────────────────────────────────────────────────

CVG_CAPTURE_URL="${CVG_CAPTURE_URL:-http://127.0.0.1:8098/capture}"
CVG_NEURON_URL="${CVG_NEURON_URL:-http://localhost:8095/api/memory/capture}"
CVG_INTERNAL_KEY="${CVG_INTERNAL_KEY:-cvg-internal-2026}"
CVG_CAPTURE_ENABLED="${CVG_CAPTURE_ENABLED:-1}"
CVG_TERMINAL_ID="bash_$$_$(date +%Y%m%d%H%M%S)"

# Track the last command for AI detection
_CVG_LAST_CMD=""
_CVG_AI_COMMANDS=("claude" "claude-cli" "aider" "llm" "sgpt" "chatgpt" "gpt4" "copilot" "continue")

# ── Core capture function ─────────────────────────────────────────────────────

cvg_capture() {
    # Usage: cvg_capture <source> <content> [role] [model]
    # Example: cvg_capture "claude" "The cluster has 3 nodes." "assistant"
    local source="${1:-unknown}"
    local content="${2:-}"
    local role="${3:-assistant}"
    local model="${4:-}"

    [[ "$CVG_CAPTURE_ENABLED" != "1" ]] && return 0
    [[ -z "$content" ]] && return 0
    [[ ${#content} -lt 10 ]] && return 0

    # Escape JSON
    local escaped_content
    escaped_content=$(echo "$content" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read().strip()))" 2>/dev/null \
                      || echo "\"${content//\"/\\\"}\"")

    local json
    json="{\"source\":\"$source\",\"content\":$escaped_content,\"role\":\"$role\",\"terminal_id\":\"$CVG_TERMINAL_ID\""
    [[ -n "$model" ]] && json="${json},\"model\":\"$model\""
    json="${json}}"

    # Try capture daemon (8098) first, then fall back to Neuron API (8095)
    local sent=0
    if curl -sf -X POST "$CVG_CAPTURE_URL" \
        -H "Content-Type: application/json" \
        -d "$json" \
        --connect-timeout 2 \
        --max-time 3 \
        -o /dev/null 2>/dev/null; then
        sent=1
    elif curl -sf -X POST "$CVG_NEURON_URL" \
        -H "Content-Type: application/json" \
        -H "X-CVG-Key: $CVG_INTERNAL_KEY" \
        -d "$json" \
        --connect-timeout 2 \
        --max-time 3 \
        -o /dev/null 2>/dev/null; then
        sent=1
    fi

    return 0  # always succeed — don't break shell
}

# ── AI tool wrappers ──────────────────────────────────────────────────────────

# Claude CLI wrapper
claude() {
    local claude_bin
    claude_bin=$(command -v claude 2>/dev/null || command -v claude-cli 2>/dev/null)
    if [[ -z "$claude_bin" ]]; then
        echo "claude: command not found" >&2
        return 127
    fi

    local tmp_out
    tmp_out=$(mktemp)
    # Run claude, tee output to temp file
    "$claude_bin" "$@" 2>&1 | tee "$tmp_out"
    local exit_code=${PIPESTATUS[0]}

    # Capture output
    local output
    output=$(cat "$tmp_out")
    rm -f "$tmp_out"

    if [[ ${#output} -gt 20 ]]; then
        local prompt="$*"
        cvg_capture "claude-cli" "[PROMPT] ${prompt:0:500}

[RESPONSE] ${output:0:3500}" "assistant"
    fi

    return $exit_code
}

# Aider wrapper
aider() {
    local aider_bin
    aider_bin=$(command -v aider 2>/dev/null)
    if [[ -z "$aider_bin" ]]; then
        echo "aider: command not found" >&2
        return 127
    fi

    local tmp_out
    tmp_out=$(mktemp)
    "$aider_bin" "$@" 2>&1 | tee "$tmp_out"
    local exit_code=${PIPESTATUS[0]}

    local output
    output=$(cat "$tmp_out")
    rm -f "$tmp_out"

    if [[ ${#output} -gt 50 ]]; then
        cvg_capture "aider" "${output:0:3500}" "assistant" "" \
            2>/dev/null
    fi

    return $exit_code
}

# LLM CLI wrapper (Simon Willison's tool)
llm() {
    local llm_bin
    llm_bin=$(command -v llm 2>/dev/null)
    if [[ -z "$llm_bin" ]]; then
        echo "llm: command not found" >&2
        return 127
    fi

    local output
    output=$("$llm_bin" "$@" 2>&1)
    local exit_code=$?
    echo "$output"

    if [[ ${#output} -gt 20 ]]; then
        local prompt="$*"
        cvg_capture "llm-cli" "[PROMPT] ${prompt:0:500}

[RESPONSE] ${output:0:3500}" "assistant"
    fi

    return $exit_code
}

# ShellGPT wrapper
sgpt() {
    local sgpt_bin
    sgpt_bin=$(command -v sgpt 2>/dev/null)
    if [[ -z "$sgpt_bin" ]]; then
        echo "sgpt: command not found" >&2
        return 127
    fi

    local output
    output=$("$sgpt_bin" "$@" 2>&1)
    local exit_code=$?
    echo "$output"

    if [[ ${#output} -gt 20 ]]; then
        local prompt="$*"
        cvg_capture "sgpt" "[PROMPT] ${prompt:0:500}

[RESPONSE] ${output:0:3500}" "assistant"
    fi

    return $exit_code
}

# ── PROMPT_COMMAND / precmd hook for bash history tracking ────────────────────

_cvg_precmd() {
    # Runs before each prompt — detects if last command was an AI tool
    local last_cmd
    last_cmd=$(history 1 2>/dev/null | sed 's/^[ ]*[0-9]*[ ]*//')

    if [[ "$last_cmd" != "$_CVG_LAST_CMD" ]] && [[ -n "$last_cmd" ]]; then
        _CVG_LAST_CMD="$last_cmd"
        local first_word="${last_cmd%% *}"

        # Detect direct AI tool invocations that weren't wrapped
        for ai_cmd in "${_CVG_AI_COMMANDS[@]}"; do
            if [[ "$first_word" == "$ai_cmd" ]]; then
                # Just record the invocation — output was already captured by wrapper
                cvg_capture "$ai_cmd" "[COMMAND] $last_cmd" "user" 2>/dev/null
                break
            fi
        done
    fi
}

# Register the hook
if [[ -n "$BASH_VERSION" ]]; then
    # Bash: append to PROMPT_COMMAND
    if [[ "$PROMPT_COMMAND" != *"_cvg_precmd"* ]]; then
        PROMPT_COMMAND="${PROMPT_COMMAND:+$PROMPT_COMMAND; }_cvg_precmd"
    fi
elif [[ -n "$ZSH_VERSION" ]]; then
    # Zsh: add to precmd_functions
    autoload -Uz add-zsh-hook
    add-zsh-hook precmd _cvg_precmd
fi

# ── Helper functions ──────────────────────────────────────────────────────────

neuron_learn() {
    # Usage: neuron_learn "key" "value" [confidence]
    # Example: neuron_learn "project.status" "Deploying new Neuron container" 0.9
    local key="${1:-}"
    local value="${2:-}"
    local confidence="${3:-0.85}"

    [[ -z "$key" || -z "$value" ]] && { echo "Usage: neuron_learn <key> <value> [confidence]"; return 1; }

    local json
    json=$(python3 -c "import json; print(json.dumps({'key':'$key','value':'$value','source':'bash','confidence':$confidence}))" 2>/dev/null \
          || echo "{\"key\":\"$key\",\"value\":\"$value\",\"source\":\"bash\",\"confidence\":$confidence}")

    curl -sf -X POST "http://localhost:8095/api/memory/learn" \
        -H "Content-Type: application/json" \
        -H "X-CVG-Key: $CVG_INTERNAL_KEY" \
        -d "$json" \
        --connect-timeout 3 --max-time 5 \
        && echo "[CVG] Learned: $key" \
        || echo "[CVG] Failed to teach Neuron (is it running?)"
}

neuron_stats() {
    # Show memory statistics
    echo "=== CVG Neuron Memory Stats ==="
    if curl -sf "http://localhost:8095/api/memory/stats" \
        -H "X-CVG-Key: $CVG_INTERNAL_KEY" \
        --connect-timeout 3 --max-time 5 \
        | python3 -c "
import json, sys
d = json.load(sys.stdin)
s = d.get('stats', {})
print(f\"  Semantic facts:    {s.get('semantic_facts', 0)}\")
print(f\"  Episodic episodes: {s.get('episodic_episodes', 0)}\")
print(f\"  Captures total:    {s.get('capture_total', 0)}\")
print(f\"  Unprocessed:       {s.get('capture_unprocessed', 0)}\")
print(f\"  Capture sources:   {s.get('capture_sources', {})}\")
print(f\"  Total size:        {s.get('total_kb', 0)} KB\")
" 2>/dev/null; then
        :
    else
        echo "  [Neuron not running]"
        # Try capture daemon
        curl -sf "http://127.0.0.1:8098/stats" --connect-timeout 2 --max-time 3 \
            | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f\"  Capture daemon: {d.get('total_on_disk', 0)} total, {d.get('unprocessed', 0)} unprocessed\")
print(f\"  Sources: {d.get('by_source', {})}\")
" 2>/dev/null || echo "  [Capture daemon not running]"
    fi
}

neuron_consolidate() {
    # Trigger memory consolidation
    curl -sf -X POST "http://localhost:8095/api/memory/consolidate" \
        -H "Content-Type: application/json" \
        -H "X-CVG-Key: $CVG_INTERNAL_KEY" \
        -d '{}' \
        --connect-timeout 3 --max-time 15 \
        | python3 -c "
import json, sys
d = json.load(sys.stdin)
a = d.get('actions', {})
print(f\"[CVG] Consolidation: promoted={a.get('promoted',0)} captures={a.get('captures_processed',0)}\")
" 2>/dev/null \
        || echo "[CVG] Consolidation failed (Neuron not running?)"
}

neuron_search() {
    # Search Neuron memory
    local query="${1:-}"
    [[ -z "$query" ]] && { echo "Usage: neuron_search <query>"; return 1; }
    curl -sf "http://localhost:8095/api/memory/search?q=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$query'))")" \
        -H "X-CVG-Key: $CVG_INTERNAL_KEY" \
        --connect-timeout 3 --max-time 5 \
        | python3 -c "
import json, sys
d = json.load(sys.stdin)
print(f\"Results for '{d.get('query', '')}' (total: {d.get('total', 0)})\")
for tier, items in d.get('results', {}).items():
    if items:
        print(f'  [{tier}]')
        for item in items[:3]:
            if isinstance(item, dict):
                key = item.get('key', item.get('event_type', '?'))
                val = str(item.get('value', item.get('summary', '')))[:100]
                print(f'    {key}: {val}')
" 2>/dev/null \
        || echo "[CVG] Search failed (Neuron not running?)"
}

# ── Startup notification ──────────────────────────────────────────────────────

if [[ "${CVG_CAPTURE_VERBOSE:-0}" == "1" ]]; then
    echo "[CVG] Neuron capture hooks loaded (terminal: $CVG_TERMINAL_ID)"
    echo "[CVG] Functions: cvg_capture, neuron_learn, neuron_stats, neuron_consolidate, neuron_search"
fi
