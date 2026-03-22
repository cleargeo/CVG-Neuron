# CVG DNS Migration Playbook
## HostGator → Self-Hosted Authoritative DNS (BIND9)
### Domain: cleargeo.tech | Classification: INTERNAL — PROPRIETARY

---

## TABLE OF CONTENTS

1. [Executive Summary](#executive-summary)
2. [Current State Architecture](#current-state)
3. [Target State Architecture](#target-state)
4. [Pre-Migration Checklist](#pre-migration-checklist)
5. [Phase 1 — Audit & Inventory](#phase-1-audit--inventory)
6. [Phase 2 — Build Self-Hosted DNS](#phase-2-build-self-hosted-dns)
7. [Phase 3 — TTL Reduction & Staging](#phase-3-ttl-reduction--staging)
8. [Phase 4 — Registrar Cutover](#phase-4-registrar-cutover)
9. [Phase 5 — Post-Migration Verification](#phase-5-post-migration-verification)
10. [Rollback Plan](#rollback-plan)
11. [DNS Record Reference](#dns-record-reference)
12. [Infrastructure Reference](#infrastructure-reference)

---

## Executive Summary

We are fully migrating DNS authority for `cleargeo.tech` away from HostGator-managed nameservers (cPanel/WHM) to self-hosted authoritative BIND9 nameservers running on the CVG Hive infrastructure.

**Outcome:** Complete DNS independence from HostGator. All records managed via CVG DNS Support Engine (port 8810) and BIND9 zone files on our own hardware.

**Timeline estimate:** 48–72 hours total (24h TTL bleed-off + cutover window)

**Zero-downtime approach:** New nameservers are stood up and tested *before* the registrar cutover. The old HostGator NS remains active until propagation confirms clean resolution.

---

## Current State

```
Internet users
     │
     ▼
HostGator Nameservers (ns1.hostgator.com / ns2.hostgator.com)
     │   (registered at HostGator registrar)
     ▼
HostGator cPanel/WHM DNS Zone
     │  cleargeo.tech zone records
     ▼
CVG infrastructure (A records pointing back to our IPs)
```

**Nameservers in use today:**
- `ns1.hostgator.com`
- `ns2.hostgator.com`

**Registrar:** HostGator (same company — extra risk; they can lock domains)

---

## Target State

```
Internet users
     │
     ▼
Self-Hosted NS (ns1.cleargeo.tech / ns2.cleargeo.tech)
     │   (glue records registered at HostGator registrar)
     ▼
BIND9 Primary — cvg-stormsurge-01 (10.10.10.200 / PUBLIC_IP_PRIMARY)
     │  + BIND9 Secondary (axfr slave)
     ▼
CVG DNS Support Engine (port 8810) — zone management API
     │
     ▼
Same CVG infrastructure A/CNAME/MX records — no service change
```

**New authoritative nameservers:**
- `ns1.cleargeo.tech` → PUBLIC IP of cvg-stormsurge-01 (or DMZ host)
- `ns2.cleargeo.tech` → PUBLIC IP of secondary node (vm-454 or CT-104)

> **IMPORTANT:** `ns1` and `ns2` must have GLUE RECORDS registered at the HostGator registrar. Glue records are A records at the registry level that point `ns1.cleargeo.tech` to its IP *before* it's serving the zone — breaking the circular dependency.

---

## Pre-Migration Checklist

Before starting anything:

- [ ] Confirm public-facing IP(s) for ns1 and ns2 (static, with stable routing)
- [ ] Confirm firewall / FortiGate allows inbound **UDP/TCP 53** from internet to ns1 and ns2 IPs
- [ ] Confirm CVG DNS Support Engine is reachable at `http://10.10.10.200:8810/health`
- [ ] Export ALL current DNS records from HostGator cPanel (Zone Editor export)
- [ ] Save export to `docs/dns_records_hostgator_backup_YYYYMMDD.txt`
- [ ] Identify ALL subdomains in use (check git.cleargeo.tech, neuron.cleargeo.tech, mail records, etc.)
- [ ] Confirm MX records / email routing (do we use HostGator email or external?)
- [ ] Identify SPF/DKIM/DMARC TXT records (critical for email deliverability)
- [ ] Reduce all DNS TTLs to **300 seconds (5 min)** at HostGator — do this 24h before cutover
- [ ] Verify BIND9 is installable / Docker-able on target nodes

---

## Phase 1 — Audit & Inventory

### Step 1.1 — Export Current DNS Records from HostGator

**Via HostGator cPanel:**
1. Log in to HostGator cPanel → `Zone Editor`
2. Click `Manage` on `cleargeo.tech`
3. Use **`AXFR`** or **manual copy** to export all records
4. Save the full zone file

**Via CVG DNS Support Engine (if cPanel API key available):**
```bash
# From any CVG node or local machine:
curl http://10.10.10.200:8810/zones/cleargeo.tech/export \
  -H "X-API-Key: YOUR_KEY"
# This calls the cpanel_service.py to fetch records via WHM API
```

**Via dig (quick audit from any internet-connected machine):**
```bash
# Run scripts/dns_audit.sh cleargeo.tech
# Or manually:
dig @ns1.hostgator.com cleargeo.tech ANY +noall +answer
dig @ns1.hostgator.com cleargeo.tech MX +noall +answer
dig @ns1.hostgator.com cleargeo.tech TXT +noall +answer
dig @ns1.hostgator.com git.cleargeo.tech A +noall +answer
dig @ns1.hostgator.com neuron.cleargeo.tech A +noall +answer
dig @ns1.hostgator.com www.cleargeo.tech A +noall +answer
```

### Step 1.2 — Identify All Subdomains

Known subdomains (verify/expand this list):
| Subdomain | Type | Expected Target |
|-----------|------|----------------|
| `cleargeo.tech` | A | Main IP |
| `www.cleargeo.tech` | A/CNAME | Main IP |
| `git.cleargeo.tech` | A | Gitea server IP |
| `neuron.cleargeo.tech` | A | AI Neuron IP |
| `mail.cleargeo.tech` | MX/A | Mail server |
| `_dmarc.cleargeo.tech` | TXT | DMARC policy |
| `default._domainkey.cleargeo.tech` | TXT | DKIM key |

**Run the audit script to auto-discover:**
```bash
bash scripts/dns_audit.sh cleargeo.tech
```

### Step 1.3 — Document Current TTLs

Prior to reducing TTLs, record what they are. Most HostGator zones default to **14400s (4h)** or **86400s (24h)**. This tells you the maximum wait for old cache to expire after cutover.

---

## Phase 2 — Build Self-Hosted DNS

### Step 2.1 — Determine ns1 and ns2 Host Assignments

| Nameserver | BIND9 Role | Internal IP | Public IP |
|------------|-----------|-------------|-----------|
| `ns1.cleargeo.tech` | Primary (master) | 10.10.10.200 | **[CONFIRM: your WAN IP or DMZ IP]** |
| `ns2.cleargeo.tech` | Secondary (slave) | 10.10.10.x | **[CONFIRM: secondary WAN IP]** |

> If you only have ONE public IP, you can still run ns1 and ns2 on the same IP (same server, dual-listed) — many registrars allow this but it's not ideal for true redundancy. Better: use a VPS or cloud instance as ns2.
>
> **Recommended ns2 option:** Spin up a lightweight $4/mo VPS (Hetzner, Vultr, or Oracle Free Tier) as the secondary authoritative nameserver (AXFR slave from ns1).

### Step 2.2 — Install BIND9 on Primary (cvg-stormsurge-01)

**Option A: Docker (recommended — aligns with CVG stack)**
```bash
# On cvg-stormsurge-01 (10.10.10.200) as root/sudo:
mkdir -p /opt/cvg/bind/zones /opt/cvg/bind/config /opt/cvg/bind/log

# Create named.conf (see Step 2.4)
# Create zone file (see Step 2.5)

docker run -d \
  --name bind9-primary \
  --restart unless-stopped \
  -p 53:53/udp \
  -p 53:53/tcp \
  -v /opt/cvg/bind/config:/etc/bind \
  -v /opt/cvg/bind/zones:/var/lib/bind \
  -v /opt/cvg/bind/log:/var/log/named \
  internetsystemsconsortium/bind9:9.18
```

**Option B: Native BIND9**
```bash
apt-get update && apt-get install -y bind9 bind9utils bind9-doc
systemctl enable named
```

### Step 2.3 — Install BIND9 on Secondary (ns2)

Same Docker/apt install as above. The secondary will AXFR (zone transfer) from ns1 automatically.

```bash
# On ns2 node:
docker run -d \
  --name bind9-secondary \
  --restart unless-stopped \
  -p 53:53/udp \
  -p 53:53/tcp \
  -v /opt/cvg/bind/config:/etc/bind \
  internetsystemsconsortium/bind9:9.18
```

### Step 2.4 — BIND9 named.conf for Primary

Create `/opt/cvg/bind/config/named.conf`:
```
// CVG BIND9 Primary — ns1.cleargeo.tech
// Managed by CVG DNS Support Engine

options {
    directory "/var/cache/bind";
    recursion no;                  // authoritative only — no recursion
    allow-transfer { NS2_PUBLIC_IP; };  // allow AXFR to ns2 only
    listen-on { any; };
    listen-on-v6 { any; };
    dnssec-validation auto;
    auth-nxdomain no;
};

logging {
    channel default_log {
        file "/var/log/named/named.log" versions 3 size 5m;
        print-time yes;
        print-severity yes;
        print-category yes;
    };
    category default { default_log; };
    category queries { default_log; };
};

zone "cleargeo.tech" {
    type master;
    file "/var/lib/bind/cleargeo.tech.zone";
    allow-transfer { NS2_PUBLIC_IP; };
    notify yes;
};

// Internal management zone (keep BIND serving LAN too if needed)
// zone "cvg.local" { ... };
```

> Replace `NS2_PUBLIC_IP` with actual public IP of your ns2 server.

### Step 2.5 — BIND9 named.conf for Secondary

Create `/opt/cvg/bind/config/named.conf` on ns2:
```
// CVG BIND9 Secondary — ns2.cleargeo.tech

options {
    directory "/var/cache/bind";
    recursion no;
    listen-on { any; };
    dnssec-validation auto;
};

zone "cleargeo.tech" {
    type slave;
    masters { NS1_PUBLIC_IP; };
    file "/var/cache/bind/cleargeo.tech.zone";
};
```

### Step 2.6 — Create the Zone File

> See `config/bind9/cleargeo.tech.zone.template` for the full template.

Create `/opt/cvg/bind/zones/cleargeo.tech.zone` — populate with ALL records exported in Phase 1.

**Critical records required:**
```dns
; SOA record — Start of Authority
$ORIGIN cleargeo.tech.
$TTL 300

@   IN  SOA  ns1.cleargeo.tech. hostmaster.cleargeo.tech. (
            2026032201  ; Serial (YYYYMMDDNN — increment on every change)
            3600        ; Refresh (1h)
            900         ; Retry (15min)
            604800      ; Expire (7 days)
            300 )       ; Negative TTL (5min)

; Nameserver records
@   IN  NS   ns1.cleargeo.tech.
@   IN  NS   ns2.cleargeo.tech.

; Glue A records for nameservers (must match registrar glue records)
ns1 IN  A    NS1_PUBLIC_IP
ns2 IN  A    NS2_PUBLIC_IP

; === POPULATE FROM HOSTGATOR EXPORT BELOW ===
; All A, CNAME, MX, TXT, SRV records go here
```

### Step 2.7 — Firewall Rules (FortiGate)

Add policies on FortiGate to allow inbound DNS:
```
# Allow UDP/TCP 53 inbound to ns1 public IP (WAN → 10.10.10.200)
# Allow UDP/TCP 53 inbound to ns2 public IP
# Allow TCP 53 from ns2 to ns1 (for AXFR zone transfers)
```

Via FortiGate CLI:
```
config firewall policy
  edit 0
    set name "DNS-INBOUND-NS1"
    set srcintf "wan1"
    set dstintf "internal"
    set srcaddr "all"
    set dstaddr "ns1-vip"
    set service "DNS"
    set action accept
  next
end
```

> Create VIP (Virtual IP) for ns1 and ns2 public IPs → internal IPs (10.10.10.200 etc.)

---

## Phase 3 — TTL Reduction & Staging

### Step 3.1 — Reduce TTLs at HostGator (T-24 hours)

**24 Hours before cutover:**
1. Log in to HostGator cPanel → Zone Editor
2. Change TTL on ALL records to **300 seconds (5 minutes)**
3. Specifically critical: SOA TTL, @ A record, NS records, MX records

This ensures old cached DNS entries expire within 5 minutes after cutover rather than 4–24 hours.

### Step 3.2 — Test Your New DNS Servers

Before updating the registrar, test your BIND9 servers directly via IP:

```bash
# Test ns1 directly (using its IP):
dig @NS1_PUBLIC_IP cleargeo.tech SOA
dig @NS1_PUBLIC_IP cleargeo.tech NS
dig @NS1_PUBLIC_IP cleargeo.tech A
dig @NS1_PUBLIC_IP cleargeo.tech MX
dig @NS1_PUBLIC_IP cleargeo.tech TXT
dig @NS1_PUBLIC_IP git.cleargeo.tech A
dig @NS1_PUBLIC_IP neuron.cleargeo.tech A

# Test ns2 (zone transfer should have replicated):
dig @NS2_PUBLIC_IP cleargeo.tech A
dig @NS2_PUBLIC_IP cleargeo.tech MX
```

**All answers should match HostGator exactly before proceeding.**

### Step 3.3 — Validate with External Tools

- https://mxtoolbox.com/SuperTool.aspx — test your NS IPs directly
- https://dnschecker.org — global propagation check (use "Use Custom NS")
- https://intodns.com/cleargeo.tech — full zone health check

---

## Phase 4 — Registrar Cutover

### Step 4.1 — Register Glue Records at HostGator Registrar

> This is the MOST CRITICAL step. Do this BEFORE changing nameservers.

Glue records tie `ns1.cleargeo.tech` and `ns2.cleargeo.tech` to their IP addresses at the registry level (above HostGator's DNS).

**In HostGator Domains Dashboard:**

1. Log in to HostGator → **Domains** section (not cPanel — the account/billing section)
2. Find `cleargeo.tech` → **Manage Domain**
3. Look for **"Register Nameservers"** or **"Private Nameservers"** or **"Child Nameservers"**
4. Add:
   - `ns1.cleargeo.tech` → `NS1_PUBLIC_IP`
   - `ns2.cleargeo.tech` → `NS2_PUBLIC_IP`
5. Save / Submit

Wait for confirmation (usually immediate, but can take 5–15 minutes).

**Verify glue records were registered:**
```bash
whois cleargeo.tech | grep -i "name server"
# Should NOT show ns1.hostgator.com yet — but the glue IPs should be in WHOIS
```

### Step 4.2 — Update Nameservers at HostGator Registrar

Only after glue records are confirmed working:

1. In HostGator Domains Dashboard → `cleargeo.tech` → **Manage Domain**
2. **Change Nameservers** (or "Update Nameservers"):
   - Remove: `ns1.hostgator.com`, `ns2.hostgator.com`
   - Add: `ns1.cleargeo.tech`, `ns2.cleargeo.tech`
3. Save

> ⚠️ **Point of no return** — after clicking save, DNS begins using your servers. If your BIND9 is misconfigured, the domain will fail to resolve.

### Step 4.3 — Monitor Propagation

```bash
# Watch propagation globally:
watch -n 30 'dig cleargeo.tech NS +short'

# Expected output after propagation:
# ns1.cleargeo.tech.
# ns2.cleargeo.tech.
```

Full global propagation: **15 minutes to 4 hours** (with TTL at 300s).
WHOIS databases: **up to 24 hours**.

---

## Phase 5 — Post-Migration Verification

### Step 5.1 — Verify All Records Resolve Correctly

```bash
# Run the full verification:
bash scripts/dns_audit.sh cleargeo.tech --verify-against-backup docs/dns_records_hostgator_backup_YYYYMMDD.txt

# Or manually:
dig cleargeo.tech A +short
dig cleargeo.tech MX +short
dig cleargeo.tech TXT +short
dig git.cleargeo.tech A +short
dig neuron.cleargeo.tech A +short
dig www.cleargeo.tech A +short
```

### Step 5.2 — Verify Email Still Works

If you use email on cleargeo.tech:
```bash
# Check MX records resolve:
dig cleargeo.tech MX +short

# Verify SPF:
dig cleargeo.tech TXT +short | grep spf

# Verify DKIM:
dig default._domainkey.cleargeo.tech TXT +short

# Send a test email to an external address and verify delivery
```

### Step 5.3 — Check Zone Health

```bash
# From ns1 server — check zone is loaded:
docker exec bind9-primary rndc status
docker exec bind9-primary rndc zonestatus cleargeo.tech

# Check zone transfer on ns2:
docker exec bind9-secondary rndc zonestatus cleargeo.tech
```

### Step 5.4 — Raise TTLs Back

After 24–48 hours of clean operation:
1. Edit `/opt/cvg/bind/zones/cleargeo.tech.zone`
2. Change `$TTL 300` to `$TTL 3600` (or `86400` for very stable records)
3. Increment the SOA serial number
4. Reload BIND9: `docker exec bind9-primary rndc reload cleargeo.tech`

### Step 5.5 — Update CVG Neuron Semantic Memory

```bash
# Tell Neuron about the migration:
curl -s http://localhost:11434/api/memory/learn -d '{
  "fact": "cvg.dns.external = self-hosted BIND9 ns1.cleargeo.tech / ns2.cleargeo.tech — migrated from HostGator on 2026-MM-DD",
  "category": "infrastructure"
}'
```

---

## Rollback Plan

If anything goes wrong during Phase 4, rollback is straightforward:

### Immediate Rollback (within propagation window):

1. Go to HostGator Domains → `cleargeo.tech`
2. Change nameservers back to:
   - `ns1.hostgator.com`
   - `ns2.hostgator.com`
3. Save

Because TTLs are at 300s, recovery time is ~5–10 minutes.

### If HostGator Account is Inaccessible:

Contact HostGator support and provide domain authorization codes. This is why we keep the HostGator account credentials in the CVG secrets vault.

### Pre-Cutover Safety:
- Do NOT delete any HostGator DNS records during the migration
- Do NOT change HostGator cPanel records after reducing TTLs
- The HostGator zone should remain fully intact as a fallback

---

## DNS Record Reference

### Known Records (verify from live export)

```dns
; ==========================================
; cleargeo.tech — Authoritative Zone File
; Last updated from HostGator: [DATE]
; ==========================================

$ORIGIN cleargeo.tech.
$TTL 3600

; SOA
@  IN  SOA  ns1.cleargeo.tech. hostmaster.cleargeo.tech. (
            2026032201   ; Serial
            3600         ; Refresh
            900          ; Retry
            604800       ; Expire
            300 )        ; Min TTL

; Nameservers
@   IN  NS   ns1.cleargeo.tech.
@   IN  NS   ns2.cleargeo.tech.
ns1 IN  A    [NS1_PUBLIC_IP]
ns2 IN  A    [NS2_PUBLIC_IP]

; Main domain
@   IN  A    [MAIN_IP]
www IN  A    [MAIN_IP]
; OR:
; www IN CNAME @

; CVG Services
git     IN  A    [GITEA_IP]
neuron  IN  A    [NEURON_IP]

; Mail
@   IN  MX  10  mail.cleargeo.tech.
mail    IN  A    [MAIL_IP]

; Email authentication
@   IN  TXT  "v=spf1 ip4:[MAIL_IP] ~all"
_dmarc  IN  TXT  "v=DMARC1; p=none; rua=mailto:postmaster@cleargeo.tech"
; DKIM: copy key from HostGator export
; default._domainkey  IN  TXT  "v=DKIM1; k=rsa; p=[KEY]"
```

> **Action required:** Fill in all `[...]` placeholders with actual values from the HostGator export.

---

## Infrastructure Reference

| Component | Location | Address | Port |
|-----------|----------|---------|------|
| Primary BIND9 (ns1) | cvg-stormsurge-01 | 10.10.10.200 | 53 |
| Secondary BIND9 (ns2) | TBD | TBD | 53 |
| CVG DNS Support Engine | cvg-stormsurge-01 | 10.10.10.200 | 8810 |
| FortiGate (internal DNS) | Hive-0 | 10.10.10.1 | 53 |
| HostGator Registrar | external | — | web |
| Audit VM | vm-220 | 10.10.10.220 | — |

### CVG DNS Support Engine API

The CVG DNS Support Engine (`G:\07_APPLICATIONS_TOOLS\CVG_DNS_SupportEngine`) manages BIND9 zones via REST API on port **8810**:

```bash
# Health check
curl http://10.10.10.200:8810/health

# List zones
curl http://10.10.10.200:8810/zones

# Get all records in zone
curl http://10.10.10.200:8810/zones/cleargeo.tech/records

# Add a record
curl -X POST http://10.10.10.200:8810/zones/cleargeo.tech/records \
  -H "Content-Type: application/json" \
  -d '{"name":"test", "type":"A", "value":"1.2.3.4", "ttl":300}'

# Export zone from cPanel (before migration)
curl http://10.10.10.200:8810/zones/cleargeo.tech/export

# Reload BIND9
curl -X POST http://10.10.10.200:8810/zones/cleargeo.tech/reload
```

---

## Quick Reference — Migration Day Sequence

```
T-24h:  Reduce ALL HostGator DNS TTLs to 300s
T-12h:  Export full zone from HostGator, save backup
T-12h:  Deploy BIND9 on ns1 and ns2
T-12h:  Create zone file with ALL records from export
T-6h:   Test BIND9 by querying ns1/ns2 IPs directly
T-6h:   Verify ALL records match HostGator exactly
T-1h:   Register GLUE RECORDS at HostGator registrar
        (ns1.cleargeo.tech → NS1_PUBLIC_IP)
        (ns2.cleargeo.tech → NS2_PUBLIC_IP)
T=0:    Update registrar nameservers to ns1/ns2.cleargeo.tech
T+15m:  Monitor: dig cleargeo.tech NS +short
T+1h:   Verify all services (web, git, neuron, mail)
T+24h:  Confirm everything clean, raise TTLs to 3600
T+48h:  Update Neuron semantic memory with migration complete
T+7d:   Consider domain transfer AWAY from HostGator registrar
        to a neutral registrar (Cloudflare Registrar, Namecheap)
```

---

## Domain Transfer Recommendation

Since HostGator is both our **registrar** AND was our **DNS provider**, there is still a dependency. Once DNS is migrated, consider also transferring the **domain registration** to a neutral registrar:

1. **Cloudflare Registrar** (at-cost pricing, no markup, excellent API)
2. **Namecheap** (reliable, good UI)
3. **Porkbun** (cheapest .tech pricing)

Steps for domain transfer occur AFTER DNS is fully migrated and verified (wait at least 7 days). Domain transfers require an **EPP/Auth code** from HostGator and a 60-day unlock.

---

*Playbook version: 1.0 | Created: 2026-03-22 | Author: CVG / Neuron AI*
*Next review: After migration completion*
