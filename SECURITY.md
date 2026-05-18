# Figure Tracker — Security Posture

## 1. Where secrets live (and where they DON'T)

| Secret | Stored at | Permission | In repo? | In logs? |
|---|---|---|---|---|
| `LITELLM_API_KEY` | `/opt/figure-tracker/.env` on server | `600` ubuntu | **No** | No |
| `ADMIN_TOKEN` | `/opt/figure-tracker/.env` on server | `600` ubuntu | **No** | No |
| YouTube cookies | `/opt/figure-tracker/youtube_cookies.txt` | `600` ubuntu | **No** | No |
| OpenAI usage | only token *counts*, never the key | n/a | n/a | yes (counts only) |

**Verification commands** (re-run any time):

```bash
# Repo has zero hardcoded secrets:
grep -rEn "sk-[a-zA-Z0-9]{20,}|api[_-]?key.*=.*['\"][^'\"]{15,}" \
  --include='*.py' --include='*.html' --include='*.sh' \
  /Users/macbook_nou/Desktop/figure-tracker

# Server file perms are tight:
ssh infograph 'stat -c "%a %n" /opt/figure-tracker/.env \
                                /opt/figure-tracker/youtube_cookies.txt \
                                /opt/figure-tracker/data/*.db'
# Expected: 600 .env  600 youtube_cookies.txt  640 data/*.db

# Admin token never appears in logs:
ssh infograph 'sudo journalctl -u figure-tracker-api --since "1 day ago" --no-pager | \
               grep -iE "admin.?token|sk-[a-z]" | head'
# Expected: (empty)
```

## 2. HTTP-level protections (applied by `main.py` middleware)

Every API/static response now ships with:

| Header | Value | Why |
|---|---|---|
| `Content-Security-Policy` | strict allow-list of self + jsdelivr + Google Fonts + YouTube | Blocks injected scripts, frames, fonts |
| `X-Content-Type-Options` | `nosniff` | Prevents MIME-type sniffing attacks |
| `X-Frame-Options` | `SAMEORIGIN` | Prevents the dashboard being iframed by attackers |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | Don't leak admin-token-bearing URLs in `Referer` |
| `Permissions-Policy` | `geolocation=(), microphone=(), camera=()` | Disallow access to device APIs |
| `Cache-Control` on HTML | `no-store, must-revalidate` | Always fetch fresh HTML; clients won't get stale CSP |

### Rate limiter (per-IP)

In-memory token bucket: **60 req burst, refill 1/sec sustained** for any `/api/*` path. Returns `429 Too Many Requests` with `Retry-After: 5` when exhausted. Sufficient for the dashboard's normal traffic; will throttle a casual scraper without affecting real users.

Tested: 60 consecutive curls = `200`, calls 61-70 = `429`.

## 3. Endpoint surface — who can read what

| Endpoint | Auth | Content sensitivity |
|---|---|---|
| `GET /api/analysis` | none | aggregate only — safe to expose |
| `GET /api/audit` | none | drill-down samples, no full dump |
| `GET /api/report` | none | hourly AI summary text |
| `GET /api/latest` | none | one row, current |
| `GET /api/news` | none | external sources, sanitised at ingest |
| `GET /api/events` | none | semantic-event log, no secrets |
| `GET /api/costs` | none | token totals + projection |
| `GET /api/health` | none | service status |
| **`GET /api/history`** | **`X-Admin-Token` header required** | full raw readings dump |

The admin token is the only thing you must keep private. Anything below it is intentionally public.

### Verify the gate is on

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://SITE/api/history          # → 401
curl -s -o /dev/null -w "%{http_code}\n" -H "X-Admin-Token: WRONG" .../api/history  # → 401
curl -s -o /dev/null -w "%{http_code}\n" -H "X-Admin-Token: $REAL" .../api/history  # → 200
```

## 4. Data ingested from the public internet is sanitised

Reddit and HackerNews are external sources. Before storing:

- URLs go through `_sanitise_url()` → must start with `http://` or `https://`, length ≤ 2048. `javascript:`, `data:`, `file:` are dropped to `None`.
- Titles / authors / snippets go through `_sanitise_text()` → strips control characters (`< 0x20` except `\n`), truncates to a bounded length.
- The frontend then `escapeHtml()`s every value before rendering — defense in depth.

XSS path is **closed** by belt-and-braces: bad scheme rejected at ingest, bad characters rejected at storage, all rendered values escaped on output.

## 5. Database file safety

| DB | Purpose | Permission | Backed up? |
|---|---|---|---|
| `tracker.db` | readings + AI reports + costs + external signals + events | `640` ubuntu:ubuntu | not yet |
| `provenance.db` | metric snapshots + influence log | `640` ubuntu:ubuntu | not yet |

The `data/` dir is owned by `ubuntu`. The service runs as `ubuntu`, so it can read/write. Nobody else on the box can.

**Recommended next step**: nightly snapshot via `cron`:

```bash
0 3 * * *  /usr/bin/sqlite3 /opt/figure-tracker/data/tracker.db ".backup '/opt/figure-tracker/data/backups/tracker-$(date +\%Y\%m\%d).db'" && find /opt/figure-tracker/data/backups -mtime +14 -delete
```

## 6. Known gaps you should fix before going public on a domain

1. **No HTTPS yet** — currently `http://57.128.108.199`. Once you pick a domain, install **Caddy** or **nginx + certbot** for free Let's Encrypt cert. Until then your `ADMIN_TOKEN` travels in plaintext when you use `/api/history`.

   Quick Caddy config (one file `/etc/caddy/Caddyfile`):
   ```caddy
   figureshift.xyz {
       reverse_proxy localhost:8000
       encode gzip
       header -Server
   }
   ```

2. **No request logging persistence** — journald rotates by default. If you need an audit trail of admin-token usage, add structured logging to a file.

3. **No 2FA on the admin token** — single-secret bearer auth. If high-value: rotate it monthly via `openssl rand -hex 24 > /opt/figure-tracker/.env` snippet.

4. **No backup of `provenance.db`** — see §5.

5. **SSH key rotation** — your deploy user is `ubuntu`. Audit `~/.ssh/authorized_keys` on the server periodically.

## 7. Threat model (what we're protected against)

| Threat | Status |
|---|---|
| Casual scraper grabs raw data | **Blocked** — `/api/history` requires token, no Export CSV button, rate-limited |
| XSS via news / robot names | **Blocked** — scheme allow-list + control-char strip + frontend escape |
| Clickjacking | **Blocked** — `X-Frame-Options: SAMEORIGIN` |
| MIME confusion | **Blocked** — `X-Content-Type-Options: nosniff` |
| 3rd-party script injection | **Blocked** — strict CSP |
| Referer leak of admin token | **Blocked** — `Referrer-Policy: strict-origin-when-cross-origin` |
| Burst DoS | **Mitigated** — per-IP rate limit, 429 after 60 burst |
| Filesystem read by non-owner | **Blocked** — `600` `.env`, `640` DBs |
| Secret in git history | **N/A** — not a git repo, no commits to leak |
| Token sniff on the wire | **Open** — needs HTTPS via Caddy/nginx + LE cert |
| Sustained DoS from many IPs | **Open** — would need Cloudflare in front |

## 8. What you should do once per month

1. Run the verification commands in §1 — make sure no secrets crept into the repo
2. `ls -la /opt/figure-tracker/.env` — confirm still `600`
3. `journalctl -u figure-tracker-api --since "30 days ago" | grep -i "admin\|sk-"` — confirm clean
4. Rotate `ADMIN_TOKEN`:
   ```bash
   ssh infograph 'NEW=$(openssl rand -hex 24); \
     sed -i "s/^ADMIN_TOKEN=.*/ADMIN_TOKEN=$NEW/" /opt/figure-tracker/.env && \
     sudo systemctl restart figure-tracker-api && echo "new token: $NEW"'
   ```
   Save the new token in 1Password.

5. Check there are no new `429`-storm IPs (could indicate a scraper):
   ```bash
   ssh infograph 'sudo journalctl -u figure-tracker-api --since "1 day ago" | grep "429"'
   ```
