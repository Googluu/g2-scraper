"""Valida la logica de extraccion contra un fixture estatico del DOM real
(sin red, determinista y rapido)."""
from pathlib import Path

from playwright.sync_api import sync_playwright

from g2_scraper.scraper import EXTRACT_CARD_JS

FIXTURE = Path(__file__).parent / "fixtures" / "product_card.html"


def test_extract_first_card():
  html = FIXTURE.read_text(encoding="utf-8")
  with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    page = browser.new_page()
    page.set_content(html)
    data = page.locator(".category-product-card").first.evaluate(EXTRACT_CARD_JS)
    browser.close()

  assert data["product_id"] == 141629
  assert data["product_uuid"] == "bb3def6a-dea8-4851-b1b4-1bd2750ebb28"
  assert data["product_name"] == "Acceleration Partners"
  assert data["product_slug"] == "acceleration-partners"
  assert data["product_url"] == "https://www.g2.com/products/acceleration-partners/reviews"
  assert data["product_type"] == "Provider"
  assert data["vendor_name"] == "Acceleration Partners"
  assert data["rating"] == 4.6
  assert data["reviews_count"] == 8
  assert data["description"].startswith("Founded in 2007")
  assert data["description"].endswith("category.")
  assert "Show More" not in data["description"]   # sin ruido
  assert data["image_url"].startswith("https://images.g2crowd.com/")