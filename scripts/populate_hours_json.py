"""Populate hours_json JSONB for museum/cultural venues from opening_hours text."""
import json
import psycopg2

DB = "postgresql://postgres:MbowHygexBYnHROAJguYAaccBeNIrvwz@shuttle.proxy.rlwy.net:17408/railway"

# Day name → (key, index in Mon=0..Sun=6)
DAY_MAP = {
    "lun": ("mon", 0), "lunes": ("mon", 0),
    "mar": ("tue", 1), "martes": ("tue", 1),
    "mie": ("wed", 2), "mié": ("wed", 2), "mièrcoles": ("wed", 2), "miercoles": ("wed", 2),
    "jue": ("thu", 3), "jueves": ("thu", 3),
    "vie": ("fri", 4), "viernes": ("fri", 4),
    "sab": ("sat", 5), "sáb": ("sat", 5), "sabado": ("sat", 5), "sábado": ("sat", 5),
    "dom": ("sun", 6), "domingo": ("sun", 6),
}
DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def day_key(token):
    return DAY_MAP.get(token.lower().strip())


def resolve_range(start_idx, end_idx):
    """Return list of day keys from start_idx to end_idx inclusive (wraps around is not needed here)."""
    return DAY_KEYS[start_idx:end_idx + 1]


def parse_time_range(segment):
    """Extract open/close from 'HH:MM–HH:MM' or 'HH:MM-HH:MM'."""
    segment = segment.replace("–", "-").replace("—", "-")
    parts = segment.split("-")
    if len(parts) >= 2:
        return parts[0].strip(), parts[-1].strip()
    return None, None


def parse_hours(text):
    """Parse Spanish opening_hours string into {mon..sun: {open,close}|null}."""
    result = {k: None for k in DAY_KEYS}

    # Split on '|'
    segments = [s.strip() for s in text.split("|")]

    for seg in segments:
        # Skip metadata segments
        lower = seg.lower()
        if any(skip in lower for skip in ["último acceso", "ultimo acceso", "último ingreso"]):
            continue

        # Check for "cerrado" (closed day)
        if "cerrado" in lower:
            # Identify the day
            tokens = lower.replace(":", " ").split()
            for tok in tokens:
                d = day_key(tok)
                if d:
                    result[d[0]] = None
            continue

        # Split on ':' to separate day specifier from time
        colon_idx = seg.find(":")
        if colon_idx == -1:
            continue

        day_part = seg[:colon_idx].strip()
        time_part = seg[colon_idx + 1:].strip()

        # Extract times from time_part
        open_t, close_t = parse_time_range(time_part)
        if not open_t or not close_t:
            continue
        hours_entry = {"open": open_t, "close": close_t}

        # Parse day_part — could be "Mar a Dom", "Lun a Vie", "Mar", "Sáb-Dom", "Mié-Vie"
        day_part_lower = day_part.lower()

        # Range with 'a' (e.g. "Mar a Dom")
        if " a " in day_part_lower:
            parts = [p.strip() for p in day_part_lower.split(" a ")]
            start_d = day_key(parts[0])
            end_d = day_key(parts[1]) if len(parts) > 1 else None
            if start_d and end_d:
                for dk in resolve_range(start_d[1], end_d[1]):
                    result[dk] = hours_entry
        # Range with '-' (e.g. "Sáb-Dom", "Mié-Vie")
        elif "-" in day_part_lower:
            parts = [p.strip() for p in day_part_lower.split("-")]
            start_d = day_key(parts[0])
            end_d = day_key(parts[1]) if len(parts) > 1 else None
            if start_d and end_d:
                for dk in resolve_range(start_d[1], end_d[1]):
                    result[dk] = hours_entry
        # Single day (e.g. "Mar", "Dom", "Sáb")
        else:
            d = day_key(day_part_lower.strip())
            if d:
                result[d[0]] = hours_entry

    return result


def main():
    conn = psycopg2.connect(DB)
    cur = conn.cursor()

    cur.execute("""
        SELECT id, name, opening_hours
        FROM venues
        WHERE venue_type IN ('Museo', 'Centro Cultural', 'Galería', 'Galeria', 'Espacio Cultural')
        AND opening_hours IS NOT NULL AND opening_hours != ''
        ORDER BY id
    """)
    venues = cur.fetchall()

    print(f"Parsing {len(venues)} venues...\n")
    for vid, name, raw in venues:
        parsed = parse_hours(raw)
        print(f"id={vid} {name}")
        print(f"  raw: {raw}")
        print(f"  json: {json.dumps(parsed, ensure_ascii=False)}")

        cur.execute(
            "UPDATE venues SET hours_json = %s WHERE id = %s",
            (json.dumps(parsed), vid),
        )

    conn.commit()
    conn.close()
    print(f"\nDone — {len(venues)} venues updated.")


if __name__ == "__main__":
    main()
