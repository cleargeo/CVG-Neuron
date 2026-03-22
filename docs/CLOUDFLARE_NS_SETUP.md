# CVG Cloudflare Custom Nameservers Setup
## Vanity Nameservers for clearviewgeographic.com, cleargeo.tech, cvg-nexus.com
### Classification: INTERNAL — PROPRIETARY

---

## Overview

We are setting up **Cloudflare-backed vanity (custom) nameservers** — meaning Cloudflare's global anycast DNS infrastructure serves all our DNS, but the nameservers appear under our own brand names:

| Domain | Nameserver 1 | Nameserver 2 |
|--------|-------------|-------------|
| `clearviewgeographic.com` | `ns1.clearviewgeographic.com` | `ns2.clearviewgeographic.com` |
| `cleargeo.tech` | `ns1.cleargeo.tech` | `ns2.cleargeo.tech` |
| `cvg-nexus.com` | `ns1.cvg-nexus.com` | `ns2.cvg-nexus.com` |

**Why Cloudflare:**
- Global anycast network — fastest DNS in the world
- DDoS protection built in
- Free SSL, WAF, analytics
- API-driven management
- Zero hardware to manage (vs. self-hosted BIND9)
- Free tier includes this capability

---

## Two Setup Modes

### Mode A: Zone-Level Custom NS (Free Plan — Recommended to Start)
Each domain uses NS names within its own zone:
- `cleargeo.tech` is served by `ns1.cleargeo.tech` / `ns2.cleargeo.tech`
- `clearviewgeographic.com` is served by `ns1.clearviewgeographic.com` / `ns2.clearviewgeographic.com`
- `cvg-nexus.com` is served by `ns1.cvg-nexus.com` / `ns2.cvg-nexus.com`

**Cost: FREE**

### Mode B: Account-Level Custom NS (Business Plan — Single NS pair for all domains)
Pick ONE domain (e.g., `clearviewgeographic.com`) for ALL nameserver names.
Every CVG domain would use `ns1.clearviewgeographic.com` / `ns2.clearviewgeographic.com`.

**Cost: $200/mo — do Mode A first, upgrade later if needed**

---

## Pre-Requisites

Before starting:
- [ ] All 3 domains must be **added to your Cloudflare account** (free)
- [ ] You must have access to each domain's **registrar** (HostGator or wherever they're registered)
- [ ] Have your **Cloudflare API Token** ready (needed for the automation script)
- [ ] Know which registrar holds each domain

**Get Cloudflare API Token:**
1. Cloudflare Dashboard → My Profile → API Tokens
2. Create Token → Use "Edit Zone DNS" template
3. Scope: All zones (or specific zones)
4. Save the token in CVG secrets vault

---

## Phase 1 — Add All 3 Domains to Cloudflare

For each domain:

1. Go to https://dash.cloudflare.com
2. Click **"Add a Site"**
3. Enter domain name (e.g., `cleargeo.tech`)
4. Choose plan: **Free** is fine
5. Cloudflare scans existing DNS records — **REVIEW AND CONFIRM** they match your HostGator zone
6. Cloudflare gives you generic NS like: `aria.ns.cloudflare.com` / `ken.ns.cloudflare.com`

> ⚠️ **DO NOT** change your registrar nameservers to Cloudflare's generic NS yet.
> We'll replace them with your vanity NS names in the next phases.

---

## Phase 2 — Enable Custom Nameservers per Zone

### For cleargeo.tech

1. In Cloudflare Dashboard → Select `cleargeo.tech` zone
2. Go to **DNS** → **Custom Nameservers**
3. Click **"Add Custom Nameservers"**
4. Enter:
   - `ns1.cleargeo.tech`
   - `ns2.cleargeo.tech`
5. Click **"Save"**
6. Cloudflare responds with **IP addresses** for each NS — example:
   ```
   ns1.cleargeo.tech → 108.162.193.X  (Cloudflare anycast IP — varies)
   ns2.cleargeo.tech → 172.64.33.X    (Cloudflare anycast IP — varies)
   ```
7. **Copy these IPs** — you need them for glue records

> NOTE: Cloudflare will automatically add A records for ns1 and ns2 in your zone.

### For clearviewgeographic.com

1. Select `clearviewgeographic.com` zone → **DNS** → **Custom Nameservers**
2. Add:
   - `ns1.clearviewgeographic.com`
   - `ns2.clearviewgeographic.com`
3. Copy the IPs Cloudflare assigns

### For cvg-nexus.com

1. Select `cvg-nexus.com` zone → **DNS** → **Custom Nameservers**
2. Add:
   - `ns1.cvg-nexus.com`
   - `ns2.cvg-nexus.com`
3. Copy the IPs Cloudflare assigns

---

## Phase 3 — Register Glue Records at Registrar

This is the **critical step** — glue records are A records at the registry level that tell the internet where to find your custom nameservers before they're active.

### For each domain, at the registrar:

**If registered at HostGator:**
1. Log in to HostGator → **Domains** section (billing area, not cPanel)
2. Find the domain → **Manage Domain**
3. Look for: **"Register Private Nameservers"** or **"Child Nameservers"**
4. Add:
   ```
   ns1.cleargeo.tech       → [IP from Cloudflare Step 2]
   ns2.cleargeo.tech       → [IP from Cloudflare Step 2]
   ```
5. Repeat for each domain at its registrar

> ⚠️ Glue records MUST be registered at the registrar that controls the parent zone. For `ns1.cleargeo.tech`, the glue goes in the `.tech` TLD registry — handled when you do it at your registrar.

**Verify glue records propagated:**
```bash
whois cleargeo.tech | grep -i "name server"
# After propagation you should see the IPs in WHOIS
```

---

## Phase 4 — Update Registrar Nameservers

**Only AFTER glue records are verified**, change each domain's nameservers at the registrar:

### cleargeo.tech
At registrar → Change Nameservers to:
```
ns1.cleargeo.tech
ns2.cleargeo.tech
```
(Remove: `ns1.hostgator.com`, `ns2.hostgator.com` or whatever was there)

### clearviewgeographic.com
At registrar → Change Nameservers to:
```
ns1.clearviewgeographic.com
ns2.clearviewgeographic.com
```

### cvg-nexus.com
At registrar → Change Nameservers to:
```
ns1.cvg-nexus.com
ns2.cvg-nexus.com
```

---

## Phase 5 — Verify Propagation

```bash
# Check each domain's NS records globally:
dig cleargeo.tech NS +short
dig clearviewgeographic.com NS +short
dig cvg-nexus.com NS +short

# Expected output for cleargeo.tech:
# ns1.cleargeo.tech.
# ns2.cleargeo.tech.

# Verify the NS names resolve to Cloudflare IPs:
dig ns1.cleargeo.tech A +short
dig ns2.cleargeo.tech A +short
# Should return the Cloudflare anycast IPs you registered
```

**External propagation check:**
- https://dnschecker.org/#NS/cleargeo.tech
- https://dnschecker.org/#NS/clearviewgeographic.com
- https://dnschecker.org/#NS/cvg-nexus.com

Propagation time: **15 minutes to 4 hours**

---

## Phase 6 — Enable Cloudflare Proxy (Orange Cloud)

Once DNS is working, enable Cloudflare proxy for your A records (the orange cloud):

In Cloudflare DNS for each zone:
- Click the grey cloud icon next to your A records → turns orange
- This enables DDoS protection, SSL, CDN, and WAF
- Your real server IPs are hidden behind Cloudflare

> ⚠️ Do NOT proxy:
> - Mail records (MX, mail.*, smtp.*)
> - Internal-only records
> - ns1/ns2 records (these must be DNS-only)

---

## Phase 7 — Cloudflare Settings to Apply to Each Zone

After NS cutover, configure these in the Cloudflare dashboard for each zone:

### SSL/TLS
- SSL/TLS → Mode: **Full (Strict)** if you have valid SSL on origin
- SSL/TLS → Edge Certificates: Enable **Always Use HTTPS**, **HSTS**

### Security
- Security → DDoS: Enable **DDoS Protection** (ON by default)
- Security → Bot Fight Mode: Enable
- Firewall Rules: Block traffic from known bad ASNs

### Speed
- Speed → Optimization: Enable **Auto Minify** (CSS/JS/HTML)
- Speed → Caching: Set **Browser Cache TTL** to 1 day

### DNS
- DNS → DNSSEC: **Enable DNSSEC** once NS is fully propagated
  (Cloudflare will give you a DS record to add at your registrar)

---

## Automation with Script

Use `scripts/cloudflare_dns_setup.py` to automate zone setup, DNS record creation, and verification via Cloudflare API:

```bash
# Set your API token:
export CLOUDFLARE_API_TOKEN="your_token_here"
export CLOUDFLARE_EMAIL="your_cloudflare_email"

# Setup all 3 zones:
python scripts/cloudflare_dns_setup.py --action setup-all

# Check status of custom NS:
python scripts/cloudflare_dns_setup.py --action ns-status

# List all records in a zone:
python scripts/cloudflare_dns_setup.py --action list-records --zone cleargeo.tech

# Add a DNS record:
python scripts/cloudflare_dns_setup.py --action add-record \
  --zone cleargeo.tech --type A --name git --value 1.2.3.4

# Verify propagation:
python scripts/cloudflare_dns_setup.py --action verify-propagation
```

---

## IP Address Reference (Fill in After Cloudflare Setup)

After you enable custom nameservers on each zone in Cloudflare, record the assigned IPs here:

```
cleargeo.tech:
  ns1.cleargeo.tech = __.__.__.__   (fill in from Cloudflare)
  ns2.cleargeo.tech = __.__.__.__   (fill in from Cloudflare)

clearviewgeographic.com:
  ns1.clearviewgeographic.com = __.__.__.__
  ns2.clearviewgeographic.com = __.__.__.__

cvg-nexus.com:
  ns1.cvg-nexus.com = __.__.__.__
  ns2.cvg-nexus.com = __.__.__.__
```

These IPs are Cloudflare anycast addresses — they route globally and never change once assigned.

---

## Quick Reference — Migration Day for cleargeo.tech

```
NOW:     Run scripts/dns_audit.ps1 (backup current HostGator records)
NOW:     Add cleargeo.tech to Cloudflare (free)
NOW:     Import all DNS records into Cloudflare zone
T-24h:   Reduce TTLs at HostGator to 300s
T-2h:    Enable Custom NS in Cloudflare → note assigned IPs
T-1h:    Register GLUE RECORDS at HostGator registrar with Cloudflare IPs
T=0:     Change registrar NS to ns1/ns2.cleargeo.tech
T+15m:   Monitor: dig cleargeo.tech NS +short
T+1h:    Verify all CVG services (git, neuron, web, mail)
T+24h:   Enable DNSSEC on Cloudflare + at registrar
T+7d:    Transfer domain registration away from HostGator to Cloudflare Registrar
         (Cloudflare Registrar = at-cost pricing, no markup)
```

---

## DNSSEC Setup (Do After NS Stable for 24h)

```
1. Cloudflare Dashboard → Zone → DNS → DNSSEC → Enable
2. Cloudflare gives you a DS record:
   DS record: Key Tag, Algorithm, Digest Type, Digest
3. At your registrar → Manage Domain → DNSSEC → Add DS Record
4. Enter the values from Cloudflare
5. Verify: dig cleargeo.tech DNSKEY +short
```

---

## Comparison: Cloudflare vs Self-Hosted BIND9

| Feature | Cloudflare Custom NS | Self-Hosted BIND9 |
|---------|---------------------|-------------------|
| Infrastructure | Zero (Cloudflare manages) | Requires 2+ servers |
| DDoS protection | Global anycast, built-in | DIY (FortiGate only) |
| Propagation | <1 min globally | Standard TTL |
| DNSSEC | One-click | Manual key management |
| API management | Full REST API | rndc + zone files |
| Cost | Free (DNS) | Server costs + maintenance |
| Vanity NS names | Yes (what we're doing) | Yes (native) |
| Analytics | Full query analytics | Requires setup |
| Failover | Automatic (Cloudflare HA) | Manual (AXFR secondary) |
| **Recommendation** | **Use this** | Fallback option |

---

*Setup Guide v1.0 | Created: 2026-03-22 | CVG / Neuron AI*
