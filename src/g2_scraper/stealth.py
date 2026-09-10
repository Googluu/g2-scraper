# contramedidas anti-deteccion
"""Playwright/CDP expone senales (navigator.webdriver, etc.) que los sistemas
anti-bot usan para bloquear. Se inyecta ANTES de que cargue cada pagina."""

STEALTH_INIT_SCRIPT = r"""
// Oculta el flag de automatizacion
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// Idiomas y plugins "humanos"
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', {
  get: () => [{ name: 'Chrome PDF Viewer' }, { name: 'Chrome PDF Plugin' }]
});

// Objeto chrome presente en un navegador real
if (!window.chrome) { window.chrome = { runtime: {} }; }

// permissions.query coherente
const originalQuery = window.navigator.permissions?.query?.bind(window.navigator.permissions);
if (originalQuery) {
  window.navigator.permissions.query = (params) =>
    params.name === 'notifications'
      ? Promise.resolve({ state: Notification.permission })
      : originalQuery(params);
}

// Hardware plausible
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
"""