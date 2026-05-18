# Deploy frontend on Vercel — step-by-step

Backend (`uvicorn + OCR worker + scheduler + DBs`) **stays on infograph (57.128.108.199)** because Vercel cannot run a long-lived OCR pipeline. Vercel hosts only the static dashboard and proxies API calls to the OVH server through its edge network.

End-state:
- Frontend at `https://your-project.vercel.app` (HTTPS auto, CDN edge worldwide)
- API requests from the browser hit `https://your-project.vercel.app/api/...`
- Vercel rewrites them server-side to `http://57.128.108.199/api/...`
- Browser never sees the OVH IP or plaintext HTTP

## What's already in the repo

- `vercel.json` — region `fra1`, output dir `frontend/`, `/api/*` rewrite to OVH, security + cache headers
- `frontend/index.html`, `frontend/audit.html` — static, no build step needed

## Step 1 — push the repo to GitHub (1 min)

If not already on GitHub:
```bash
cd /Users/macbook_nou/Desktop/figure-tracker
git init && git add . && git commit -m "initial public deploy"
# Create empty repo on github.com/olegchetrean/figure-tracker
git remote add origin git@github.com:olegchetrean/figure-tracker.git
git push -u origin main
```

If you don't want to push the local DBs / cookies (you shouldn't), add to `.gitignore`:
```
backend/__pycache__/
*.pyc
data/
youtube_cookies.txt
.env
```

## Step 2 — connect to Vercel (2 min)

1. Go to https://vercel.com — sign in with GitHub
2. Click **New Project** → import the `figure-tracker` repo
3. Framework Preset: **Other**
4. Root Directory: `.` (repo root — `vercel.json` lives here)
5. Build & Output settings: leave defaults (Vercel reads `vercel.json`)
6. Click **Deploy**

First deploy finishes in ~20–40s. You get a URL like `figure-tracker-abc123.vercel.app`.

## Step 3 — pick your `*.vercel.app` subdomain (optional, 30s)

In Vercel project → **Settings → Domains**: rename to something cleaner, e.g. `figure-tracker.vercel.app` if available.

## Step 4 — verify

Open `https://figure-tracker.vercel.app/`:
- Dashboard loads, video embed plays
- DevTools → Network: requests to `/api/analysis` succeed (Vercel proxies them to OVH)
- HTTPS lock icon, no mixed-content warnings
- `https://figure-tracker.vercel.app/audit.html` also loads

## Step 5 — backend stays as-is on OVH

Nothing to change on `infograph`. The OCR worker keeps running, DBs keep growing, `/api/*` keeps responding. The only client now is Vercel's edge.

Optional hardening: restrict the firewall so port 80 only accepts traffic from Vercel's IP ranges. Vercel publishes them here: https://vercel.com/docs/edge-network/regions. Not urgent — UFW + rate-limiter already in place.

## Future revisions

Want a real domain later?
1. Buy `figureshift.xyz` (or similar)
2. In Vercel → Settings → Domains → Add Domain → paste `figureshift.xyz`
3. Vercel gives DNS records → set them at the registrar
4. Wait 1–5 min for propagation
5. HTTPS cert is automatic via Let's Encrypt

Cost: $0.99–$2/year for the domain, $0/month for Vercel hosting on free tier.

## What still loads from the OVH IP

Nothing user-facing. The browser only talks to Vercel. The OVH IP becomes "ours alone" and disappears from public view.

## Caveats

- Vercel free tier: 100GB bandwidth/month. We use maybe 1–2GB.
- Vercel serverless functions have execution limits — we don't use any (pure rewrites).
- If you ever tear down OVH, the dashboard breaks immediately (Vercel just proxies; it doesn't store data).
- The OCR worker, DBs, and provenance logs ALL live on OVH and must keep running.
