from sqlalchemy import create_engine, text
import os

e = create_engine(os.environ['DATABASE_URL'])
conn = e.connect()

TODAY = '2026-04-28'

sources = [
    ('puntoticket',  'puntoticket%'),
    ('cinepolis',    'cinepolis%'),
    ('cinemark',     'cinemark%'),
    ('passline',     'passline%'),
    ('comedypass',   'comedypass%'),
    ('ticketplus',   'ticketplus%'),
    ('ticketmaster', 'ticketmaster%'),
    ('evently',      'evently%'),
    ('portaldisc',   'portaldisc%'),
]

results = []
for name, pattern in sources:
    r = conn.execute(text(
        "SELECT"
        " COUNT(*) AS total,"
        " ROUND(COUNT(description)::numeric / NULLIF(COUNT(*),0) * 100) AS desc_pct,"
        " ROUND(COUNT(image_url)::numeric   / NULLIF(COUNT(*),0) * 100) AS img_pct,"
        " ROUND(COUNT(price_range)::numeric / NULLIF(COUNT(*),0) * 100) AS price_pct,"
        " ROUND(COUNT(time_start)::numeric  / NULLIF(COUNT(*),0) * 100) AS time_pct,"
        " ROUND(COUNT(venue_id)::numeric    / NULLIF(COUNT(*),0) * 100) AS vid_pct,"
        " ROUND(COUNT(url)::numeric         / NULLIF(COUNT(*),0) * 100) AS url_pct,"
        " ROUND(COUNT(category)::numeric    / NULLIF(COUNT(*),0) * 100) AS cat_pct,"
        " ROUND(COUNT(CASE WHEN is_sold_out = true THEN 1 END)::numeric / NULLIF(COUNT(*),0) * 100) AS soldout_pct"
        " FROM events"
        " WHERE date >= '" + TODAY + "'"
        " AND source_url ILIKE '" + pattern + "'"
    )).fetchone()
    results.append((name,) + tuple(r))

# Also grab a few sample rows per source for spot-check
print("=" * 90)
print("FIELD COMPLETENESS AUDIT — upcoming events only (date >= today)")
print("=" * 90)
print(f"{'Source':<13} {'N':>5}  {'Desc':>5} {'Img':>5} {'Price':>6} {'Time':>5} {'VenID':>6} {'URL':>5} {'Cat':>5} {'Sold%':>6}")
print("-" * 90)
for row in results:
    src, total, desc, img, price, time_, vid, url, cat, sold = row
    if total == 0:
        print(f"{src:<13}   NONE")
        continue
    def p(v): return f"{int(v)}%" if v is not None else "  n/a"
    print(f"{src:<13} {total:>5}  {p(desc):>5} {p(img):>5} {p(price):>6} {p(time_):>5} {p(vid):>6} {p(url):>5} {p(cat):>5} {p(sold):>6}")

print()

# Per-source sample rows
for name, pattern in sources:
    rows = conn.execute(text(
        "SELECT name, date, time_start, description IS NOT NULL as has_desc,"
        " image_url IS NOT NULL as has_img, price_range IS NOT NULL as has_price,"
        " venue_id IS NOT NULL as has_vid, url IS NOT NULL as has_url,"
        " category, source_url"
        " FROM events"
        " WHERE date >= '" + TODAY + "'"
        " AND source_url ILIKE '" + pattern + "'"
        " ORDER BY date ASC LIMIT 5"
    )).fetchall()
    if not rows:
        continue
    print(f"\n--- {name.upper()} sample rows ---")
    for r in rows:
        flags = []
        if not r[3]: flags.append("NO_DESC")
        if not r[4]: flags.append("NO_IMG")
        if not r[5]: flags.append("NO_PRICE")
        if not r[6]: flags.append("NO_VID")
        if not r[7]: flags.append("NO_URL")
        flag_str = " ".join(flags) if flags else "OK"
        print(f"  [{r[8] or '?':10}] {str(r[1]):10} {str(r[2] or '?'):6}  {r[0][:45]!r:<47}  {flag_str}")

conn.close()
