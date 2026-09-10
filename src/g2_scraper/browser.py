"""Gestion de la sesion del navegador.

Requisito #2 (entorno desacoplado): se conecta por CDP a un Chrome lanzado de
forma independiente (scripts/launch_chrome.sh). La vida del navegador es
independiente de la del script: si el scraper muere, la sesion (y su token
`datadome` tibio) sobrevive.

NOTA SOBRE STEALTH
------------------
Este modulo NO inyecta parches de fingerprint, y es deliberado. La version
anterior inyectaba un `stealth.py` que, con un Chrome **real** por CDP, empeora
la deteccion en lugar de mejorarla:

  - `navigator.webdriver`: Chrome lanzado solo con --remote-debugging-port ya
    reporta `false`. El parche lo ponia en `undefined`, valor que ningun
    navegador real devuelve: creaba la anomalia que pretendia tapar.
  - `Object.defineProperty(navigator, ...)` deja la propiedad como *own
    property* de la instancia cuando nativamente vive en `Navigator.prototype`;
    un `getOwnPropertyDescriptor` lo delata, igual que el `toString()` del
    getter (codigo de flecha en lugar de `[native code]`).
  - `navigator.plugins` quedaba como array plano de 2 objetos con solo `name`,
    en vez de un `PluginArray` con `item()`/`namedItem()`.
  - `navigator.languages = ['en-US','en']` contradecia el header
    `Accept-Language` real del navegador. El mismatch header <-> JS es uno de
    los checks mas baratos y fiables que existen.

Con un navegador real, la superficie de fingerprint ya es autentica: lo que hay
que corregir es el *comportamiento* (ritmo, referer, interaccion), no mentir
sobre el entorno. Ver antibot.py.
"""
import re

from playwright.async_api import Browser, BrowserContext, Page, Playwright

from .config import NAV_TIMEOUT_MS, PROFILE_DIR

_LAUNCH_ARGS = [
  "--disable-blink-features=AutomationControlled",
  "--no-first-run",
  "--no-default-browser-check",
  "--window-size=1440,900",
]

# Origenes cuyo storage se limpia al reciclar la sesion
_G2_ORIGINS = ("https://www.g2.com", "https://g2.com")

# Cualquier subdominio de g2.com: el token `datadome` puede estar sembrado en
# ".g2.com" o en "www.g2.com" segun la respuesta que lo emitio
_G2_COOKIE_DOMAIN = re.compile(r"(^|\.)g2\.com$")

_STORAGE_TYPES = "local_storage,session_storage,indexeddb,cache_storage,service_workers,websql"


class BrowserSession:
  """Clase para gestionar la sesion del navegador."""

  def __init__(self, cdp_url: str) -> None:
    self.cdp_url = cdp_url
    self.browser: Browser | None = None
    self.context: BrowserContext | None = None
    self._owns_browser = False  # True si lo lanzamos nosotros (fallback)

  async def connect(self, pw: Playwright) -> None:
    try:
      self.browser = await pw.chromium.connect_over_cdp(self.cdp_url, timeout=5000)
      # contexto por defecto del chrome real - usa perfil persistente
      self.context = self.browser.contexts[0]
      print(f"[browser] conectado por CDP -> {self.cdp_url} (navegador externo)")
    except Exception as exc:
      print(f"[browser] CDP no disponible ({type(exc).__name__}). "
            f"Lanzando Chromium local persistente (fallback)...")

      try:
        self.context = await pw.chromium.launch_persistent_context(
          str(PROFILE_DIR), headless=False, channel="chrome",
          args=_LAUNCH_ARGS, viewport={"width": 1440, "height": 900},
        )
      except Exception:
        self.context = await pw.chromium.launch_persistent_context(
          str(PROFILE_DIR), headless=False,
          args=_LAUNCH_ARGS, viewport={"width": 1440, "height": 900},
        )
      self._owns_browser = True

  async def new_page(self) -> Page:
    assert self.context is not None, "sesion no conectada"
    page = await self.context.new_page()
    page.set_default_timeout(NAV_TIMEOUT_MS)
    page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
    return page

  # -------------------------------------------------------------------------
  # Reciclaje de sesion
  # -------------------------------------------------------------------------

  async def clear_site_data(self, page: Page) -> None:
    """Descarta el token `datadome` y todo el storage de g2.com.

    Es la operacion que en las pruebas manuales restauro el acceso: el bloqueo
    esta ligado al token, asi que soltarlo devuelve una sesion limpia sin tocar
    la IP. Se acota a los origenes de G2 para no destruir el resto del perfil.
    """
    assert self.context is not None, "sesion no conectada"

    # 1) Descargar la pagina primero. Con el JS de G2 aun vivo, sus scripts de
    #    analitica vuelven a sembrar cookies mientras se limpia.
    try:
      await page.goto("about:blank", wait_until="domcontentloaded", timeout=10_000)
    except Exception:
      pass

    # 2) Storage por origen (localStorage, sessionStorage, IndexedDB, cache...)
    try:
      cdp = await self.context.new_cdp_session(page)
      try:
        for origin in _G2_ORIGINS:
          await cdp.send("Storage.clearDataForOrigin",
                         {"origin": origin, "storageTypes": _STORAGE_TYPES})
      finally:
        await cdp.detach()
    except Exception as exc:
      print(f"    [sesion] no se pudo limpiar storage por CDP: {type(exc).__name__}")

    # 3) Cookies de cualquier subdominio de g2.com (el token `datadome` incluido)
    try:
      await self.context.clear_cookies(domain=_G2_COOKIE_DOMAIN)
    except Exception:
      # Respaldo si el filtro por patron no esta disponible
      for domain in ("www.g2.com", ".g2.com", "g2.com"):
        try:
          await self.context.clear_cookies(domain=domain)
        except Exception:
          continue

    left = [c["name"] for c in await self.context.cookies()
            if c.get("domain", "").endswith("g2.com")]
    print(f"    [sesion] token liberado (quedan {len(left)} cookies de g2.com"
          f"{', datadome AUN PRESENTE' if 'datadome' in left else ''})")

  async def recycle_page(self, page: Page) -> Page:
    """Cierra la pestaña y abre una nueva: renderer limpio, sin estado en memoria."""
    try:
      await page.close()
    except Exception:
      pass
    return await self.new_page()

  async def close(self) -> None:
    if self._owns_browser:
      if self.context:
        await self.context.close()
    elif self.browser:
      # Con CDP solo nos desconectamos: el navegador externo sigue abierto
      await self.browser.close()
