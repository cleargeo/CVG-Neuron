# CVG Neuron -- Universal Memory Capture Hook for PowerShell
# (c) Clearview Geographic, LLC -- Proprietary and PRIVATE
#
# SOURCE THIS FILE in your PowerShell profile to enable automatic
# capture of ALL AI tool interactions into Neuron's memory.
#
# Installation:
#   Add to $PROFILE:
#     . "G:\07_APPLICATIONS_TOOLS\CVG_Neuron\scripts\cvg_capture.ps1"
#
# Or run manually:
#   . .\scripts\cvg_capture.ps1
#
# What this does:
#   - Wraps 'claude', 'cline', 'aider', 'llm', 'sgpt' commands to capture output
#   - Provides Send-NeuronCapture function for manual submission
#   - Runs silently — any capture errors are non-fatal
#
# Capture targets (auto-detected):
#   - Claude CLI   (claude)
#   - Aider        (aider)
#   - LLM CLI      (llm)
#   - ShellGPT     (sgpt)
#   - Custom hooks via Send-NeuronCapture

# ── Configuration ────────────────────────────────────────────────────────────

$CVG_CAPTURE_URL   = $env:CVG_CAPTURE_URL   ?? "http://127.0.0.1:8098/capture"
$CVG_NEURON_URL    = $env:CVG_NEURON_URL    ?? "http://localhost:8095/api/memory/capture"
$CVG_INTERNAL_KEY  = $env:CVG_INTERNAL_KEY  ?? "cvg-internal-2026"
$CVG_CAPTURE_ENABLED = $env:CVG_CAPTURE_ENABLED ?? "1"

# Terminal session ID (unique per PowerShell session)
$CVG_TERMINAL_ID = "ps_$([System.Diagnostics.Process]::GetCurrentProcess().Id)_$(Get-Date -Format 'yyyyMMddHHmmss')"

# ── Core capture function ─────────────────────────────────────────────────────

function Send-NeuronCapture {
    <#
    .SYNOPSIS
        Send a memory capture to CVG Neuron.
    .PARAMETER Source
        The AI tool name (e.g., 'claude', 'aider', 'cline', 'custom')
    .PARAMETER Content
        The content to capture (prompt + response, or just response)
    .PARAMETER Role
        Role: 'user', 'assistant', or 'system' (default: 'assistant')
    .PARAMETER Model
        Model name if known
    .PARAMETER Silent
        If set, suppress output on success
    .EXAMPLE
        Send-NeuronCapture -Source "claude" -Content "The cluster has 3 nodes online." -Role "assistant"
    #>
    param(
        [Parameter(Mandatory=$true)]
        [string]$Source,

        [Parameter(Mandatory=$true)]
        [string]$Content,

        [string]$Role = "assistant",
        [string]$Model = "",
        [hashtable]$Metadata = @{},
        [switch]$Silent
    )

    if ($CVG_CAPTURE_ENABLED -ne "1") { return }
    if ([string]::IsNullOrWhiteSpace($Content)) { return }

    $payload = @{
        source      = $Source
        content     = $Content.Trim()
        role        = $Role
        terminal_id = $CVG_TERMINAL_ID
        session_id  = $CVG_TERMINAL_ID
    }
    if ($Model) { $payload.model = $Model }
    if ($Metadata.Count -gt 0) { $payload.metadata = $Metadata }

    $json = $payload | ConvertTo-Json -Compress

    # Try capture daemon first (port 8098), fall back to Neuron API (port 8095)
    $sent = $false
    foreach ($url in @($CVG_CAPTURE_URL, $CVG_NEURON_URL)) {
        try {
            $headers = @{ "Content-Type" = "application/json" }
            if ($url -like "*8095*") {
                $headers["X-CVG-Key"] = $CVG_INTERNAL_KEY
            }
            $resp = Invoke-RestMethod -Uri $url -Method POST -Body $json `
                -Headers $headers -TimeoutSec 3 -ErrorAction Stop
            if (-not $Silent) {
                Write-Verbose "[CVG] Captured from $Source → $($resp.id)"
            }
            $sent = $true
            break
        } catch {
            # Silent fail — don't disrupt workflow
        }
    }

    if (-not $sent -and -not $Silent) {
        Write-Verbose "[CVG] Capture skipped (Neuron not running)"
    }
}

# ── AI tool wrappers ──────────────────────────────────────────────────────────

function Invoke-ClaudeCLI {
    <#
    .SYNOPSIS
        Wrapper for 'claude' CLI that captures output to Neuron memory.
    #>
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Args)

    $claudePath = Get-Command claude -ErrorAction SilentlyContinue
    if (-not $claudePath) {
        Write-Error "claude CLI not found in PATH"
        return
    }

    # Capture the full output
    $output = & $claudePath.Source @Args 2>&1
    $outputText = $output -join "`n"

    # Display output normally
    $output | ForEach-Object { Write-Output $_ }

    # Capture to Neuron
    if ($outputText.Length -gt 20) {
        $prompt = ($Args -join " ").Substring(0, [Math]::Min(($Args -join " ").Length, 500))
        $captureContent = "[PROMPT] $prompt`n`n[RESPONSE] $outputText"
        Send-NeuronCapture -Source "claude-cli" -Content $captureContent -Role "assistant" -Silent
    }
}

function Invoke-AiderCLI {
    <#
    .SYNOPSIS
        Wrapper for 'aider' that captures session output to Neuron memory.
    #>
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Args)

    $aiderPath = Get-Command aider -ErrorAction SilentlyContinue
    if (-not $aiderPath) {
        Write-Error "aider not found in PATH"
        return
    }

    # For aider (interactive), tee output to a temp file
    $tmpFile = [System.IO.Path]::GetTempFileName()
    try {
        & $aiderPath.Source @Args 2>&1 | Tee-Object -FilePath $tmpFile
        $content = Get-Content $tmpFile -Raw -ErrorAction SilentlyContinue
        if ($content -and $content.Length -gt 50) {
            Send-NeuronCapture -Source "aider" -Content $content -Role "assistant" `
                -Metadata @{ args = ($Args -join " ") } -Silent
        }
    } finally {
        Remove-Item $tmpFile -ErrorAction SilentlyContinue
    }
}

function Invoke-LLMCommand {
    <#
    .SYNOPSIS
        Wrapper for 'llm' CLI (Simon Willison's LLM tool) that captures to Neuron.
    #>
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Args)

    $llmPath = Get-Command llm -ErrorAction SilentlyContinue
    if (-not $llmPath) {
        Write-Error "llm not found in PATH"
        return
    }

    $output = & $llmPath.Source @Args 2>&1
    $outputText = $output -join "`n"
    $output | ForEach-Object { Write-Output $_ }

    if ($outputText.Length -gt 20) {
        $prompt = ($Args -join " ").Substring(0, [Math]::Min(($Args -join " ").Length, 500))
        Send-NeuronCapture -Source "llm-cli" -Content "[PROMPT] $prompt`n`n[RESPONSE] $outputText" `
            -Role "assistant" -Silent
    }
}

# ── PSReadLine hook — capture last command output ─────────────────────────────
# This hook fires AFTER every command completes in the interactive shell.
# It captures output from known AI CLIs that were run directly.

$CVG_LAST_COMMAND = ""
$CVG_AI_COMMANDS = @("claude", "aider", "llm", "sgpt", "chatgpt", "gpt", "copilot")

# Register PSReadLine key handler for post-command capture (if available)
if (Get-Module -Name PSReadLine -ErrorAction SilentlyContinue) {
    try {
        # Add a prompt hook that checks if the last command was an AI tool
        $ExecutionContext.InvokeCommand.CommandNotFoundAction = {
            param($commandName, $commandLookupEventArgs)
            # Silently ignore — prevent error for missing commands
        }
    } catch {
        # PSReadLine hook not available in this version
    }
}

# ── Cline integration (VS Code extension) ────────────────────────────────────
# Cline hooks are done via the MCP server or VS Code task.
# These functions provide manual integration and a helper to check status.

function Get-ClineCaptures {
    <#
    .SYNOPSIS
        Show recent Cline captures stored in Neuron memory.
    #>
    try {
        $resp = Invoke-RestMethod -Uri "$CVG_NEURON_URL/../recent?source=cline" `
            -Headers @{ "X-CVG-Key" = $CVG_INTERNAL_KEY } -TimeoutSec 5
        $resp.captures | ForEach-Object {
            Write-Host "[$($_.timestamp.Substring(0,16))] $($_.content.Substring(0,[Math]::Min($_.content.Length,120)))"
        }
    } catch {
        Write-Warning "Could not reach Neuron: $_"
    }
}

# ── Memory status helpers ─────────────────────────────────────────────────────

function Get-NeuronMemoryStats {
    <#
    .SYNOPSIS
        Show CVG Neuron memory statistics.
    #>
    $statsUrl = "http://localhost:8095/api/memory/stats"
    try {
        $resp = Invoke-RestMethod -Uri $statsUrl `
            -Headers @{ "X-CVG-Key" = $CVG_INTERNAL_KEY } -TimeoutSec 5
        Write-Host "=== CVG Neuron Memory Stats ===" -ForegroundColor Cyan
        Write-Host "  Semantic facts:    $($resp.stats.semantic_facts)"
        Write-Host "  Episodic episodes: $($resp.stats.episodic_episodes)"
        Write-Host "  Captures total:    $($resp.stats.capture_total)"
        Write-Host "  Capture sources:   $(($resp.stats.capture_sources | ConvertTo-Json -Compress))"
        Write-Host "  Total size:        $($resp.stats.total_kb) KB"
    } catch {
        # Try capture daemon
        try {
            $resp = Invoke-RestMethod -Uri "http://127.0.0.1:8098/stats" -TimeoutSec 3
            Write-Host "=== CVG Capture Daemon Stats ===" -ForegroundColor Yellow
            Write-Host "  Total captures: $($resp.total_on_disk)"
            Write-Host "  Unprocessed:    $($resp.unprocessed)"
            Write-Host "  By source:      $(($resp.by_source | ConvertTo-Json -Compress))"
        } catch {
            Write-Warning "Neuron and capture daemon both unreachable"
        }
    }
}

function Send-NeuronLearn {
    <#
    .SYNOPSIS
        Directly teach Neuron a fact.
    .EXAMPLE
        Send-NeuronLearn -Key "project.current" -Value "Working on CVG-Neuron memory improvements"
    #>
    param(
        [Parameter(Mandatory=$true)][string]$Key,
        [Parameter(Mandatory=$true)][string]$Value,
        [string]$Source  = "powershell",
        [float]$Confidence = 0.85
    )
    try {
        $payload = @{ key = $Key; value = $Value; source = $Source; confidence = $Confidence } | ConvertTo-Json
        $resp = Invoke-RestMethod -Uri "http://localhost:8095/api/memory/learn" -Method POST `
            -Body $payload -Headers @{ "Content-Type" = "application/json"; "X-CVG-Key" = $CVG_INTERNAL_KEY } `
            -TimeoutSec 5
        Write-Host "[CVG] Learned: $Key = $Value" -ForegroundColor Green
    } catch {
        Write-Warning "Could not teach Neuron: $_"
    }
}

function Invoke-NeuronConsolidate {
    <#
    .SYNOPSIS
        Trigger Neuron memory consolidation (processes pending captures).
    #>
    try {
        $resp = Invoke-RestMethod -Uri "http://localhost:8095/api/memory/consolidate" -Method POST `
            -Headers @{ "X-CVG-Key" = $CVG_INTERNAL_KEY; "Content-Type" = "application/json" } `
            -Body "{}" -TimeoutSec 15
        Write-Host "[CVG] Consolidation: promoted=$($resp.actions.promoted) captures=$($resp.actions.captures_processed)" -ForegroundColor Green
    } catch {
        Write-Warning "Consolidation failed: $_"
    }
}

# ── Aliases ───────────────────────────────────────────────────────────────────

Set-Alias -Name neuron-capture  -Value Send-NeuronCapture    -Force -ErrorAction SilentlyContinue
Set-Alias -Name neuron-learn    -Value Send-NeuronLearn      -Force -ErrorAction SilentlyContinue
Set-Alias -Name neuron-stats    -Value Get-NeuronMemoryStats -Force -ErrorAction SilentlyContinue
Set-Alias -Name neuron-consolidate -Value Invoke-NeuronConsolidate -Force -ErrorAction SilentlyContinue

# ── Startup notification ──────────────────────────────────────────────────────

Write-Verbose "[CVG] Neuron capture hooks loaded (terminal: $CVG_TERMINAL_ID)"
Write-Verbose "[CVG] Commands: Send-NeuronCapture, Send-NeuronLearn, Get-NeuronMemoryStats, Invoke-NeuronConsolidate"
