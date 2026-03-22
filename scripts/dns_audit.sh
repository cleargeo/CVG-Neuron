#!/usr/bin/env bash
# =============================================================================
# CVG DNS Audit Script
# Usage: bash dns_audit.sh [domain] [--nameserver NS_IP]
#
# Queries current HostGator nameservers (or a custom NS) and dumps ALL DNS
# records for the given domain. Saves a timestamped backup file.
#
# (c) Clearview Geographic, LLC — Proprietary
# =============================================================================

set -euo pipefail

DOMAIN="${1:-cleargeo.tech}"
CUSTOM_NS="${3:-}"   # Optional: pass --nameserver <IP> as 2nd and 3rd args
BACKUP_DIR="$(dirname "$0")/../docs"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="${BACKUP_DIR}/dns_records_backup_${DOMAIN//\./_}_${TIMESTAMP}.txt"

# Parse optional --nameserver flag
if [[ "${2:-}" == "--nameserver" ]] && [[ -n "${3:-}" ]]; then
    CUSTOM_NS="$3"
fi

# Determine which nameserver to query
if [[ -n "$CUSTOM_NS" ]]; then
    NS_TARGET="@${CUSTOM_NS}"
    NS_LABEL="Custom NS: $CUSTOM_NS"
else
    NS_TARGET=""
    NS_LABEL="System default resolver"
fi

# Check for dig
if ! command -v dig &>/dev/null; then
    echo "ERROR: 'dig' not found. Install: apt-get install dnsutils" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"

echo "============================================================"
echo "  CVG DNS Audit — $DOMAIN"
echo "  Nameserver: $NS_LABEL"
echo "  Timestamp:  $TIMESTAMP"
echo "============================================================"
echo ""

{
    echo "# CVG DNS Audit — $DOMAIN"
    echo "# Nameserver: $NS_LABEL"
    echo "# Generated: $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo "# ============================================================"
    echo ""
} > "$BACKUP_FILE"

# Function to query and display + save a record type
query_record() {
    local rtype="$1"
    local label="${2:-$rtype}"
    local qname="${3:-$DOMAIN}"
    echo "--- $label ---"
    local result
    # shellcheck disable=SC2086
    result=$(dig $NS_TARGET "$qname" "$rtype" +noall +answer +multiline 2>/dev/null || true)
    if [[ -n "$result" ]]; then
        echo "$result"
    else
        echo "(no $rtype records found)"
    fi
    echo ""
    {
        echo "# $label"
        if [[ -n "$result" ]]; then
            echo "$result"
        else
            echo "; (no $rtype records)"
        fi
        echo ""
    } >> "$BACKUP_FILE"
}

# Query current nameservers (from WHOIS/registry perspective)
echo "=== CURRENT NAMESERVERS (authoritative) ==="
dig "$DOMAIN" NS +short 2>/dev/null | sort | tee -a "$BACKUP_FILE"
echo ""

# Core record types
query_record "SOA"  "SOA — Start of Authority"
query_record "NS"   "NS — Nameservers"
query_record "A"    "A — Root domain IPv4"
query_record "AAAA" "AAAA — Root domain IPv6"
query_record "MX"   "MX — Mail"
query_record "TXT"  "TXT — Text (SPF, DKIM, DMARC, verification)"

# Email authentication specifics
echo "--- DMARC ---"
dig ${NS_TARGET} "_dmarc.${DOMAIN}" TXT +noall +answer 2>/dev/null \
    | tee -a "$BACKUP_FILE" || true
echo ""

echo "--- DKIM (default selector) ---"
dig ${NS_TARGET} "default._domainkey.${DOMAIN}" TXT +noall +answer 2>/dev/null \
    | tee -a "$BACKUP_FILE" || true
echo ""

echo "--- DKIM (mail selector) ---"
dig ${NS_TARGET} "mail._domainkey.${DOMAIN}" TXT +noall +answer 2>/dev/null \
    | tee -a "$BACKUP_FILE" || true
echo ""

query_record "CAA"  "CAA — Certificate Authority Authorization"
query_record "SRV"  "SRV — Service Records"

# Known CVG subdomains
SUBDOMAINS=(
    "www"
    "git"
    "neuron"
    "mail"
    "smtp"
    "imap"
    "webmail"
    "ftp"
    "api"
    "vpn"
    "proxmox"
    "dns"
    "ns1"
    "ns2"
)

echo "=== SUBDOMAIN SCAN ==="
echo "# SUBDOMAIN SCAN" >> "$BACKUP_FILE"
for sub in "${SUBDOMAINS[@]}"; do
    fqdn="${sub}.${DOMAIN}"
    result=$(dig ${NS_TARGET} "$fqdn" A +noall +answer 2>/dev/null || true)
    cname=$(dig ${NS_TARGET} "$fqdn" CNAME +noall +answer 2>/dev/null || true)
    if [[ -n "$result" ]] || [[ -n "$cname" ]]; then
        echo "  FOUND: $fqdn"
        [[ -n "$result" ]] && echo "$result"
        [[ -n "$cname" ]] && echo "$cname"
        echo ""
        {
            echo "# $fqdn"
            [[ -n "$result" ]] && echo "$result"
            [[ -n "$cname" ]] && echo "$cname"
            echo ""
        } >> "$BACKUP_FILE"
    else
        echo "  (no record): $fqdn"
    fi
done

echo ""
echo "============================================================"
echo "  AUDIT COMPLETE"
echo "  Backup saved: $BACKUP_FILE"
echo "============================================================"

# Final summary
echo ""
echo "=== QUICK SUMMARY ==="
echo "Domain:      $DOMAIN"
echo "NS Query:    $NS_LABEL"
echo ""
echo "Nameservers:"
dig ${NS_TARGET} "$DOMAIN" NS +short 2>/dev/null | sed 's/^/  /'
echo ""
echo "A record(s):"
dig ${NS_TARGET} "$DOMAIN" A +short 2>/dev/null | sed 's/^/  /'
echo ""
echo "MX record(s):"
dig ${NS_TARGET} "$DOMAIN" MX +short 2>/dev/null | sed 's/^/  /'
echo ""
echo "Backup file: $BACKUP_FILE"
