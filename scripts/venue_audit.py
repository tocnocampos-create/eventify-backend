# -*- coding: utf-8 -*-
import json, unicodedata, re
from datetime import datetime

DB_FILE = '/Users/antoniocampos/.claude/projects/-Users-antoniocampos-Desktop-eventifye2e/ccff01d3-7996-47d0-881a-6eb107e9ac96/tool-results/b6im3td52.txt'
REPORT_PATH = '/Users/antoniocampos/Desktop/eventifye2e/eventify-backend-feat-integrate-e2e/scripts/venue_master_list_audit.md'

db = json.loads(open(DB_FILE).read())


def norm(s):
    s = s.lower().strip()
    s = unicodedata.normalize('NFD', s)
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    s = re.sub(r"[^\w\s]", ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def sig_words(s):
    stop = {'de', 'la', 'el', 'los', 'las', 'del', 'en', 'y', 'a', 'bar',
            'club', 'teatro', 'sala', 'centro', 'parque', 'estadio', 'museo'}
    return {w for w in norm(s).split() if w not in stop and len(w) > 2}


def find_match(master_name, db_list):
    mn = norm(master_name)
    mw = sig_words(master_name)
    # 1. Exact
    for v in db_list:
        if norm(v['name']) == mn:
            return v
    # 2. Substring
    for v in db_list:
        vn = norm(v['name'])
        if mn in vn or vn in mn:
            return v
    # 3. Word overlap >= 75%
    best, best_score = None, 0
    for v in db_list:
        vw = sig_words(v['name'])
        if not mw:
            continue
        overlap = len(mw & vw) / len(mw)
        if overlap > best_score:
            best_score = overlap
            best = v
    if best_score >= 0.75 and best:
        return best
    return None


def fmt_row(v, expected_type=None):
    cov = '\u2713' if v['has_cover']      else '\u2717'
    pro = '\u2713' if v['has_profile']    else '\u2717'
    crd = '\u2713' if v['has_real_coords'] else '\u2717'
    ev  = v['upcoming']
    name_d = v['name'][:42]
    type_ok = expected_type is None or v['venue_type'] == expected_type
    pfx = '\u2705' if type_ok else '\u26a0\ufe0f '
    row = f"{pfx} id={v['id']:<4} {name_d:<44} | cover {cov} | profile {pro} | coords {crd} | {ev} eventos"
    if not type_ok:
        row += f"\n       \u21b3 venue_type='{v['venue_type']}' should be '{expected_type}'"
    return row


MASTER = {
    'Arena': [
        'Parque Padre Hurtado', 'Estadio Bicentenario La Florida', 'Movistar Arena',
        'Estadio Nacional', 'Estadio Monumental', 'Claro Arena', 'Estadio Santa Laura',
        'Gran Arena Monticello', 'Parque Bicentenario Vitacura', "Parque O'Higgins",
        'Estadio Municipal de la Cisterna',
    ],
    'Bar': [
        'Bar El Clan', 'Bar Grez', 'Thelonious Club de Jazz', 'Club de Jazz de Santiago',
        'Bar La Capital', 'Bar El Bajo', 'Club de San Miguel', 'La Batuta', 'Backroom Bar',
        'Ambar Restobar', 'Palermo Teatro Bar', 'Galpon Italia', 'Onaciu', 'Bar Victoria',
        'Bar La Puerta Amarilla', 'Bar de Rene', 'Fortunatos Bar',
        'Gran Refugio Barrio Italia', 'Gran Refugio Mall Plaza Egana', 'Comedy Restobar',
        'Honesto Mike Vitacura', 'Honesto Mike Barrio Lastarria', 'Honesto Mike Providencia',
        'Doble Standup', 'Bar Loreto', 'Teatro Fiebre Bar', 'El Cachafaz',
        'Casa Conejo', 'Bar El Tunel',
    ],
    'Club': [
        'Club Chocolate', 'Cajacustica', 'Blondie', 'La Feria Club', 'Club Subterraneo',
        'Club Ambar', 'Club Room SCL', 'Club Amanda', 'Illuminati Disco',
        'Sala Gente-Omnium', 'Centro Parque', 'Espacio Riesco', 'Teatro Alicia',
    ],
    'Sala de Concierto': [
        'Gran Sala Sinfonica Nacional', 'Omnilab Sound', 'Sala Master', 'Sala Metronomo',
        'Sala SCD Bellavista', 'Sala SCD Plaza Egana', 'CityLab GAM',
    ],
    'Teatro': [
        'Teatro Nescafe de las Artes', 'Centro Cultural Lo Prado',
        'Teatro Municipal de Santiago', 'Teatro Finis Terrae', 'Teatro Cariola',
        'Teatro Roma', 'Teatro Caupolican', 'Teatro Camilo Henriquez',
        'Teatro Municipal de Las Condes', 'Teatro Oriente', 'Teatro Ictus', 'Teatro UC',
        'Teatro Universidad de Chile', 'Teatro San Gines', 'Teatro Coliseo',
        'Teatro La Memoria', 'Teatro Novedades', 'Teatro Zoco', 'Teatro Mori Vitacura',
        'Teatro Mori Bellavista', 'Teatro Mori Parque Arauco', 'Teatro Mori Recoleta',
        'Teatro Teleton',
    ],
}

MASTER_DISPLAY = {
    'Arena': [
        'Parque Padre Hurtado', 'Estadio Bicentenario La Florida', 'Movistar Arena',
        'Estadio Nacional', 'Estadio Monumental', 'Claro Arena', 'Estadio Santa Laura',
        'Gran Arena Monticello', 'Parque Bicentenario Vitacura', "Parque O'Higgins",
        'Estadio Municipal de la Cisterna',
    ],
    'Bar': [
        'Bar El Clan', 'Bar Grez', 'Thelonious Club de Jazz', 'Club de Jazz de Santiago',
        'Bar La Capital', 'Bar El Bajo', 'Club de San Miguel', 'La Batuta', 'Backroom Bar',
        'Ambar Restobar', 'Palermo Teatro Bar', 'Galp\u00f3n Italia', 'Onaciu', 'Bar Victoria',
        'Bar La Puerta Amarilla', 'Bar de Ren\u00e9', 'Fortunatos Bar',
        'Gran Refugio Barrio Italia', 'Gran Refugio Mall Plaza Eg\u00e3na', 'Comedy Restobar',
        'Honesto Mike Vitacura', 'Honesto Mike Barrio Lastarria', 'Honesto Mike Providencia',
        'Doble Standup', 'Bar Loreto', 'Teatro Fiebre Bar', 'El Cachafaz',
        'Casa Conejo', 'Bar El T\u00fanel',
    ],
    'Club': [
        'Club Chocolate', 'Cajac\u00fastica', 'Blondie', 'La Feria Club', 'Club Subterr\u00e1neo',
        'Club Ambar', 'Club Room SCL', 'Club Amanda', 'Illuminati Disco',
        'Sala Gente-Omnium', 'Centro Parque', 'Espacio Riesco', 'Teatro Alicia',
    ],
    'Sala de Concierto': [
        'Gran Sala Sinf\u00f3nica Nacional', 'Omnilab Sound', 'Sala Master', 'Sala Metr\u00f3nomo',
        'Sala SCD Bellavista', 'Sala SCD Plaza Eg\u00e3na', 'CityLab GAM',
    ],
    'Teatro': [
        'Teatro Nescaf\u00e9 de las Artes', 'Centro Cultural Lo Prado',
        'Teatro Municipal de Santiago', 'Teatro Finis Terrae', 'Teatro Cariola',
        'Teatro Roma', 'Teatro Caupolic\u00e1n', 'Teatro Camilo Henr\u00edquez',
        'Teatro Municipal de Las Condes', 'Teatro Oriente', 'Teatro Ictus', 'Teatro UC',
        'Teatro Universidad de Chile', 'Teatro San Gin\u00e9s', 'Teatro Coliseo',
        'Teatro La Memoria', 'Teatro Novedades', 'Teatro Zoco', 'Teatro Mori Vitacura',
        'Teatro Mori Bellavista', 'Teatro Mori Parque Arauco', 'Teatro Mori Recoleta',
        'Teatro Telet\u00f3n',
    ],
}

EMOJI = {
    'Arena': '\U0001f3df\ufe0f',
    'Bar': '\U0001f37a',
    'Club': '\U0001f3a7',
    'Sala de Concierto': '\U0001f3bb',
    'Teatro': '\U0001f3ad',
    'Museo': '\U0001f3db\ufe0f',
    'Centro Cultural': '\U0001f3a8',
    'Cine': '\U0001f3ac',
    'Espacio Cultural': '\U0001f3e2',
    'Parque': '\U0001f333',
    'Cerro': '\u26f0\ufe0f',
    'Bosque': '\U0001f332',
    'Comedia': '\U0001f602',
}

lines = [
    '# Venue Master List Audit',
    f'Generated: {datetime.now().strftime("%Y-%m-%d %H:%M")}',
    f'DB total: {len(db)} venues',
    '',
    '> Categories with a master list: Arena, Bar, Club, Sala de Concierto, Teatro',
    '> All other categories show DB-only data (master list not provided)',
    '',
]

summary_rows = []
db_matched_ids = set()

for cat, norm_names in MASTER.items():
    display_names = MASTER_DISPLAY[cat]
    emoji = EMOJI.get(cat, '\U0001f4cd')
    db_cat = [v for v in db if v['venue_type'] == cat]

    found, missing, wrong_type = [], [], []

    for i, nname in enumerate(norm_names):
        dname = display_names[i]
        v = find_match(nname, db)
        if v is None:
            missing.append(dname)
        elif v['venue_type'] == cat:
            found.append((dname, v))
            db_matched_ids.add(v['id'])
        else:
            wrong_type.append((dname, v))
            db_matched_ids.add(v['id'])

    in_db = len(found) + len(wrong_type)
    no_cover  = sum(1 for _, v in found + wrong_type if not v['has_cover'])
    no_coords = sum(1 for _, v in found + wrong_type if not v['has_real_coords'])

    lines.append(f'## {emoji} {cat.upper()} ({in_db} in DB / {len(norm_names)} in master list)')
    lines.append('')

    for dname, v in sorted(found, key=lambda x: x[1]['name']):
        lines.append(fmt_row(v, expected_type=cat))
    for dname, v in wrong_type:
        lines.append(fmt_row(v, expected_type=cat))
    for dname in sorted(missing):
        lines.append(f'\u274c MISSING: {dname}')

    # Extra in DB with same type, not in master
    extras = [v for v in db_cat if v['id'] not in db_matched_ids]
    if extras:
        lines.append('')
        lines.append(f'  -- {len(extras)} extra in DB (not in master list):')
        for v in sorted(extras, key=lambda x: -x['upcoming'])[:20]:
            ev = v['upcoming']
            lines.append(f'     id={v["id"]:<4} {v["name"]} ({ev} eventos)')
        if len(extras) > 20:
            lines.append(f'     ... and {len(extras)-20} more')

    lines.append('')
    summary_rows.append({
        'cat': f'{emoji} {cat}', 'in_db': in_db, 'master': len(norm_names),
        'missing': len(missing), 'wrong': len(wrong_type),
        'no_img': no_cover, 'no_coords': no_coords,
    })

# DB-only categories
DB_ONLY = [
    ('Museo', '\U0001f3db\ufe0f'),
    ('Centro Cultural', '\U0001f3a8'),
    ('Cine', '\U0001f3ac'),
    ('Espacio Cultural', '\U0001f3e2'),
    ('Parque', '\U0001f333'),
    ('Cerro', '\u26f0\ufe0f'),
    ('Bosque', '\U0001f332'),
    ('Comedia', '\U0001f602'),
]

for cat, emoji in DB_ONLY:
    venues = [v for v in db if v['venue_type'] == cat]
    no_cover  = sum(1 for v in venues if not v['has_cover'])
    no_coords = sum(1 for v in venues if not v['has_real_coords'])

    lines.append(f'## {emoji} {cat.upper()} (master list not provided -- {len(venues)} in DB)')
    lines.append('')
    for v in sorted(venues, key=lambda x: (-x['upcoming'], x['name'])):
        lines.append(fmt_row(v))
    lines.append('')
    summary_rows.append({
        'cat': f'{emoji} {cat}', 'in_db': len(venues), 'master': '--',
        'missing': '--', 'wrong': '--', 'no_img': no_cover, 'no_coords': no_coords,
    })

# Summary
lines.append('## SUMMARY TABLE')
lines.append('')
lines.append('| Category | In DB | Master | Missing | Wrong type | No cover | No coords |')
lines.append('|----------|------:|-------:|--------:|-----------:|---------:|----------:|')
for r in summary_rows:
    lines.append(f"| {r['cat']} | {r['in_db']} | {r['master']} | {r['missing']} | {r['wrong']} | {r['no_img']} | {r['no_coords']} |")
lines.append('')

report = '\n'.join(lines)
with open(REPORT_PATH, 'w', encoding='utf-8') as f:
    f.write(report)

# Print to stdout as well
print(report)
