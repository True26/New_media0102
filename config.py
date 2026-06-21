import os

BASE_URL = "https://tioplus.app"

# TMDB API — la key se lee del entorno (GitHub Secret) o del archivo .env local
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "51aed133d416fd847276ebb8fcd8c98b")
TMDB_SEARCH  = "https://api.themoviedb.org/3/search/multi"
TMDB_LANG    = "es-ES"

# Selectores del sitio
SELECTORS = {
    # Tarjetas del listado
    "cards":      "article",
    "card_link":  "a.itemA",
    "card_title": "h2",
    "card_cover": "img",

    # Página de detalle
    "detail_title": "h1",
    "detail_cover": "#player-tr, .playrn, .video-html",

    # Reproductor
    "iframe_player": "iframe",
}

# Secciones del catálogo con su total de páginas
CATALOG = {
    "peliculas": 470,   # 11,276 items
    "series":     99,   #  2,355 items
    "animes":     13,   #    302 items
    "doramas":    11,   #    249 items
}

# Configuración del browser
HEADLESS      = True    # False para ver el navegador
TIMEOUT       = 30000   # ms
DELAY_BETWEEN = 1.5     # segundos entre requests
MAX_PAGES     = 0       # páginas por sección (0 = todas)
MAX_ITEMS     = 0       # items a scrapear (0 = todos)
