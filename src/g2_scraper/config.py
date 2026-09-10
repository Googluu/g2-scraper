# Configuracion central del scraper
from pathlib import Path

# IMPORTANTE: sin barra final. Las URLs se construyen con `absolute_url()`
# (urljoin), porque concatenar BASE_URL + "/categories/x" producia
# "https://www.g2.com//categories/x". Ningun navegador conducido por una
# persona genera esa forma: es firma de bot y viajaba en el header Referer
# que DataDome registra.
BASE_URL = "https://www.g2.com"
CATEGORIES_URL = f"{BASE_URL}/categories"

# navegador externo (chrome con --remote-debugging-port)
CDP_URL = "http://localhost:9222"

# perfil persistente = la sesion (incluido el token datadome) sobrevive
# entre ejecuciones, lo que permite reanudar con una sesion "tibia"
PROFILE_DIR = Path.home() / ".g2-scraper" / "chrome-profile"

# salidas
DATA_DIR = Path("data")
CATEGORIES_FILE = DATA_DIR / "categories.json"
PRODUCTS_FILE = DATA_DIR / "products.jsonl"
FAILURES_FILE = DATA_DIR / "failures.jsonl"
REPORT_FILE = DATA_DIR / "report.json"

# ---------------------------------------------------------------------------
# Objetivo y timeouts
# ---------------------------------------------------------------------------
MAX_REQUESTS = 100                 # objetivo de la prueba
MAX_ATTEMPTS = 3                   # reintentos por categoria (SOLO fallos de DOM/red)
NAV_TIMEOUT_MS = 45_000            # timeout de navegacion
ELEMENT_TIMEOUT_MS = 15_000        # timeout esperando la product-card
CHALLENGE_WAIT_S = 45.0            # max. espera de un challenge auto-resoluble

# ---------------------------------------------------------------------------
# Ritmo humano
# ---------------------------------------------------------------------------
# Se usa una distribucion log-normal (cola larga: muchas pausas cortas, algunas
# muy largas) en lugar de un uniform(2.5, 6.0). Un delay uniforme es detectable
# por su varianza constante: las personas no navegan a cadencia fija.
DELAY_MEDIAN_S = 13.0              # mediana de la pausa entre solicitudes
DELAY_SIGMA = 0.55                 # dispersion (mayor = mas cola)
DELAY_MIN_S = 5.0
DELAY_MAX_S = 70.0

# Pausa de "lectura": cada tanto la persona se detiene a leer o se distrae
READING_BREAK_PROB = 0.18
READING_BREAK_MIN_S = 30.0
READING_BREAK_MAX_S = 105.0

# Dwell en la pagina antes de extraer (scroll + mouse: DataDome recolecta
# eventos de interaccion; cero eventos en 100 vistas es una anomalia)
DWELL_MIN_S = 1.2
DWELL_MAX_S = 4.5

# ---------------------------------------------------------------------------
# Presupuesto por sesion (token `datadome`)
# ---------------------------------------------------------------------------
# El bloqueo observado se ligo al token datadome, no a la IP: 91 solicitudes
# con un mismo token cruzaron el umbral de score. Se rota preventivamente
# ANTES de llegar ahi, y cada token nuevo se re-calienta (home -> /categories)
# para no arrancar en frio.
SESSION_REQUEST_BUDGET = 30        # solicitudes por token antes de rotar
SESSION_BREAK_MIN_S = 75.0         # pausa al cerrar una micro-sesion
SESSION_BREAK_MAX_S = 165.0

# ---------------------------------------------------------------------------
# Recuperacion ante bloqueo
# ---------------------------------------------------------------------------
# Un bloqueo NUNCA se reintenta en caliente: reintentar sobre el 403 fue lo que
# profundizo el flag en la corrida anterior (6 categorias x 3 intentos = 18
# golpes extra contra el muro). Se recicla la sesion y se enfria.
BLOCK_COOLDOWN_S = (120.0, 300.0, 600.0)  # escalada por reciclaje consecutivo
MAX_BLOCK_RECOVERIES = 5           # reciclajes totales antes de abortar limpio

# Fallos de DOM/red (si: reintentables)
BACKOFF_BASE_S = 8.0               # backoff exponencial: 8s, 16s
CONSECUTIVE_FAILURES_COOLDOWN = 3  # fallos seguidos antes de pausa larga
COOLDOWN_S = 90.0                  # pausa larga de recuperacion

# ---------------------------------------------------------------------------
# Warm-up
# ---------------------------------------------------------------------------
# Entrar directo a /categories/<slug> con un token recien nacido es señal de
# bot. Se recorre home -> /categories con dwell real antes de la fase 2.
WARMUP_DWELL_MIN_S = 3.0
WARMUP_DWELL_MAX_S = 9.0
