"""Motor de extraccion de G2.com.

Fase 1: /categories -> inventario de categorias (form#categorySearch).
Fase 2: por cada categoria, navega y extrae la primera product-card.

Fuente primaria de datos: atributos data-event-options (JSON limpio incrustado
por G2). Fallback: texto del DOM.
"""
import json
import time
from urllib.parse import urlparse

from playwright.async_api import Page, TimeoutError as PWTimeout

from .config import (
  BASE_URL, CATEGORIES_URL, CHALLENGE_WAIT_S,
  ELEMENT_TIMEOUT_MS, NAV_TIMEOUT_MS,
)
from .models import Category


class NavigationError(Exception):
  """No se pudo cargar la pagina (timeout, challenge, red)."""


class DomChangedError(Exception):
  """La pagina cargo pero el DOM no contiene lo esperado."""


# ---------------------------------------------------------------------------
# Navegacion resiliente
# ---------------------------------------------------------------------------

async def _title(page: Page) -> str:
  try:
    return (await page.title()).lower()
  except Exception:
    return ""


async def _challenge_present(page: Page) -> bool:
  title = await _title(page)
  if "just a moment" in title or "attention required" in title:
    return True
  try:
    n = await page.locator(
      "#challenge-running, #challenge-stage, iframe[src*='challenges.cloudflare.com']"
    ).count()
    return n > 0
  except Exception:
    return False


async def safe_goto(page: Page, url: str) -> None:
  """Navega a `url` g2.

  - `domcontentloaded`: mas rapido y tolerante que networkidle.
  - Si Cloudflare presenta challenge, espera a que se resuelva.
  """
  try:
    await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
  except PWTimeout:
    # El DOM puede estar listo aunque recursos lentos disparen el timeout
    if not page.url.startswith("http"):
      raise NavigationError("goto_timeout")

  deadline = time.monotonic() + CHALLENGE_WAIT_S
  while time.monotonic() < deadline:
    if not await _challenge_present(page):
      return
    await page.wait_for_timeout(1500)
  raise NavigationError("cloudflare_challenge")


async def accept_cookies(page: Page) -> None:
  """Cierra el banner de cookies (OneTrust u otros) si aparece."""
  for selector in ("#onetrust-accept-btn-handler", "button:has-text('Accept')", "#\36 24a1a1f-785f-4407-a368-42444c0ef4f2 > div.osano-cm-dialog__buttons.osano-cm-buttons > button.osano-cm-accept-all.osano-cm-buttons__button.osano-cm-button.osano-cm-button--type_accept"):
    try:
      btn = page.locator(selector).first
      if await btn.count():
        await btn.click(timeout=3000)
        await page.wait_for_timeout(500)
        return
    except Exception:
      continue


# ---------------------------------------------------------------------------
# Fase 1: categorías desde /categories
# ---------------------------------------------------------------------------

_CATEGORIES_JS = r"""
els => els.map(el => ({
  href: el.getAttribute('href') || '',
  text: (el.textContent || '').trim(),
  options: el.getAttribute('data-event-options') || '',
}))
"""


def parse_categories(raw: list[dict]) -> list[Category]:
  """Convierte el DOM crudo del form#categorySearch en categorias.

  El selector apunta a todos los <a href="/categories/..."> del formulario;
  data-event-options trae JSON limpio con category_id y nombre.
  """
  categories: list[Category] = []
  seen: set[str] = set()
  for item in raw:
    href = item.get("href", "")
    slug = href.removeprefix("/categories/").strip("/")
    if not slug or slug in seen:
      continue
    try:
      opts = json.loads(item.get("options") or "{}")
    except json.JSONDecodeError:
      opts = {}
    seen.add(slug)
    categories.append(Category(
      category_id=opts.get("category_id"),
      slug=slug,
      name=item.get("text") or opts.get("category") or slug.replace("-", " ").title(),
      url=BASE_URL + href,
    ))
  return categories


async def collect_categories(page: Page) -> list[Category]:
  await safe_goto(page, CATEGORIES_URL)
  await accept_cookies(page)
  await page.wait_for_selector("form#categorySearch", state="attached",
                              timeout=ELEMENT_TIMEOUT_MS)
  # Scroll completo para disparar lazy-loading del listado
  await page.evaluate("""
    async () => {
      for (let y = 0; y <= document.body.scrollHeight; y += 800) {
        window.scrollTo(0, y);
        await new Promise(r => setTimeout(r, 100));
      }
      window.scrollTo(0, 0);
    }
  """)
  await page.wait_for_timeout(1000)
  raw = await page.eval_on_selector_all(
    "form#categorySearch a[href^='/categories/']", _CATEGORIES_JS
  )
  return parse_categories(raw)


# ---------------------------------------------------------------------------
# Fase 2: primera product-card de una categorIa
# ---------------------------------------------------------------------------

EXTRACT_CARD_JS = r"""
el => {
  const opts = node => {
    try {
      return JSON.parse((node && node.getAttribute) ? (node.getAttribute('data-event-options') || '{}') : '{}');
    } catch (e) { return {}; }
  };

  const productLink = el.querySelector("a[href*='/products/']");
  const vendorLink  = el.querySelector("a[href*='/sellers/']");
  const o = opts(productLink);   // JSON limpio incrustado por G2
  const v = opts(vendorLink);

  const ratingText = (el.querySelector("[class*='star-wrapper__desc__rating']") || {}).textContent || '';
  const countText  = (el.querySelector("[class*='star-wrapper__desc__count']") || {}).textContent || '';

  // Descripción: la parte visible truncada + la continuación oculta (.hide-if-js)
  let description = null;
  const descP = el.querySelector('[data-nosnippet] p');
  if (descP) {
    const clone = descP.cloneNode(true);
    clone.querySelectorAll('a, script').forEach(n => n.remove());
    description = (clone.textContent || '')
        .replace(/\s+/g, ' ')
        .replace(/\s*\.{3,}/g, '')   // elipsis del truncado
        .trim() || null;
  }

  const img = el.querySelector('img');
  const nameEl = el.querySelector('.a--md');

  let productSlug = null;
  let productUrl = null;
  if (productLink) {
    productUrl = productLink.href;
    try {
      const parts = new URL(productUrl).pathname.split('/products/');
      productSlug = (parts[1] || '').split('/')[0] || null;
    } catch (e) {}
  }

  return {
    product_id:      o.product_id ?? null,
    product_uuid:    o.product_uuid ?? null,
    product_name:    o.product || (nameEl ? nameEl.textContent.trim() : null),
    product_slug:    productSlug,
    product_url:     productUrl,
    product_type:    o.product_type ?? null,
    vendor_name:     v.vendor || (vendorLink ? vendorLink.textContent.trim() : null),
    vendor_url:      vendorLink ? vendorLink.href : null,
    rating:          ratingText.trim() ? parseFloat(ratingText) : null,
    reviews_count:   countText.trim() ? parseInt(countText.replace(/[^\d]/g, ''), 10) : null,
    description:     description,
    image_url:       img ? img.src : null,
  };
}
"""

# Selectores en orden de preferencia: si G2 cambia el DOM, probamos el siguiente
CARD_SELECTORS = [
  "[data-ordered-events-item='products'] .category-product-card",
  ".category-product-card",
  "[data-ordered-events-item='products'] .content-card",
  ".content-card",
]

# --- Sub-categorías ---------------------------------------------------------
# Algunas categorías no listan productos: muestran enlaces a sub-categorías:
#   <a class="link link--chevron" href="/categories/ai-agents">
#     <div class="link--chevron__content"> AI Agents</div>
#   </a>
# En ese caso navegamos a la primera sub-categoría válida y repetimos ahí.
SUBCATEGORY_SELECTORS = [
    "main a.link--chevron[href^='/categories/']",     # patrón observado, acotado al contenido
    "a.link--chevron[href^='/categories/']",          # patrón observado, sin acotar
    "main a[href^='/categories/']:not([href$='#'])",  # último recurso dentro del contenido
]

MAX_SUBCATEGORY_HOPS = 3        # profundidad máxima (evita cadenas infinitas)
SUBCATEGORY_TIMEOUT_MS = 4_000  # espera al localizar sub-categorías
FALLBACK_TIMEOUT_MS = 5_000     # timeout corto para selectores de respaldo de tarjetas


async def _wait_first_card(page: Page):
    """Primera product-card probando los selectores en cascada.

    El primer selector usa el timeout completo (caso común); los de respaldo
    usan uno corto, así las páginas SIN tarjetas no demoran demasiado antes
    de caer a la lógica de sub-categorías.
    """
    for i, selector in enumerate(CARD_SELECTORS):
        timeout = ELEMENT_TIMEOUT_MS if i == 0 else FALLBACK_TIMEOUT_MS
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="attached", timeout=timeout)
            return locator
        except PWTimeout:
            continue
    return None


_SUBCATEGORY_JS = r"""
els => els.map(el => ({
  href: el.getAttribute('href') || '',
  name: (el.querySelector('.link--chevron__content') || el).textContent.trim(),
}))
"""

# en algunos casos G2 no lista sub-categorias, pero si enlaces a otras categorias por si no encontramos la product-card, entonces nevagamos a la primera sub-categoria valida y repetimos ahi
async def _collect_subcategories(page: Page) -> list[dict]:
  """Sub-categorias visibles (href + nombre), en orden del DOM."""
  for selector in SUBCATEGORY_SELECTORS:
    locator = page.locator(selector)
    try:
      await locator.first.wait_for(state="attached", timeout=SUBCATEGORY_TIMEOUT_MS)
    except PWTimeout:
      continue
    try:
      return await locator.evaluate_all(_SUBCATEGORY_JS)
    except Exception:
      continue
  return []


def _pick_subcategory(candidates: list[dict], current_slug: str,
                      visited: set[str]) -> tuple[str, str, str] | None:
  """Elige la primera sub-categoria usable -> (slug, nombre, url).

  Descarta: enlaces que no apunten a /categories/<slug>, la categoria actual
  (p. ej. breadcrumbs que apuntan a si mismas) y las ya visitadas (ciclos).
  """
  for cand in candidates:
    href = (cand or {}).get("href", "")
    if not href or href in ("/categories", "/categories/"):
      continue
    slug = urlparse(href).path.removeprefix("/categories/").strip("/")
    if not slug or slug == current_slug or slug in visited:
      continue
    url = href if href.startswith("http") else BASE_URL + href
    return slug, (cand or {}).get("name") or slug.replace("-", " ").title(), url
  return None


async def extract_first_product(
    page: Page,
    category: Category,
    *,
    _hops: list[str] | None = None,
    _visited: set[str] | None = None,
) -> dict:
  """Navega a la categoria y extrae la primera product-card como dict.

  Fallback de sub-categorias: si la categoria no muestra product-cards sino
  un listado de sub-categorias (enlaces `link--chevron`), navega a la
  PRIMERA sub-categoria valida y reintenta la extraccion alli, con limite
  de profundidad (MAX_SUBCATEGORY_HOPS) y deteccion de ciclos.
  """
  if _hops is None:
    _hops = []
  if _visited is None:
    _visited = set()

  if category.slug in _visited:
    raise DomChangedError(f"subcategory_loop:{category.slug}")
  _visited.add(category.slug)

  if len(_hops) >= MAX_SUBCATEGORY_HOPS:
    raise DomChangedError("subcategory_depth_exceeded")

  await safe_goto(page, category.url)

  # 1) Camino normal: ¿hay product-cards?
  card = await _wait_first_card(page)
  if card is not None:
    data = await card.evaluate(EXTRACT_CARD_JS)
    if not data or not data.get("product_name"):
      raise DomChangedError("empty_card")
    # Trazabilidad: como se resolvio esta muestra (queda en el dataset)
    data["subcategory_hops"] = list(_hops)
    data["resolved_via_subcategory"] = bool(_hops)
    return data

  # 2) No hay tarjetas -> probablemente es un listado de sub-categorias
  candidates = await _collect_subcategories(page)
  picked = _pick_subcategory(candidates, category.slug, _visited)
  if picked is None:
    raise DomChangedError("product_card_not_found")

  sub_slug, sub_name, sub_url = picked
  print(f"    -> sin product-cards; siguiendo sub-categoria: {sub_slug}")
  sub_category = Category(category_id=None, slug=sub_slug, name=sub_name, url=sub_url)
  return await extract_first_product(
    page, sub_category, _hops=_hops + [sub_slug], _visited=_visited,
  )