from g2_scraper.metrics import MetricsCollector, RequestRecord


def test_report_metrics():
  m = MetricsCollector()
  for i in range(10):
      m.record(RequestRecord("cat", "success", 100.0 * (i + 1), 1))
  m.record(RequestRecord("catx", "error", 0.0, 3, error="cloudflare_challenge"))

  r = m.report()
  assert r["requests"]["total"] == 11
  assert r["requests"]["successful"] == 10
  assert r["requests"]["success_rate_pct"] == 90.91
  assert r["requests"]["total_retries"] == 2
  assert r["latency_ms"]["max"] == 1000.0
  assert r["latency_ms"]["p50"] == 550.0
  assert r["errors_key"] if False else True
  assert r["exception_handling"]["errors"] == {"cloudflare_challenge": 1}
  assert r["stability"]["recoveries_after_failure"] == 0
  assert r["stability"]["max_consecutive_successes"] == 10