"""
Seed al-aire-libre outdoor venues (parques, cerros, bosques, etc.) into the DB.

Usage (from repo root with Docker running):
    docker exec eventify-api-dev python scripts/seed_alairelibre.py

Checks for duplicate names before inserting. Safe to run multiple times.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from app.db.base import Base
from app.db.models import Venue

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    f"postgresql://{os.getenv('DB_USER','eventify')}:{os.getenv('DB_PASSWORD','eventify')}"
    f"@{os.getenv('DB_HOST','eventify-db')}:{os.getenv('DB_PORT','5432')}/{os.getenv('DB_NAME','eventify')}"
)

VENUES = [
    {"name": "Parque Metropolitano de Santiago",   "type": "Parque",           "city": "Santiago",       "lat": -33.42416546248855,  "lon": -70.63310151391471},
    {"name": "Jardín Japonés de la Amistad",       "type": "Parque",           "city": "Santiago",       "lat": -33.413216794442675, "lon": -70.61422605016216},
    {"name": "Parque Mahuida",                     "type": "Parque",           "city": "La Reina",       "lat": -33.454458124058476, "lon": -70.51782977083843},
    {"name": "Cerro Manquehue",                    "type": "Cerro",            "city": "Lo Barnechea",   "lat": -33.35022168057135,  "lon": -70.58239409582227},
    {"name": "Cerro Provincia",                    "type": "Cerro",            "city": "Las Condes",     "lat": -33.42647199069589,  "lon": -70.43473421534377},
    {"name": "Cerro Pochoco",                      "type": "Cerro",            "city": "Las Condes",     "lat": -33.35296442837397,  "lon": -70.45661399653086},
    {"name": "Parque Natural Aguas de Ramón",      "type": "Parque",           "city": "La Reina",       "lat": -33.43318620915419,  "lon": -70.51933017116406},
    {"name": "Salto de Apoquindo",                 "type": "Salto",            "city": "Las Condes",     "lat": -33.441241471014536, "lon": -70.46087059863656},
    {"name": "Parque Natural Quebrada de Macul",   "type": "Parque",           "city": "Macul",          "lat": -33.49307236124458,  "lon": -70.5180953798127},
    {"name": "Santuario de la Naturaleza Yerba Loca", "type": "Santuario",     "city": "Lo Barnechea",   "lat": -33.3377442748433,   "lon": -70.33279462883594},
    {"name": "Monumento Natural El Morado",        "type": "Monumento Natural","city": "San José de Maipo","lat": -33.78293945491223, "lon": -70.07171161590882},
    {"name": "Parque Nacional Río Clarillo",       "type": "Parque Nacional",  "city": "Pirque",         "lat": -33.71161735144678,  "lon": -70.52451136502867},
    {"name": "Parque Bosque Panul",                "type": "Bosque",           "city": "La Florida",     "lat": -33.53482444986238,  "lon": -70.53434202164821},
    {"name": "Parquemet Bosque Santiago",          "type": "Bosque",           "city": "Huechuraba",     "lat": -33.376312148364306, "lon": -70.60979655767187},
    {"name": "Parque Cerro del Medio",             "type": "Parque",           "city": "Las Condes",     "lat": -33.34974134729358,  "lon": -70.52290583485572},
    {"name": "Cerro Santa Lucía",                  "type": "Cerro",            "city": "Santiago",       "lat": -33.4402179719282,   "lon": -70.64332575073524},
    {"name": "Parque Bicentenario de Cerrillos",   "type": "Parque",           "city": "Cerrillos",      "lat": -33.496126456294526, "lon": -70.70185889814842},
    {"name": "Parque Bicentenario de la Infancia", "type": "Parque",           "city": "Independencia",  "lat": -33.419757528410386, "lon": -70.63963554232812},
    {"name": "Parque Araucano",                    "type": "Parque",           "city": "Las Condes",     "lat": -33.40216399475757,  "lon": -70.57249992883592},
    {"name": "Parque Quinta Normal",               "type": "Parque",           "city": "Santiago",       "lat": -33.442000,          "lon": -70.681000},
    {"name": "Parque Forestal",                    "type": "Parque",           "city": "Santiago",       "lat": -33.435571443907584, "lon": -70.6412692},
    {"name": "Parque de los Reyes",                "type": "Parque",           "city": "Santiago",       "lat": -33.429060213901096, "lon": -70.6665218},
    {"name": "Parque de la Familia",               "type": "Parque",           "city": "Santiago",       "lat": -33.424918616004895, "lon": -70.67848107116407},
    {"name": "Parque Inés de Suárez",              "type": "Parque",           "city": "Providencia",    "lat": -33.44043344867349,  "lon": -70.61134197116405},
    {"name": "Parque Observatorio Cerro Calán",    "type": "Parque",           "city": "Las Condes",     "lat": -33.39594197567063,  "lon": -70.53649127116405},
    {"name": "Parque Estadio Nacional",            "type": "Parque",           "city": "Ñuñoa",          "lat": -33.466330373702,    "lon": -70.61104925767188},
    {"name": "Parque La Bandera",                  "type": "Parque",           "city": "La Florida",     "lat": -33.54232372197957,  "lon": -70.63973808465623},
    {"name": "Parque Municipal Pueblito de Las Vizcachas", "type": "Parque",   "city": "Puente Alto",    "lat": -33.59958381611999,  "lon": -70.52947094512132},
    {"name": "Parque La Castrina",                 "type": "Parque",           "city": "Santiago",       "lat": -33.51137984922847,  "lon": -70.62916791159005},
    {"name": "Parque Bernardo Leighton",           "type": "Parque",           "city": "Ñuñoa",          "lat": -33.46565312457208,  "lon": -70.69510105767188},
    {"name": "Parque André Jarlán",                "type": "Parque",           "city": "Santiago",       "lat": -33.48477363312076,  "lon": -70.66986014232812},
    {"name": "Parque Pierre Dubois",               "type": "Parque",           "city": "Santiago",       "lat": -33.48795103459799,  "lon": -70.67143285767189},
    {"name": "Parque San Borja",                   "type": "Parque",           "city": "Santiago",       "lat": -33.440461723077654, "lon": -70.63754618173212},
    {"name": "Parque La Reina",                    "type": "Parque",           "city": "Santiago",       "lat": -33.43700647240313,  "lon": -70.55367175351189},
    {"name": "Parque O'Higgins",                   "type": "Parque",           "city": "Santiago",       "lat": -33.46323812388637,  "lon": -70.65995312788604},
    {"name": "Parque Padre Hurtado",               "type": "Parque",           "city": "Santiago",       "lat": -33.42997595087368,  "lon": -70.54734143141206},
    {"name": "Parque Bicentenario Vitacura",       "type": "Parque",           "city": "Santiago",       "lat": -33.39850912328684,  "lon": -70.60249413142414},
    {"name": "Parque Ramón Cruz",                  "type": "Parque",           "city": "Santiago",       "lat": -33.455309468192624, "lon": -70.5807906711645},
    {"name": "Parque Santa Rosa de Apoquindo",     "type": "Parque",           "city": "Santiago",       "lat": -33.416695511942024, "lon": -70.539737057671},
    {"name": "Parque de las Esculturas",           "type": "Parque",           "city": "Santiago",       "lat": -33.42015963773938,  "lon": -70.61287560337762},
    {"name": "Templo Bahai",                       "type": "Parque",           "city": "Santiago",       "lat": -33.480005529742506, "lon": -70.52388277232721},
    {"name": "Parque Viña Cousiño Macul",          "type": "Parque",           "city": "Santiago",       "lat": -33.498471205575974, "lon": -70.56254477135431},
    {"name": "Parque las Hualtatas",               "type": "Parque",           "city": "Santiago",       "lat": -33.32051183130034,  "lon": -70.53701554232899},
]


def main():
    engine = create_engine(DATABASE_URL)
    inserted = 0
    skipped = 0

    with Session(engine) as session:
        existing_names = {name for (name,) in session.query(Venue.name).all()}

        for v in VENUES:
            if v["name"] in existing_names:
                print(f"  SKIP (exists): {v['name']}")
                skipped += 1
                continue

            venue = Venue(
                name=v["name"],
                venue_type=v["type"],
                city=v["city"],
                coordinates=[v["lat"], v["lon"]],
                description=None,
                cover_image_url=None,
                profile_image_url=None,
                website_url=None,
                menu_pdf_url=None,
                neighborhood_id=None,
                stars=None,
                schedule=None,
            )
            session.add(venue)
            print(f"  INSERT: {v['name']} ({v['type']})")
            inserted += 1

        session.commit()

    print(f"\nDone — inserted: {inserted}, skipped (already exist): {skipped}")


if __name__ == "__main__":
    main()
