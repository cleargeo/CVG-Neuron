# CVG Neuron Health Check & Notification Script
# Runs via cron to check Neuron health and send alerts through Hermes
# Outputs JSON for the cron agent to process

$ErrorActionPreference = 'SilentlyContinue'
$results = @{
    timestamp = (Get-Date -Format "yyyy-MM-ddTHH:mm:ssZ")
    checks = @()
    overall = "healthy"
}

# Check 1: CVG Neuron API
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8808/api/status/ping" -TimeoutSec 5 -UseBasicParsing
    $results.checks += @{
        name = "cvg-neuron-api"
        status = "ok"
        detail = "HTTP $($r.StatusCode), Neuron alive"
    }
} catch {
    $results.checks += @{
        name = "cvg-neuron-api"
        status = "error"
        detail = $_.Exception.Message
    }
    $results.overall = "degraded"
}

# Check 2: Ollama
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 5 -UseBasicParsing
    $data = $r.Content | ConvertFrom-Json
    $modelCount = ($data.models | Measure-Object).Count
    $results.checks += @{
        name = "ollama"
        status = "ok"
        detail = "$modelCount models available"
    }
} catch {
    $results.checks += @{
        name = "ollama"
        status = "error"
        detail = $_.Exception.Message
    }
    $results.overall = "critical"
}

# Check 3: Disk space
$disk = Get-PSDrive C
$freeGB = [math]::Round($disk.Free / 1GB, 1)
$totalGB = [math]::Round($disk.Used / 1GB + $disk.Free / 1GB, 1)
$pctFree = [math]::Round($disk.Free / ($disk.Used + $disk.Free) * 100, 1)
$diskStatus = if ($pctFree -lt 10) { "warning" } else { "ok" }
$results.checks += @{
    name = "disk-c"
    status = $diskStatus
    detail = "${freeGB}GB free of ${totalGB}GB ($pctFree%)"
}
if ($diskStatus -eq "warning") { $results.overall = "degraded" }

# Check 4: Memory
$os = Get-CimInstance Win32_OperatingSystem
$totalMem = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
$freeMem = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
$usedMem = $totalMem - $freeMem
$pctUsed = [math]::Round($usedMem / $totalMem * 100, 1)
$memStatus = if ($pctUsed -gt 90) { "warning" } else { "ok" }
$results.checks += @{
    name = "memory"
    status = $memStatus
    detail = "${usedMem}GB used of ${totalMem}GB ($pctUsed%)"
}
if ($memStatus -eq "warning") { $results.overall = "degraded" }

# Check 5: Tailscale
try {
    $ts = tailscale status --json 2>$null | ConvertFrom-Json
    $results.checks += @{
        name = "tailscale"
        status = "ok"
        detail = "Tailscale online"
    }
} catch {
    $results.checks += @{
        name = "tailscale"
        status = "warning"
        detail = "Tailscale status unknown"
    }
}

# Output results
$results | ConvertTo-Json -Depth 3
