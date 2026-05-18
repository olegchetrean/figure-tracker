# 100-Step Plan — Community Polls System

**Goal**: A friendly, no-money, no-betting prediction system where viewers vote on Figure-AI-related outcomes (e.g., "Will the robot pass 150K packages in the next 24h?"). Pure entertainment + community sentiment, NO stakes.

**Scope**: Backend + frontend + auto-generated polls + auto-resolution + leaderboard. Anonymous voters (browser fingerprint, no signup required).

---

## Phase 1 — Data Foundation (steps 1–10)

1. Create new SQLite tables in `tracker.db`: `polls`, `poll_options`, `poll_votes`, `poll_voters`.
2. Schema `polls`: `id, slug, title, kind, status, opens_at, closes_at, resolved_at, correct_option_id, source, metadata_json, created_at`.
3. Schema `poll_options`: `id, poll_id, label, order_idx, target_value, metadata_json`.
4. Schema `poll_votes`: `id, poll_id, option_id, voter_id, voted_at`, with `UNIQUE(poll_id, voter_id)` to prevent double-vote.
5. Schema `poll_voters`: `voter_id (UUID), first_seen_at, last_seen_at, vote_count, correct_count`.
6. Indexes: `idx_polls_status`, `idx_votes_poll`, `idx_votes_voter`, `idx_polls_closes_at`.
7. Migration file `infra/migrations/002_polls.sql` so fresh deploys recreate schema.
8. Stdlib-only module `backend/polls_db.py` exposing the table ops as functions.
9. Add `init_polls_db()` to `main.py` startup, alongside the existing `init_*` calls.
10. Unit test that an empty DB inserts a poll + an option + a vote correctly.

## Phase 2 — Anonymous voter identity (steps 11–18)

11. Issue a `voter_id` UUID v4 per browser, stored in a `localStorage` key + a server-set `HttpOnly` cookie.
12. Backend endpoint `GET /api/polls/whoami` returns the current `voter_id`; creates one on first hit.
13. Cookie attributes: `SameSite=Lax`, 1-year max-age, no PII collected.
14. Display the short voter id (first 6 chars) in the UI so users see "you're voting as `7f3a2c·…`".
15. Provide `POST /api/polls/forget` to clear the voter id (button in UI).
16. Rate-limit per voter: max 1 vote per poll, max 50 polls per hour.
17. Log voter_id but never any user-agent / IP combination, to preserve anonymity.
18. Document the anonymity model in `SECURITY.md` + visible footer disclaimer.

## Phase 3 — Poll types (steps 19–28)

19. Type `yes_no`: simple binary, two options (Yes / No).
20. Type `multi_choice`: 2–6 mutually-exclusive options (e.g., which robot today).
21. Type `range_bucket`: numeric ranges (e.g., "Peak rate this week: 1300-1500 / 1500-1700 / 1700-1900 / 1900+").
22. Type `over_under`: a single threshold; user picks "OVER" or "UNDER".
23. Type `time_window`: a time slot (e.g., "When does 200K packages hit?" with hour-buckets).
24. Type `open_text` (deferred): free-text guess, max 80 chars. Curated manually.
25. Each type maps to a tiny renderer component in the frontend.
26. JSON validation per type in `polls_db.create_poll()`.
27. Reject creates where `opens_at >= closes_at` or `closes_at` is in the past.
28. Polls have an explicit `auto_resolve` flag indicating if a metric-based resolver will close it.

## Phase 4 — Public read APIs (steps 29–38)

29. `GET /api/polls/active` — currently open polls, with current vote tallies + percentages.
30. `GET /api/polls/upcoming` — polls scheduled but not yet opened.
31. `GET /api/polls/resolved?limit=20` — recently resolved polls.
32. `GET /api/polls/<slug>` — detail view for one poll.
33. Responses include vote count per option + total + percent (compute server-side).
34. Cache the active list for 5 seconds to absorb high traffic.
35. Show "your vote" annotation when the request has a known `voter_id` cookie.
36. Sort active polls by `closes_at` ascending (soonest closing first).
37. Hide raw `voter_id` from any public response — only aggregates leak out.
38. Light pagination on `/resolved` (limit + cursor by `resolved_at`).

## Phase 5 — Vote write API (steps 39–46)

39. `POST /api/polls/<slug>/vote` body `{option_id}` returns updated tallies.
40. Enforce poll status == "open" and `closes_at > now()`.
41. Reject when `voter_id` already voted on this poll (HTTP 409, structured error).
42. Allow vote update only if poll allows it (boolean `allow_change` in metadata).
43. On vote, write to `poll_votes`, update voter counters, emit a structured log line.
44. Return JSON with updated percentages so UI can animate instantly.
45. Validate `option_id` belongs to the poll referenced.
46. Add CSRF protection by requiring `Origin` header to match site host (relaxed on localhost for dev).

## Phase 6 — Auto-generated poll templates (steps 47–58)

47. Template "Will the robot pass package milestone X in next N hours?" auto-spawns when current_pkgs / X >= 0.7.
48. Template "Which robot will work most in the next shift?" spawns when a phase change to `solo_active` is detected.
49. Template "Peak rate this week" rolls weekly (closes Sundays UTC).
50. Template "Will robot beat human in the next split_challenge?" spawns at the moment is_split becomes true.
51. Template "Next new robot name?" — open multi_choice with KNOWN_ROBOT_NAMES minus those already met.
52. Template "Will today's 24h rate exceed yesterday's?" rolls every UTC midnight.
53. Template "Will any dock event happen in next 6h?" yes/no.
54. Template "Will stream pass 200h within next 7 days?" range-bucketed.
55. Generator module `backend/poll_generator.py` runs every 15 minutes via APScheduler.
56. Each template includes resolution criteria as code (lambda) that runs at `closes_at`.
57. Idempotency: the generator checks for an existing open poll with the same `slug` before creating.
58. Skip generation when the underlying metric is "insufficient_data".

## Phase 7 — Auto-resolution (steps 59–68)

59. Scheduler job `_resolve_polls` runs every minute; iterates polls where `closes_at <= now AND status='open'`.
60. For each, evaluate the resolution criteria stored at creation time (registered by template id).
61. Set `correct_option_id`, status `resolved`, `resolved_at`.
62. For each voter who picked the correct option, increment `correct_count`.
63. Polls that can't be auto-resolved (e.g., "open_text") flip to `awaiting_manual` status.
64. Admin endpoint `POST /api/polls/<id>/resolve` (requires `ADMIN_TOKEN`) for manual resolution.
65. Resolution emits a structured log line + appends to a `poll_resolution_log` table.
66. Notify the page with a small flash banner when a poll the visitor voted in resolves.
67. After resolution, freeze the poll — no more votes accepted.
68. Display correct option visually with a green ring in the UI.

## Phase 8 — Voter scoring & leaderboard (steps 69–76)

69. Voter accuracy = `correct_count / vote_count`, computed on read.
70. Show your own accuracy + vote total + correct count in a small "Your scorecard" card.
71. Top-10 leaderboard by accuracy, gated by `vote_count >= 5` to avoid noise.
72. Anonymized leaderboard: only show short voter_id like `7f3a2c…`.
73. Track streaks: consecutive correct predictions; show as a "🔥 streak" badge.
74. Endpoint `GET /api/polls/leaderboard` for the table.
75. Optional friendly display name: voters can set a non-unique nickname stored locally only.
76. Show nickname as the leaderboard primary key when set, voter_id short as fallback.

## Phase 9 — Frontend UI shell (steps 77–86)

77. New section "Community Polls" between the Achievements panel and Patterns & Predictions.
78. A horizontal scroll of "active poll" cards, soonest-closing first.
79. Each card: title + countdown to close + options as click-to-vote buttons.
80. After a click, lock the card and show live percentages with animated bars.
81. Resolved polls in a separate "Recently Resolved" carousel; shows winner + your pick.
82. Each card has a `tier` color: yes_no = blue, multi = orange, range = green, etc.
83. Long-running polls (week+) get a "weekly" badge.
84. Mobile: cards stack vertically with full-width buttons.
85. Visual states for: open / your-vote-locked / closing-soon (<1h pulse) / resolved.
86. Accessibility: each card is a `<form>` with proper labels; results read via aria-live.

## Phase 10 — Live updates without polling spam (steps 87–93)

87. Re-fetch `/api/polls/active` every 30s in the dashboard's existing poll cycle (separate from /api/analysis).
88. Stale-while-revalidate: show last known tallies immediately, then update.
89. On vote success, optimistically render the new percentages before the server response arrives.
90. Animate vote bars with the same `setLiveValue` ease-out we already use elsewhere.
91. Highlight `NEW` ribbon on polls created in the last 10 minutes.
92. Visual cue when a poll resolves: subtle glow ring matching the correct option's color.
93. Settings toggle to mute resolution flashes for users who find them noisy.

## Phase 11 — Hardening & polish (steps 94–100)

94. Add unit tests covering: vote idempotency, double-vote rejection, auto-resolution math, leaderboard ordering.
95. Add an admin page (gated by `ADMIN_TOKEN`) to inspect raw polls + force-resolve.
96. Rate-limit `POST /vote` to 1 per second per voter, return 429 on burst.
97. Add per-poll comments section? **No** — keeps the system anonymous and toxicity-free.
98. Document the entire poll system in `POLLS.md` with screenshots of each state.
99. Add disclaimer in the polls section: "Polls are entertainment only; no money or rewards. Anonymous, no signup. Your vote is local to this browser unless you cleared cookies."
100. Final pass: a11y audit (keyboard navigation through cards, screen-reader labels), responsive QA on mobile/tablet/desktop, deploy.

---

## Implementation tactics

- **Storage**: keep everything in the existing `tracker.db` (one more set of tables) — no separate service. Backups already cover it.
- **Anonymity**: never join `voter_id` with any IP / UA / location. The only identifier is the voter's own UUID that lives in their browser.
- **No money / no stakes**: explicit in the disclaimer, in the UI copy, and in `POLLS.md`. The system is engagement + sentiment data, nothing more.
- **Auto-poll cadence**: 15 minutes between generator runs, so we don't drown the page in too many simultaneous polls. Cap at 8 active polls at any moment.
- **Cost**: zero LLM tokens. Resolution is rule-based against existing analysis fields. Total marginal cost: ~10–50KB of DB growth per day from votes.

## What we'll see on the page after rollout

```
COMMUNITY POLLS — vote on outcomes (entertainment only)

[Active poll cards — 5 visible, scroll for more]
  ┌──────────────────────────────────┐  ┌──────────────────────────────────┐
  │ Will pkgs cross 150K in 12h?     │  │ Which robot logs most h today?   │
  │ closes in 1h 23m · 86 votes      │  │ closes in 5h 12m · 142 votes     │
  │ [ YES — 71% ] [ NO — 29% ]       │  │ BOB 38% · ROSE 33% · FRANK 20%   │
  │                                  │  │ JIM 6% · GARY 3%                 │
  └──────────────────────────────────┘  └──────────────────────────────────┘

YOUR SCORECARD
  voter_id: 7f3a2c… · 8/12 correct (66.7%) · 🔥 streak 3
```
