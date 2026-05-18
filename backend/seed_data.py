"""
Pre-loads all known historical data points into the database.
Run once after init: python seed_data.py
"""
from database import init_db, insert_reading

# (stream_hours, packages, is_split, human_packages, robot_packages, shift_hours, source)
# stream_hours computed from HH:MM:SS  →  H + M/60 + S/3600
SEED = [
    # ── Day 1 user screenshots (early stream, no robot identity sticker visible) ─
    (0.0339,  34,      False, None,  None,  None,  "screenshot"),  # 00:02:02 — stream just started
    (2.6706,  3_673,   False, None,  None,  None,  "screenshot"),  # 02:40:14
    (7.0442,  9_126,   False, None,  None,  None,  "screenshot"),  # 07:02:39
    (7.8378,  10_119,  False, None,  None,  None,  "screenshot"),  # 07:50:16 — FRANK on sticker

    # ── Research / news sources ──────────────────────────────────────────────
    (7.733,   10_000,  False, None,  None,  None,  "research"),   # Gary solo, 7h44m
    (17.0,    22_000,  False, None,  None,  None,  "research"),   # eWeek
    (22.0,    28_000,  False, None,  None,  None,  "research"),   # Adcock tweet
    (24.0,    30_208,  False, None,  None,  None,  "research"),   # HumaniodsDaily screenshot
    (26.0,    33_137,  False, None,  None,  None,  "research"),   # HumaniodsDaily
    (30.0,    38_000,  False, None,  None,  None,  "research"),   # Bloomberg / Ground.news
    (33.0,    40_000,  False, None,  None,  None,  "research"),   # DigitalPhablet / NenPower
    (40.0,    50_000,  False, None,  None,  None,  "research"),   # TechRepublic / multiple (~40h ~50k)
    (50.0,    60_000,  False, None,  None,  None,  "research"),   # Bloomberg CEO statement
    (72.0,    88_000,  False, None,  None,  None,  "research"),   # CryptoBriefing
    (81.0,   101_391,  False, None,  None,  None,  "research"),   # Seoul Economic Daily (exact)

    # ── Screenshots provided by user ─────────────────────────────────────────
    # 86:02:50  → 86 + 2/60 + 50/3600 = 86.0472
    (86.047,  107_257,  False, None,  None,  None,  "screenshot"),  # BOB

    # 91:10:06  → 91 + 10/60 + 6/3600 = 91.1683
    (91.168,  113_418,  False, None,  None,  None,  "screenshot"),  # BOB

    # 95:03:36  → 95 + 3/60 + 36/3600 = 95.0600
    (95.060,  117_945,  False, None,  None,  None,  "screenshot"),  # JIM

    # 97:13:26  → 97.2239   Shift 8:46:44 remaining of 9h → elapsed = 0:13:16 = 0.2211h
    # Human intern: 1,816 pkgs  |  F.03 ROSE: 1,419 pkgs
    (97.224,  120_482,  True,  1_816, 1_419, 0.221, "screenshot"),  # ROSE split-screen

    # 97:55:35  → 97.9264   Shift 8:04:36 remaining → elapsed = 0:55:24 = 0.9233h
    # Human: 2,819  |  Robot: 2,284
    (97.926,  121_347,  True,  2_819, 2_284, 0.923, "screenshot"),  # ROSE split-screen

    # 97:59:02  → 97 + 59/60 + 2/3600 = 97.9839h
    # The "FULL WORK SHIFT" overlay shows REMAINING time 8:01:08 of a 9h shift,
    # so elapsed = 9 - 8.019 = 0.981h.  Originally seeded as 8.019 (raw) — wrong.
    # Human intern: 2,904  |  F.03: 2,368
    (97.984,  121_431,  True,  2_904, 2_368, 0.981, "screenshot"),  # user screenshot Day 5
]


def seed():
    init_db()
    inserted = 0
    for row in SEED:
        sh, pkgs, is_split, human, robot, shift, src = row
        insert_reading(
            stream_hours=sh,
            packages=pkgs,
            is_split=is_split,
            human_packages=human,
            robot_packages=robot,
            shift_hours=shift,
            source=src,
        )
        inserted += 1
    print(f"Seeded {inserted} historical data points.")


if __name__ == "__main__":
    seed()
