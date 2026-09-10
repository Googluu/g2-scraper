"""Deteccion de bloqueos y simulacion de comportamiento humano.

G2.com esta protegido por **DataDome**, no por Cloudflare. Evidencia recogida
en las pruebas (ver docs/ARQUITECTURA.md):

  - respuesta 403 con el bootstrap `var dd = {...}` + `ct.captcha-delivery.com`
  - interstitial en `<iframe src="geo.captcha-delivery.com/interstitial/...">`
  - pagina de bloqueo con marca G2: "Access is temporarily restricted"

DataDome emite una cookie `datadome` que identifica al dispositivo/sesion y le
acumula un score. Cuando cruza el umbral, marca **ese token** como bloqueado:
el 403 acompaña a la cookie, no a la IP. Se comprobo experimentalmente:

  | IP      | token   | resultado |
  |---------|---------|-----------|
  | misma   | otro    | OK        |  (navegador personal, misma red)
  | otra    | mismo   | BLOQUEADO |  (VPN + perfil quemado)
  | misma   | nuevo   | OK        |  (storage limpiado)

De ahi las dos piezas de este modulo: reconocer el bloqueo a tiempo (para no
seguir golpeando) y reducir las señales que suben el score.
"""
import asyncio
import math
import random

from playwright.async_api import Page, Response

from .config import (
  DELAY_MAX_S, DELAY_MEDIAN_S, DELAY_MIN_S, DELAY_SIGMA,
  DWELL_MAX_S, DWELL_MIN_S,
  READING_BREAK_MAX_S, READING_BREAK_MIN_S, READING_BREAK_PROB,
)


class BlockedError(Exception):
  """Interrupcion por sistema anti-bot.

  Se distingue de los errores de DOM/red porque tiene un tratamiento opuesto:
  un bloqueo **no se reintenta**, se recicla la sesion y se enfria.
  """

  def __init__(self, signal: str) -> None:
    super().__init__(signal)
    self.signal = signal


# ---------------------------------------------------------------------------
# Deteccion
# ---------------------------------------------------------------------------

# Codigos que DataDome usa para denegar acceso
BLOCK_STATUS = {403: "datadome_403", 429: "rate_limited_429"}

BLOCK_TITLES = {
  "you have been blocked": "datadome_block_page",
  "access to this page has been denied": "datadome_block_page",
  "access is temporarily restricted": "datadome_temporary_block",
  # Cloudflare, por si G2 lo suma en el futuro
  "just a moment": "cloudflare_challenge",
  "attention required": "cloudflare_challenge",
}

# Marcadores en el DOM. Se evita deliberadamente buscar cualquier
# `script[src*='captcha-delivery.com']`: el tag de DataDome puede estar
# presente en paginas normales y produciria falsos positivos en todo.
BLOCK_DOM_JS = r"""
() => {
  const q = (sel) => document.querySelector(sel);

  // 1) Marcador de primera clase de DataDome en sus paginas de respuesta:
  //    <body class="dd-response-page--hard-block" data-dd-response-page="hard-block">
  //    Es la señal mas fiable y ademas dice de QUE tipo de respuesta se trata.
  const ddPage = q('[data-dd-response-page]');
  if (ddPage) {
    const kind = ddPage.getAttribute('data-dd-response-page') || 'unknown';
    return kind === 'hard-block' ? 'datadome_hard_block'
                                 : 'datadome_response_page:' + kind;
  }
  if (q('[data-dd-captcha-container]')) return 'datadome_captcha';

  // 2) Interstitial / captcha de DataDome embebido en iframe
  if (q("iframe[src*='captcha-delivery.com']")) return 'datadome_interstitial';

  // 3) Pagina 403 de arranque de DataDome: global `dd` con cid/hsh
  const dd = window.dd;
  if (dd && typeof dd === 'object' && (dd.cid || dd.hsh)) return 'datadome_js_challenge';
  if (q('#cmsg') && !q('main')) return 'datadome_js_challenge';

  // 4) Respaldo por texto, si DataDome cambia el markup. Se acota a documentos
  //    cortos: las paginas de bloqueo son minimas, una categoria real tiene
  //    miles de caracteres y podria mencionar estas frases en una descripcion.
  const text = document.body ? (document.body.innerText || '') : '';
  if (text.length < 3000 &&
      /access is temporarily restricted|you have been blocked|unusual activity from your (device|network)/i.test(text))
    return 'datadome_temporary_block';

  // 5) Cloudflare
  if (q('#challenge-running') || q('#challenge-stage') ||
      q("iframe[src*='challenges.cloudflare.com']")) return 'cloudflare_challenge';

  return null;
}
"""

# Señales que DataDome/Cloudflare pueden resolver solos: el device check corre
# en el cliente y, si pasa, recargan la pagina con una cookie valida. Merece la
# pena esperarlas. El resto es un veredicto firme y esperar solo empeora.
AUTO_RESOLVABLE = frozenset({
  "datadome_js_challenge",
  "datadome_interstitial",
  "cloudflare_challenge",
})


async def _title(page: Page) -> str:
  try:
    return (await page.title()).lower()
  except Exception:
    return ""


async def detect_block(page: Page, response: Response | None = None) -> str | None:
  """Devuelve el identificador de la señal de bloqueo, o None si la pagina esta sana.

  Orden: DOM -> titulo -> status. El DOM es lo mas **especifico** (distingue un
  device check auto-resoluble de un bloqueo firme, ambos servidos con 403), y el
  status queda como red de seguridad para respuestas sin cuerpo util.
  """
  try:
    signal = await page.evaluate(BLOCK_DOM_JS)
  except Exception:
    signal = None
  if signal:
    return signal

  title = await _title(page)
  for needle, mapped in BLOCK_TITLES.items():
    if needle in title:
      return mapped

  if response is not None:
    return BLOCK_STATUS.get(response.status)
  return None


# ---------------------------------------------------------------------------
# Ritmo humano
# ---------------------------------------------------------------------------

def human_delay() -> float:
  """Pausa entre solicitudes: log-normal + pausa de lectura ocasional.

  La mediana es DELAY_MEDIAN_S, pero la cola larga produce esperas de decenas
  de segundos de vez en cuando. Eso reproduce la varianza real de una persona
  navegando, a diferencia de un uniform() cuya firma es plana.
  """
  base = random.lognormvariate(math.log(DELAY_MEDIAN_S), DELAY_SIGMA)
  base = min(max(base, DELAY_MIN_S), DELAY_MAX_S)
  if random.random() < READING_BREAK_PROB:
    base += random.uniform(READING_BREAK_MIN_S, READING_BREAK_MAX_S)
  return base


async def human_dwell(page: Page, *, deep: bool = False) -> None:
  """Movimiento de mouse + scroll antes de extraer.

  El cliente de DataDome (`c.js`) recolecta eventos de mouse, scroll y teclado.
  Cien vistas de pagina con cero eventos de interaccion es, por si misma, una
  anomalia. Nunca propaga excepciones: es ruido cosmetico, no debe tumbar la
  extraccion.
  """
  try:
    for _ in range(random.randint(2, 4)):
      await page.mouse.move(
        random.randint(80, 1360), random.randint(90, 820),
        steps=random.randint(4, 14),
      )
      await asyncio.sleep(random.uniform(0.12, 0.45))

    for _ in range(random.randint(3, 6) if deep else random.randint(1, 3)):
      await page.mouse.wheel(0, random.randint(220, 680))
      await asyncio.sleep(random.uniform(0.35, 1.2))

    # Scroll de vuelta hacia arriba: patron tipico al revisar un listado
    if random.random() < 0.45:
      await page.mouse.wheel(0, -random.randint(120, 400))
      await asyncio.sleep(random.uniform(0.25, 0.8))

    await asyncio.sleep(random.uniform(DWELL_MIN_S, DWELL_MAX_S))
  except Exception:
    pass
