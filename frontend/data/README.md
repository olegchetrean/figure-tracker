# Static snapshot — 2026-06-15

Frozen archive of `figureshift.vercel.app` after the Figure AI livestream ended (last reading: 2026-05-20).

Each JSON here mirrors what the corresponding `/api/*` endpoint returned on the live OVH backend (57.128.108.199) on 2026-06-15.

| file | source endpoint | size |
|---|---|---|
| analysis.json | `/api/analysis` | 375 KB |
| report.json   | `/api/report`   | (stub — AI analyst unavailable) |
| latest.json   | `/api/latest`   | last OCR reading |
| news.json     | `/api/news`     | news signals |
| costs.json    | `/api/costs`    | vision spend |
| events.json   | `/api/events?limit=30` | recent events |
| audit.json    | `/api/audit`    | (stub — endpoint was 500) |

To regenerate (if backend ever comes back up), run:

```bash
cd frontend/data
for ep in analysis report news costs latest "events?limit=30" audit; do
  name=$(echo "$ep" | sed 's/?.*//')
  curl -s "http://57.128.108.199/api/$ep" -o "$name.json"
done
```
