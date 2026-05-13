# Venue Duplicates Report

**Generated:** 2026-05-06 (production DB — audit only, no changes made)
**Duplicate groups found:** 5
**Venues to delete (after merge):** 5
**Events to be reassigned:** 15

> ⚠️ **Audit only.** No data has been modified. Review each group and confirm before executing.

---

## Group 1 — `barderene`

| | ID | Name | Upcoming | Total | Images |
|---|---|---|---|---|---|
| ✅ **KEEP** | 464 | Barderene | 29 | 30 | none |
| ❌ **DELETE** | 354 | Bar de Rene | 4 | 4 | none |

**Action:** Reassign 4 events from id=354 → id=464, then delete id=354.

⚠️ **Name warning:** Canonical (id=464) has slug-style name `"Barderene"` while id=354 has the readable `"Bar de Rene"`. After merging, also run: `UPDATE venues SET name = 'Bar de Rene' WHERE id = 464`.

---

## Group 2 — `estadiobicentenariolaflorida`

| | ID | Name | Upcoming | Total | Images |
|---|---|---|---|---|---|
| ✅ **KEEP** | 427 | Estadio Bicentenario la Florida | 1 | 2 | none |
| ❌ **DELETE** | 69 | Estadio Bicentenario, La Florida | 0 | 3 | **✓ HAS IMAGES** |

**Action:** Reassign 3 events from id=69 → id=427, then delete id=69.

⚠️ **Image warning:** id=69 (being deleted) **has images** that id=427 does not. Copy cover + profile image URLs from id=69 to id=427 before deleting.
⚠️ **Name warning:** id=69 has the better-formatted name. After merging, update id=427's name to `"Estadio Bicentenario, La Florida"`.

---

## Group 3 — `plazavictoria`

| | ID | Name | Upcoming | Total | Images |
|---|---|---|---|---|---|
| ✅ **KEEP** | 456 | Plazavictoria | 3 | 3 | none |
| ❌ **DELETE** | 390 | Plaza Victoria | 1 | 1 | none |

**Action:** Reassign 1 event from id=390 → id=456, then delete id=390.

⚠️ **Name warning:** Canonical (id=456) has slug-style name `"Plazavictoria"`. After merging, update to `"Plaza Victoria"`.

---

## Group 4 — `salamaster`

| | ID | Name | Upcoming | Total | Images |
|---|---|---|---|---|---|
| ✅ **KEEP** | 460 | Salamaster | 21 | 21 | none |
| ❌ **DELETE** | 46 | Sala Master | 1 | 4 | **✓ HAS IMAGES** |

**Action:** Reassign 4 events from id=46 → id=460, then delete id=46.

⚠️ **Image warning:** id=46 (being deleted) **has images** that id=460 does not. Copy cover + profile image URLs from id=46 to id=460 before deleting.
⚠️ **Name warning:** id=46 has the better-formatted name `"Sala Master"`. After merging, update id=460's name to `"Sala Master"`.

---

## Group 5 — `salarbx`

| | ID | Name | Upcoming | Total | Images |
|---|---|---|---|---|---|
| ✅ **KEEP** | 289 | Sala RBX | 17 | 23 | none |
| ❌ **DELETE** | 462 | Salarbx | 3 | 3 | none |

**Action:** Reassign 3 events from id=462 → id=289, then delete id=462.

No warnings — canonical has proper name and more events. ✓

---

---

## Group 6 — `lapuertaamarilla`

| | ID | Name | Upcoming | Total | Images |
|---|---|---|---|---|---|---|
| ✅ **KEEP** | 459 | Lapuertaamarilla | 4 | 4 | none |
| ❌ **DELETE** | 15 | Bar La Puerta Amarilla | 0 | 1 | **✓ HAS IMAGES** |

**Action:** Reassign 1 event from id=15 → id=459, then delete id=15.

⚠️ **Image warning:** id=15 (being deleted) **has images** that id=459 does not. Copy cover + profile image URLs from id=15 to id=459 before deleting.
⚠️ **Name warning:** id=459 has a slug-style name. After merging, update id=459's name to `"Bar La Puerta Amarilla"`.

---

## Group 7 — `cinepolispaseolosdominicos` (potential — verify before executing)

| | ID | Name | Upcoming | Total | Images |
|---|---|---|---|---|---|---|
| ✅ **KEEP** | 299 | Cinépolis Paseo Los Dominicos San Carlos | 14 | 105 | **✓ HAS IMAGES** |
| ❌ **DELETE** | 126 | Cinépolis Paseo Los Dominicos | 0 | 0 | **✓ HAS IMAGES** |

**Action:** id=126 has 0 events total, so no reassignment needed — delete id=126 directly.

⚠️ **Verify first:** id=126 may refer to a separate physical screen/hall vs. id=299 (San Carlos hall). Check the venue address/coordinates in the DB before deleting. If they are distinct physical locations, do NOT merge.
⚠️ **Image note:** Both have images. Inspect id=126's images — if they're the same venue art, they can be discarded with the delete. If unique, copy to id=299 first.

---

## Not duplicates — dismissed pairs

The following pairs surfaced in the substring search but are **not true duplicates**:

| Pair | Reason |
|------|--------|
| Centro Arte Alameda (id=102) vs Sala Ceina-Centro Arte Alameda (id=118) | Sub-venue relationship — Sala Ceina is a room inside Centro Arte Alameda. Keep both. |
| Cinépolis Parque Arauco (id=121) vs Cinépolis Premium Class Parque Arauco (id=300) | Different format/tier at the same mall. Distinct venues in practice. Keep both. |
| Estadio Nacional (id=71) vs Parque Estadio Nacional (id=166) | Different locations — the stadium interior vs. the surrounding park/grounds. Keep both. |
| MAC (id=78) vs MAC Quinta Normal (id=79) | Different branches of Museo de Arte Contemporáneo. Keep both. |
| Por Confirmar (id=284) vs LA REINA Por Confirmar (id=435) | Placeholder venues for different communes. Keep both. |

---

## Summary table

| Group | Keep ID | Keep Name | Delete ID | Delete Name | Events | Warnings |
|-------|---------|-----------|-----------|-------------|--------|----------|
| barderene | 464 | Barderene | 354 | Bar de Rene | 4 | Rename canonical |
| estadiobicentenariolaflorida | 427 | Estadio Bicentenario la Florida | 69 | Estadio Bicentenario, La Florida | 3 | Copy images + rename |
| plazavictoria | 456 | Plazavictoria | 390 | Plaza Victoria | 1 | Rename canonical |
| salamaster | 460 | Salamaster | 46 | Sala Master | 4 | Copy images + rename |
| salarbx | 289 | Sala RBX | 462 | Salarbx | 3 | — |
| lapuertaamarilla | 459 | Lapuertaamarilla | 15 | Bar La Puerta Amarilla | 1 | Copy images + rename |
| cinepolispaseolosdominicos | 299 | Cinépolis Paseo Los Dominicos San Carlos | 126 | Cinépolis Paseo Los Dominicos | 0 | **Verify physical location before executing** |

**Total events to reassign: 16** (15 original + 1 new)
**Total venues to delete: 6 confirmed + 1 pending verification**
**Groups with image loss risk: 3** (groups 2, 4, and 6 — copy images before deleting)
**Groups needing canonical name fix: 5** (groups 1, 2, 3, 4, 6)

---

## Merge SQL (DO NOT RUN until confirmed)

```sql
-- ── Group 1: barderene ────────────────────────────────────────────────────
BEGIN;
UPDATE events SET venue_id = 464 WHERE venue_id = 354;
UPDATE venues SET name = 'Bar de Rene' WHERE id = 464;
DELETE FROM venues WHERE id = 354;
COMMIT;

-- ── Group 2: estadiobicentenariolaflorida ─────────────────────────────────
BEGIN;
UPDATE venues SET
  cover_image_url   = (SELECT cover_image_url   FROM venues WHERE id = 69),
  profile_image_url = (SELECT profile_image_url FROM venues WHERE id = 69),
  name              = 'Estadio Bicentenario, La Florida'
WHERE id = 427;
UPDATE events SET venue_id = 427 WHERE venue_id = 69;
DELETE FROM venues WHERE id = 69;
COMMIT;

-- ── Group 3: plazavictoria ────────────────────────────────────────────────
BEGIN;
UPDATE events SET venue_id = 456 WHERE venue_id = 390;
UPDATE venues SET name = 'Plaza Victoria' WHERE id = 456;
DELETE FROM venues WHERE id = 390;
COMMIT;

-- ── Group 4: salamaster ───────────────────────────────────────────────────
BEGIN;
UPDATE venues SET
  cover_image_url   = (SELECT cover_image_url   FROM venues WHERE id = 46),
  profile_image_url = (SELECT profile_image_url FROM venues WHERE id = 46),
  name              = 'Sala Master'
WHERE id = 460;
UPDATE events SET venue_id = 460 WHERE venue_id = 46;
DELETE FROM venues WHERE id = 46;
COMMIT;

-- ── Group 5: salarbx ─────────────────────────────────────────────────────
BEGIN;
UPDATE events SET venue_id = 289 WHERE venue_id = 462;
DELETE FROM venues WHERE id = 462;
COMMIT;

-- ── Group 6: lapuertaamarilla ──────────────────────────────────────────────
BEGIN;
UPDATE venues SET
  cover_image_url   = (SELECT cover_image_url   FROM venues WHERE id = 15),
  profile_image_url = (SELECT profile_image_url FROM venues WHERE id = 15),
  name              = 'Bar La Puerta Amarilla'
WHERE id = 459;
UPDATE events SET venue_id = 459 WHERE venue_id = 15;
DELETE FROM venues WHERE id = 15;
COMMIT;

-- ── Group 7: cinepolispaseolosdominicos (verify physical location first!) ──
-- id=126 has 0 events — no reassignment needed.
-- Only run after confirming id=126 and id=299 refer to the same physical venue.
BEGIN;
DELETE FROM venues WHERE id = 126;
COMMIT;
```
