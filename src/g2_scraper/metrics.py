"""Metricas de rendimiento"""
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import fmean

SUCCESS = "success"


@dataclass
class RequestRecord:
  category_slug: str
  status: str               # "success" | "error"
  latency_ms: float         # latencia del intento definitivo
  attempts: int             # intentos consumidos
  error: str | None = None
  ts: float = field(default_factory=time.time)


def _percentile(values: list[float], p: float) -> float:
  if not values:
    return 0.0
  ordered = sorted(values)
  k = (len(ordered) - 1) * p
  f = int(k)
  c = min(f + 1, len(ordered) - 1)
  return ordered[f] + (ordered[c] - ordered[f]) * (k - f)


class MetricsCollector:
  def __init__(self) -> None:
    self.records: list[RequestRecord] = []
    self.started_at = time.time()
    self._ok_streak = 0
    self._fail_streak = 0
    self.max_ok_streak = 0
    self.max_fail_streak = 0
    self.recoveries = 0  # exitos inmediatamente posteriores a un fallo

  def record(self, rec: RequestRecord) -> None:
    self.records.append(rec)
    if rec.status == SUCCESS:
      if self._fail_streak > 0:
        self.recoveries += 1
      self._fail_streak = 0
      self._ok_streak += 1
      self.max_ok_streak = max(self.max_ok_streak, self._ok_streak)
    else:
      self._ok_streak = 0
      self._fail_streak += 1
      self.max_fail_streak = max(self.max_fail_streak, self._fail_streak)

  def report(self) -> dict:
    total = len(self.records)
    ok = [r for r in self.records if r.status == SUCCESS]
    failed = [r for r in self.records if r.status != SUCCESS]
    latencies = [r.latency_ms for r in ok]
    errors: dict[str, int] = {}
    for r in failed:
      key = r.error or "unknown"
      errors[key] = errors.get(key, 0) + 1

    return {
      "generated_at": datetime.now(timezone.utc).isoformat(),
      "duration_s": round(time.time() - self.started_at, 1),
      "requests": {
        "total": total,
        "successful": len(ok),
        "failed": len(failed),
        "success_rate_pct": round(100.0 * len(ok) / total, 2) if total else 0.0,
        "total_retries": sum(r.attempts - 1 for r in self.records),
      },
      "latency_ms": {
        "mean": round(fmean(latencies), 1) if latencies else 0.0,
        "p50": round(_percentile(latencies, 0.50), 1),
        "p95": round(_percentile(latencies, 0.95), 1),
        "min": round(min(latencies), 1) if latencies else 0.0,
        "max": round(max(latencies), 1) if latencies else 0.0,
      },
      "stability": {
        "max_consecutive_successes": self.max_ok_streak,
        "max_consecutive_failures": self.max_fail_streak,
        "recoveries_after_failure": self.recoveries,
      },
      "exception_handling": {
        "strategy": ("reintentos con backoff exponencial (8/16/24s), cooldown de 90s "
                      "tras 3 fallos consecutivos, fallback de selectores ante cambios "
                      "de DOM, espera activa de challenges de Cloudflare, resumible "
                      "desde JSONL"),
        "errors": errors,
      },
    }
