"""Config API endpoint."""
from fastapi import APIRouter

router = APIRouter(tags=["config"])

CONFIG = {
    "categories": [
        {
            "name": "Música",
            "icon": "musical-notes",
            "color": "#9F7BFF",
            "badge_color": "rgba(159, 123, 255, 0.7)",
            "subcategories": ["Jazz", "Vida Nocturna", "Rock", "Electrónica", "Pop", "Folclore", "Latina", "Indie"]
        },
        {
            "name": "Teatro",
            "icon": "theater-masks",
            "color": "#3B82F6",
            "badge_color": "rgba(59, 130, 246, 0.7)",
            "subcategories": ["Drama", "Comedia", "Danza-Ballet", "Musical", "Familiar"]
        },
        {
            "name": "Comedia",
            "icon": "happy",
            "color": "#FF69B4",
            "badge_color": "rgba(255, 105, 180, 0.7)",
            "subcategories": []
        },
        {
            "name": "Arte",
            "icon": "color-palette",
            "color": "#00BCD4",
            "badge_color": "rgba(0, 188, 212, 0.7)",
            "subcategories": ["Museos", "Centro Cultural", "Galerías"]
        },
        {
            "name": "Cine",
            "icon": "film",
            "color": "#3B52D8",
            "badge_color": "rgba(59, 82, 216, 0.7)",
            "subcategories": ["Acción", "Drama", "Terror", "Comedia", "Romántica", "Familiar"]
        }
    ],
    "venue_types": [
        "Bar", "Sala de Concierto", "Club", "Teatro",
        "Arena", "Museo", "Centro Cultural", "Galería", "Cine"
    ],
    "available_cities": ["Santiago"],
    "max_price": 300000,
    "price_step": 500,
    "currency": "CLP",
    "default_coordinates": {
        "latitude": -33.4489,
        "longitude": -70.6693
    },
    "recommended_searches": [
        "Salas de Concierto", "Museos en un día", "Barrio Italia",
        "Imperdibles de la ciudad", "Ruta patrimonial",
        "Eventos gratuitos", "Mercado París-Londres"
    ],
    "search_categories": [
        {"key": "Jazz", "image_url": None},
        {"key": "Comedia", "image_url": None},
        {"key": "Nacional", "image_url": None},
        {"key": "Teatro", "image_url": None},
        {"key": "Vida Nocturna", "image_url": None},
        {"key": "Galerías ", "image_url": None},
        {"key": "Barrios", "image_url": None},
        {"key": "Festivales", "image_url": None},
        {"key": "Cine", "image_url": None},
        {"key": "Museos", "image_url": None},
        {"key": "Al aire libre", "image_url": None},
        {"key": "Sunsets", "image_url": None},
        {"key": "Familiar", "image_url": None},
        {"key": "Ferias", "image_url": None},
        {"key": "City Tour", "image_url": None}
    ]
}


@router.get("/config")
async def get_config():
    """Get application configuration."""
    return CONFIG
