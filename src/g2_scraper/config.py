# Configuracion central del scraper
from pathlib import Path

BASE_URL = "https://www.g2.com/"
CATEGORIES_URL = f"{BASE_URL}/categories"

# navegador externo (chrome con --remote-debugging-port)
CDP_URL = "http://localhost:9222"

# perfil persistente = cookies y clearences de cloudflare sobreviven entre las ejecuciones
PROFILE_DIR = Path.home() / ".g2-scraper" / "chrome-profile"

# salidas
DATA_DIR = Path("data")
CATEGORIES_FILE = DATA_DIR / "categories.json"
PRODUCTS_FILE = DATA_DIR / "products.jsonl"
FAILURES_FILE = DATA_DIR / "failures.jsonl"
REPORT_FILE = DATA_DIR / "report.json"

# Parametros de scraping
MAX_REQUESTS = 100                 # objetivo de la prueba
MAX_ATTEMPTS = 3                   # reintentos por categoria
NAV_TIMEOUT_MS = 45_000            # timeout de navegacion
ELEMENT_TIMEOUT_MS = 15_000        # timeout esperando la product-card
CHALLENGE_WAIT_S = 60.0            # max. espera de challenge de Cloudflare
MIN_DELAY_S = 2.5                  # ritmo minimo entre solicitudes
MAX_DELAY_S = 6.0                  # ritmo maximo entre solicitudes
BACKOFF_BASE_S = 8.0               # backoff exponencial: 8s, 16s, 24s
CONSECUTIVE_FAILURES_COOLDOWN = 3  # fallos seguidos antes de pausa larga
COOLDOWN_S = 90.0                  # pausa larga de recuperacion