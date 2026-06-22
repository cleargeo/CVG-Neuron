# CVG Neuron Watchdog Script
# Monitors the Neuron process and restarts if it dies
# Run via Windows Task Scheduler every 5 minutes

$ErrorActionPreference = 'Stop'
$logFile = "C:\Users\AlexZelenski\CVG-Neuron\logs\watchdog.log"
$neuronPort = 8808
$maxRetries = 3
$retryDelaySec = 10

function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] $msg"
    Add-Content -Path $logFile -Value $line -ErrorAction SilentlyContinue
    Write-Host $line
}

# Ensure log directory exists
New-Item -ItemType Directory -Force -Path "C:\Users\AlexZelenski\CVG-Neuron\logs" | Out-Null

# Check if Neuron is responding on its port
$neuronAlive = $false
try {
    $tcp = Get-NetTCPConnection -State Listen -LocalPort $neuronPort -ErrorAction SilentlyContinue
    if ($tcp) {
        # Port is listening, verify it actually responds
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$neuronPort/api/status/ping" -TimeoutSec 5 -UseBasicParsing -ErrorAction SilentlyContinue
        if ($response.StatusCode -eq 200) {
            $neuronAlive = $true
            Write-Log "Neuron OK (port $neuronPort responding, PID=$($tcp.OwningProcess))"
        }
    }
} catch {
    Write-Log "Neuron health check failed: $($_.Exception.Message)"
}

if (-not $neuronAlive) {
    Write-Log "Neuron DOWN on port $neuronPort -- attempting restart"

    # Kill any stale python/uvicorn processes
    Get-Process python -ErrorAction SilentlyContinue | Where-Object {
        $_.Id -ne (Get-Process -Name "python" -ErrorAction SilentlyContinue | Where-Object {
            (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine -match "hermes"
        } | Select-Object -First 1).Id
    } | Stop-Process -Force -ErrorAction SilentlyContinue

    Start-Sleep -Seconds 3

    # Start Neuron
    $neuronDir = "C:\Users\AlexZelenski\CVG-Neuron"
    $pythonExe = "C:\Users\AlexZelenski\AppData\Local\Programs\Python\Python312\python.exe"

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $pythonExe
    $startInfo.Arguments = "-m uvicorn app.main:app --host 0.0.0.0 --port $neuronPort"
    $startInfo.WorkingDirectory = $neuronDir
    $startInfo.UseShellExecute = $true
    $startInfo.WindowStyle = "Hidden"

    try {
        [System.Diagnostics.Process]::Start($startInfo) | Out-Null
        Write-Log "Neuron restart initiated"

        # Wait and verify
        Start-Sleep -Seconds 8
        $verify = Invoke-WebRequest -Uri "http://127.0.0.1:$neuronPort/api/status/ping" -TimeoutSec 5 -UseBasicParsing -ErrorAction SilentlyContinue
        if ($verify.StatusCode -eq 200) {
            Write-Log "Neuron restart SUCCESS"
        } else {
            Write-Log "Neuron restart VERIFY FAILED (status: $($verify.StatusCode))"
        }
    } catch {
        Write-Log "Neuron restart FAILED: $($_.Exception.Message)"
    }
}
