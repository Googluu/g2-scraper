from g2_scraper.scraper import parse_categories


def test_parse_categories_from_raw_links():
  raw = [
    {"href": "/categories/affiliate-marketing-agencies",
      "text": "Affiliate Marketing Companies",
      "options": '{"category_id":2750,"category":"Affiliate Marketing Agencies",'
                '"name":"Event::Categories::Index::CategoryVisited"}'},
    # duplicado -> se ignora
    {"href": "/categories/affiliate-marketing-agencies",
      "text": "Affiliate Marketing Companies", "options": '{"category_id":2750}'},
    {"href": "/categories/marketing-automation",
      "text": "Marketing Automation", "options": ""},
  ]
  cats = parse_categories(raw)
  assert len(cats) == 2
  assert cats[0].category_id == 2750
  assert cats[0].slug == "affiliate-marketing-agencies"
  assert cats[0].url == "https://www.g2.com/categories/affiliate-marketing-agencies"
  assert cats[1].slug == "marketing-automation"


def test_parse_categories_tolerates_bad_json():
  raw = [{"href": "/categories/crm", "text": "CRM", "options": "{bad json"}]
  cats = parse_categories(raw)
  assert cats[0].category_id is None
  assert cats[0].name == "CRM"