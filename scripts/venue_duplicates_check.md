# Venue Duplicates Check
Generated: 2026-05-11 | Branch: feat/integrate-e2e-phase3

Strategies used:
- **A**: pg_trgm fuzzy similarity > 0.55
- **B**: Substring containment (name length > 6)

Legend: events_a / events_b = total event count per venue

---

## STRATEGY A — Fuzzy Similarity (27 pairs)

| sim  | id_a | name_a | type_a | ev_a | id_b | name_b | type_b | ev_b | Recommendation |
|------|------|--------|--------|------|------|--------|--------|------|----------------|
| 1.00 | 79 | Museo de Arte Contemporáneo (MAC) – Quinta Normal | Museo | 1 | 478 | Museo de Arte Contemporáneo MAC Quinta Normal | Museo | 9 | **SAME VENUE — merge** → keep id=478 (most events); copy cover from id=79; copy real coords [-33.4422,-70.6806] from id=79; DELETE id=79 |
| 0.86 | 314 | Estadio Lo Barnechea | Espacio Cultural | 0 | 444 | Estadio Barnechea | Espacio Cultural | 1 | **SAME VENUE — merge** → keep id=444 (has events); DELETE id=314 |
| 0.76 | 299 | Cinépolis Paseo Los Dominicos San Carlos | Cine | 80 | 483 | Paseo Los Dominicos (San Carlos) | Cine | 95 | **SAME VENUE — merge** → keep id=299 (branded name, has images); reassign id=483 events → id=299; DELETE id=483 |
| 0.76 | 280 | La Casona de Hilda Parra | Espacio Cultural | 1 | 428 | Casona Hilda Parra | Club | 1 | **SAME VENUE — merge** → keep id=428 (has images); rename to "La Casona de Hilda Parra"; reassign id=280 event → id=428; DELETE id=280 |
| 0.72 | 247 | Hotel Pullman Santiago Vitacura | Bar | 1 | 273 | Hotel Pullman Vitacura | Espacio Cultural | 1 | **SAME VENUE — merge** → keep id=247 (longer canonical name); reassign id=273 event → id=247; DELETE id=273 |
| 0.71 | 71 | Estadio Nacional | Arena | 2 | 166 | Parque Estadio Nacional | Parque | 1 | **DIFFERENT VENUES** — stadium vs. surrounding park; distinct venue types and event uses |
| 0.70 | 102 | Centro Arte Alameda | Centro Cultural | 8 | 118 | Sala Ceina-Centro Arte Alameda | Cine | 13 | **DIFFERENT VENUES** — Sala CEINA is a specific film screening room inside Centro Arte Alameda; intentionally separate for showtime granularity |
| 0.68 | 78 | Museo de Arte Contemporáneo (MAC) | Museo | 0 | 79 | Museo de Arte Contemporáneo (MAC) – Quinta Normal | Museo | 1 | **3-WAY DUPLICATE** — see note below (id=78/79/478); id=78 is MAC Santiago Centro branch (real coords [-33.4351,-70.6444]); id=79 and id=478 are both MAC Quinta Normal |
| 0.68 | 78 | Museo de Arte Contemporáneo (MAC) | Museo | 0 | 478 | Museo de Arte Contemporáneo MAC Quinta Normal | Museo | 9 | **3-WAY DUPLICATE** — see note below |
| 0.67 | 126 | Cinépolis Mall Plaza Los Dominicos | Cine | 0 | 296 | Cinépolis Mallplaza Los Dominicos Premium Class | Cine | 226 | **DIFFERENT VENUES** — already verified in previous session: id=126 coords [-33.4091,-70.537] vs id=296 fallback; different cinema halls at different addresses |
| 0.67 | 121 | Cinépolis Parque Arauco | Cine | 190 | 300 | Cinépolis Parque Arauco Premium Class | Cine | 148 | **DIFFERENT VENUES** — standard vs. Premium Class halls in same building; intentionally separate for ticket pricing and showtime purposes |
| 0.64 | 53 | Teatro Municipal de Santiago | Teatro | 2 | 272 | Teatro Municipal de San Miguel | Teatro | 1 | **DIFFERENT VENUES** — different municipalities (Santiago Centro vs. San Miguel) |
| 0.63 | 356 | El 7 Restorant | Espacio Cultural | 1 | 377 | Restaurant El 7 | Espacio Cultural | 1 | **SAME VENUE — merge** → keep id=356; reassign id=377 event → id=356; DELETE id=377 |
| 0.63 | 131 | Cinemark Plaza Tobalaba | Cine | 253 | 136 | Cineplanet Plaza Tobalaba | Cine | 0 | **DIFFERENT VENUES** — different chains (Cinemark vs. Cineplanet) at the same mall |
| 0.63 | 295 | Cinépolis Mallplaza Egaña Premium Class | Cine | 464 | 296 | Cinépolis Mallplaza Los Dominicos Premium Class | Cine | 226 | **DIFFERENT VENUES** — different malls (Egaña vs. Los Dominicos) |
| 0.62 | 141 | Parque Metropolitano de Santiago | Parque | 0 | 466 | Metropolitan Santiago | Espacio Cultural | 2 | **DIFFERENT VENUES** — Parque Metropolitano is the large hilltop park; Metropolitan Santiago is likely a hotel/event space with a similar name |
| 0.61 | 284 | Por Confirmar | Espacio Cultural | 1 | 435 | LA REINA (Por confirmar) | Espacio Cultural | 1 | **DIFFERENT VENUES** — both are TBD/placeholder venues for different events in different areas; keep both |
| 0.61 | 77 | Museo Bellas Artes | Museo | 0 | 477 | Museo Nacional de Bellas Artes | Museo | 1 | **SAME VENUE — merge** → keep id=477 (official name, has events); DELETE id=77 |
| 0.59 | 137 | Cineplanet Mallplaza Alameda | Cine | 187 | 138 | Cineplanet Mallplaza Norte | Cine | 247 | **DIFFERENT VENUES** — different malls (Alameda vs. Norte/Huecharaba) |
| 0.59 | 67 | Teatro Novedades | Teatro | 0 | 386 | Teatro Comunitario Novedades | Teatro | 1 | **SAME VENUE — merge** → keep id=386 (full official name); DELETE id=67 |
| 0.58 | 303 | Cinépolis Patio Outlet Maipú | Cine | 115 | 311 | Cinépolis Patio Outlet La Florida | Cine | 223 | **DIFFERENT VENUES** — different Patio Outlet locations (Maipú vs. La Florida) |
| 0.57 | 122 | Cinépolis Plaza Egaña | Cine | 0 | 127 | Cinépolis Plaza Oeste | Cine | 0 | **DIFFERENT VENUES** — different plazas (Egaña vs. Oeste) |
| 0.57 | 304 | Cinépolis Santa María de Melipilla | Cine | 57 | 484 | Santa Maria de Melipilla | Cine | 62 | **SAME VENUE — merge** → keep id=304 (branded name, has images); reassign id=484 events → id=304; DELETE id=484 |
| 0.57 | 323 | Centro de Eventos Ignis | Bar | 1 | 426 | Centro de eventos Tumbaos | Espacio Cultural | 1 | **DIFFERENT VENUES** — different event centers with similar generic names |
| 0.57 | 121 | Cinépolis Parque Arauco | Cine | 190 | 307 | Cinépolis Arauco Maipú | Cine | 406 | **DIFFERENT VENUES** — Parque Arauco (Las Condes) vs. Arauco Maipú (Maipú); different cities |
| 0.56 | 129 | Cinemark Mallplaza Vespucio | Cine | 478 | 133 | Cinemark Mallplaza Oeste | Cine | 542 | **DIFFERENT VENUES** — different malls (Vespucio vs. Oeste) |
| 0.56 | 349 | Fuente Maestra Chicureo | Comedia | 2 | 379 | La Fuente Maestra | Espacio Cultural | 1 | **NEEDS VERIFICATION** — "Chicureo" suggests a branch; "La Fuente Maestra" may be a different location of the same brand. Check coordinates or contact info to confirm |

---

## STRATEGY B — Substring Containment (5 pairs, all covered by Strategy A)

| id_a | name_a | ev_a | id_b | name_b | ev_b | Recommendation |
|------|--------|------|------|--------|------|----------------|
| 78 | Museo de Arte Contemporáneo (MAC) | 0 | 79 | Museo de Arte Contemporáneo (MAC) – Quinta Normal | 1 | See 3-way note below |
| 121 | Cinépolis Parque Arauco | 190 | 300 | Cinépolis Parque Arauco Premium Class | 148 | DIFFERENT VENUES (see Strategy A) |
| 102 | Centro Arte Alameda | 8 | 118 | Sala Ceina-Centro Arte Alameda | 13 | DIFFERENT VENUES (see Strategy A) |
| 71 | Estadio Nacional | 2 | 166 | Parque Estadio Nacional | 1 | DIFFERENT VENUES (see Strategy A) |
| 284 | Por Confirmar | 1 | 435 | LA REINA (Por confirmar) | 1 | DIFFERENT VENUES (see Strategy A) |

---

## SPECIAL CASE: 3-Way MAC Duplicate (id=78, 79, 478)

MAC has **two physical branches**:

| id | name | events | coords | images | Branch |
|----|------|--------|--------|--------|--------|
| 78 | Museo de Arte Contemporáneo (MAC) | 0 | [-33.4351, -70.6444] (real) | cover=Y, profile=Y | MAC Santiago Centro (Parque Forestal area) |
| 79 | Museo de Arte Contemporáneo (MAC) – Quinta Normal | 1 | [-33.4422, -70.6806] (real) | cover=Y, profile=N |  MAC Quinta Normal |
| 478 | Museo de Arte Contemporáneo MAC Quinta Normal | 9 | fallback | cover=N, profile=N | MAC Quinta Normal (duplicate of id=79) |

**Action:**
- id=78 → **KEEP** as "MAC Santiago Centro" (distinct branch, real coords, has images)
- id=79 + id=478 → **SAME VENUE**: merge id=79 → id=478; copy cover+real coords from id=79 to id=478; DELETE id=79
- Rename id=78 to "Museo de Arte Contemporáneo (MAC) – Santiago Centro" for clarity

---

## Summary: Confirmed Merges

| Priority | Keep | Delete | Events to reassign | Action |
|----------|------|--------|--------------------|--------|
| HIGH | id=478 (MAC Quinta Normal) | id=79 | 1 | Copy cover+real coords from id=79; DELETE id=79 |
| HIGH | id=299 (Cinépolis Paseo Los Dominicos San Carlos) | id=483 | ~95 | Reassign all; DELETE id=483 |
| HIGH | id=304 (Cinépolis Santa María de Melipilla) | id=484 | ~62 | Reassign all; DELETE id=484 |
| MED | id=444 (Estadio Barnechea) | id=314 | 0 | DELETE id=314 |
| MED | id=477 (Museo Nacional de Bellas Artes) | id=77 | 0 | DELETE id=77 |
| MED | id=386 (Teatro Comunitario Novedades) | id=67 | 0 | DELETE id=67 |
| MED | id=428 (Casona Hilda Parra) | id=280 | 1 | Rename id=428; reassign; DELETE id=280 |
| MED | id=247 (Hotel Pullman Santiago Vitacura) | id=273 | 1 | Reassign; DELETE id=273 |
| MED | id=356 (El 7 Restorant) | id=377 | 1 | Reassign; DELETE id=377 |
| LOW | id=78 (MAC Santiago Centro) | — | — | Rename only |
| VERIFY | id=349 or id=379 (Fuente Maestra) | TBD | — | Check coords before merging |
