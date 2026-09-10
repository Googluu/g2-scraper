"""Gestion de la sesion del navegador

Estrategia para el requisito #2: entorno desacoplado:
Conectarse por CDP a un chrome lanzado de forma independiente(scripts/launch_chrome.sh)

"""

from playwright.sync_api import Browser, Page, BrowserContext, Playwright

from .config import NAV_TIMEOUT_MS, PROFILE_DIR
from .stealth import STEALTH_INIT_SCRIPT

_LAUNCH_ARGS = [
  "--disable-blink-features=AutomationControlled",
  "--no-first-run",
  "--no-default-browser-check",
  "--window-size=1440,900",
]

class BrowserSession:
  """Clase para gestionar la sesion del navegador"""

  def __init__(self, cdp_url: str) -> None:
    self.cdp_url = cdp_url
    self.browser: Browser | None = None
    self.context: BrowserContext | None = None
    self._owns_browser = False  # True si lo lanzamos nosotros (fallback)


  async def connect(self, pw: Playwright) -> None:
    try:
      self.browser = await pw.chromium.connect_over_cdp(self.cdp_url, timeout=5000)
      # contexto por defecto del chrome real - usa pperfil persistente
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
    await page.add_init_script(STEALTH_INIT_SCRIPT)
    page.set_default_timeout(NAV_TIMEOUT_MS)
    page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
    return page
  
  async def close(self) -> None:
    if self._owns_browser:
      if self.context:
          await self.context.close()
    elif self.browser:
      # Con CDP solo nos desconectamos: el navegador externo sigue abierto
      await self.browser.close()