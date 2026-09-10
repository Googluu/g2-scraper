"""Valida la deteccion de bloqueos contra los artefactos REALES capturados
durante la corrida que se bloqueo (sin red, determinista y rapido).

El caso mas importante es el ultimo: una pagina de categoria normal NO debe
dar falso positivo. Un falso positivo dispararia reciclajes de sesion
innecesarios y arruinaria el rendimiento.
"""
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from g2_scraper.antibot import AUTO_RESOLVABLE, BLOCK_DOM_JS, human_delay
from g2_scraper.config import DELAY_MAX_S, DELAY_MIN_S, READING_BREAK_MAX_S

FIXTURES = Path(__file__).parent / "fixtures"

CASES = [
  ("datadome_hard_block.html", "datadome_hard_block"),
  ("datadome_interstitial.html", "datadome_interstitial"),
  ("datadome_403.html", "datadome_js_challenge"),
  ("product_card.html", None),   # pagina sana: sin falso positivo
]


@pytest.fixture(scope="module")
def detect():
  """Evalua BLOCK_DOM_JS sobre un fixture, con la red cortada.

  Cada caso usa una pestaña nueva: los fixtures traen scripts inline que
  definen globals (`window.dd`) y reutilizar la pagina los filtraria al caso
  siguiente. En produccion cada navegacion crea un contexto JS limpio, asi que
  la pestaña por caso es lo que reproduce la condicion real.
  """
  with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)

    def _run(filename: str):
      page = browser.new_page()
      # Sin red: los fixtures referencian assets de captcha-delivery.com
      page.route("**/*", lambda route: route.abort())
      try:
        page.set_content((FIXTURES / filename).read_text(encoding="utf-8"))
        return page.evaluate(BLOCK_DOM_JS)
      finally:
        page.close()

    yield _run
    browser.close()


@pytest.mark.parametrize("filename,expected", CASES)
def test_block_detection(detect, filename, expected):
  assert detect(filename) == expected


def test_hard_block_is_not_auto_resolvable(detect):
  """Un veredicto firme no se espera: se recicla la sesion de inmediato."""
  assert detect("datadome_hard_block.html") not in AUTO_RESOLVABLE


def test_js_challenge_is_auto_resolvable(detect):
  """El device check puede pasar solo; vale la pena esperarlo."""
  assert detect("datadome_403.html") in AUTO_RESOLVABLE


def test_human_delay_within_bounds():
  samples = [human_delay() for _ in range(5000)]
  assert min(samples) >= DELAY_MIN_S
  assert max(samples) <= DELAY_MAX_S + READING_BREAK_MAX_S
  # Cola larga: el promedio debe superar claramente la mediana, a diferencia
  # de un uniform() cuya firma plana es detectable.
  ordered = sorted(samples)
  median = ordered[len(ordered) // 2]
  assert sum(samples) / len(samples) > median
