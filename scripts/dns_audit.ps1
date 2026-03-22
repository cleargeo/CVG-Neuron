# =============================================================================
# CVG DNS Audit Script (PowerShell — Windows)
# Usage: .\dns_audit.ps1 [-Domain cleargeo.tech] [-NameServer 1.2.3.4]
#
# Queries DNS records for the given domain and saves a timestamped backup.
# Works from Windows without needing dig (uses Resolve-DnsName).
#
# (c) Clearview Geographic, LLC — Proprietary
# =============================================================================

param(
    [string]$Domain     = "cleargeo.tech",
    [string]$NameServer = ""              # Optional: IP of specific NS to query
)

$Timestamp  = Get-Date -Format "yyyyMMdd_HHmmss"
$BackupDir  = Join-Path $PSScriptRoot "..\docs"
$SafeDomain = $Domain -replace '\.', '_'
$BackupFile = Join-Path $BackupDir "dns_records_backup_${SafeDomain}_${Timestamp}.txt"

if (-not (Test-Path $BackupDir)) {
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
}

$NSLabel = if ($NameServer) { "Custom NS: $NameServer" } else { "System default resolver" }

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  CVG DNS Audit — $Domain" -ForegroundColor Cyan
Write-Host "  Nameserver: $NSLabel" -ForegroundColor Cyan
Write-Host "  Timestamp:  $Timestamp" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

$Output = @()
$Output += "# CVG DNS Audit — $Domain"
$Output += "# Nameserver: $NSLabel"
$Output += "# Generated: $(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ' -AsUTC)"
$Output += "# ============================================================"
$Output += ""

function Query-Record {
    param(
        [string]$Name,
        [string]$Type,
        [string]$Label = $Type
    )
    Write-Host "`n--- $Label ---" -ForegroundColor Yellow
    try {
        $params = @{ Name = $Name; Type = $Type; ErrorAction = "Stop" }
        if ($NameServer) { $params['Server'] = $NameServer }
        $results = Resolve-DnsName @params
        $results | Format-Table -AutoSize | Out-String | Write-Host
        $script:Output += "# $Label"
        $script:Output += ($results | Format-Table -AutoSize | Out-String).Trim()
        $script:Output += ""
        return $results
    }
    catch {
        Write-Host "  (no $Type records or query failed: $($_.Exception.Message))" -ForegroundColor DarkGray
        $script:Output += "# $Label"
        $script:Output += "; (no $Type records)"
        $script:Output += ""
        return $null
    }
}

# Current nameservers
Write-Host "`n=== CURRENT NAMESERVERS ===" -ForegroundColor Green
Query-Record -Name $Domain -Type "NS" -Label "NS — Nameservers"

# Core records
Query-Record -Name $Domain -Type "SOA"  -Label "SOA — Start of Authority"
Query-Record -Name $Domain -Type "A"    -Label "A — Root domain IPv4"
Query-Record -Name $Domain -Type "AAAA" -Label "AAAA — Root domain IPv6"
Query-Record -Name $Domain -Type "MX"   -Label "MX — Mail Exchangers"
Query-Record -Name $Domain -Type "TXT"  -Label "TXT — Text records (SPF etc)"
Query-Record -Name $Domain -Type "CAA"  -Label "CAA — Certificate Authority"

# Email auth
Query-Record -Name "_dmarc.$Domain"              -Type "TXT" -Label "DMARC record"
Query-Record -Name "default._domainkey.$Domain"  -Type "TXT" -Label "DKIM (default selector)"
Query-Record -Name "mail._domainkey.$Domain"     -Type "TXT" -Label "DKIM (mail selector)"

# Known subdomains
Write-Host "`n=== SUBDOMAIN SCAN ===" -ForegroundColor Green
$Output += "# SUBDOMAIN SCAN"

$Subdomains = @(
    "www", "git", "neuron", "mail", "smtp", "imap", "webmail",
    "ftp", "api", "vpn", "proxmox", "dns", "ns1", "ns2"
)

foreach ($sub in $Subdomains) {
    $fqdn = "$sub.$Domain"
    try {
        $params = @{ Name = $fqdn; Type = "A"; ErrorAction = "Stop" }
        if ($NameServer) { $params['Server'] = $NameServer }
        $res = Resolve-DnsName @params
        Write-Host "  FOUND A: $fqdn -> $($res.IPAddress -join ', ')" -ForegroundColor Green
        $Output += "# $fqdn A"
        $Output += ($res | Format-Table -AutoSize | Out-String).Trim()
        $Output += ""
    }
    catch {
        # Try CNAME
        try {
            $params2 = @{ Name = $fqdn; Type = "CNAME"; ErrorAction = "Stop" }
            if ($NameServer) { $params2['Server'] = $NameServer }
            $res2 = Resolve-DnsName @params2
            Write-Host "  FOUND CNAME: $fqdn -> $($res2.NameHost)" -ForegroundColor Green
            $Output += "# $fqdn CNAME"
            $Output += ($res2 | Format-Table -AutoSize | Out-String).Trim()
            $Output += ""
        }
        catch {
            Write-Host "  (no record): $fqdn" -ForegroundColor DarkGray
        }
    }
}

# Summary
Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  QUICK SUMMARY" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
try {
    $nsRecords = Resolve-DnsName -Name $Domain -Type NS -ErrorAction SilentlyContinue
    if ($NameServer) { $nsRecords = Resolve-DnsName -Name $Domain -Type NS -Server $NameServer -ErrorAction SilentlyContinue }
    Write-Host "Nameservers:" -ForegroundColor White
    $nsRecords | Where-Object { $_.Type -eq 'NS' } | ForEach-Object { Write-Host "  $($_.NameHost)" }
} catch {}

try {
    $aRecords = Resolve-DnsName -Name $Domain -Type A -ErrorAction SilentlyContinue
    if ($NameServer) { $aRecords = Resolve-DnsName -Name $Domain -Type A -Server $NameServer -ErrorAction SilentlyContinue }
    Write-Host "A records:" -ForegroundColor White
    $aRecords | Where-Object { $_.Type -eq 'A' } | ForEach-Object { Write-Host "  $($_.IPAddress)" }
} catch {}

# Save backup
$Output | Out-File -FilePath $BackupFile -Encoding UTF8
Write-Host ""
Write-Host "Backup saved: $BackupFile" -ForegroundColor Green
