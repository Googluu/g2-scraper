"""Normalizacion de URLs.

La version anterior concatenaba BASE_URL + href y generaba
"https://www.g2.com//categories/x". Ese doble slash viajaba en el header
Referer que DataDome registra y ninguna navegacion humana lo produce.
"""
import pytest

from g2_scraper.scraper import absolute_url

CANON = "https://www.g2.com/categories/marketing-automation"


@pytest.mark.parametrize("href", [
  "/categories/marketing-automation",
  "categories/marketing-automation",
  "//categories/marketing-automation",              # barras duplicadas, no protocol-relative
  "///categories/marketing-automation",
  "  /categories/marketing-automation  ",
  "https://www.g2.com//categories/marketing-automation",   # legado en categories.json
  "https://www.g2.com/categories/marketing-automation",
])
def test_absolute_url_normalizes(href):
  assert absolute_url(href) == CANON


def test_absolute_url_preserves_query_and_fragment():
  assert absolute_url("/categories/crm?order=top") == \
    "https://www.g2.com/categories/crm?order=top"
  assert absolute_url("/categories/crm#reviews") == \
    "https://www.g2.com/categories/crm#reviews"


def test_no_double_slash_survives():
  for href in ("//a//b//c", "/a//b", "https://www.g2.com//a//b"):
    assert "//" not in absolute_url(href).removeprefix("https://")
