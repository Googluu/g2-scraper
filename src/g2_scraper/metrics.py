"""Metricas de rendimiento (requisito #3 de la prueba).

El reporte se estructura sobre las cuatro secciones obligatorias:
  - Tasa de exito   -> capturas limpias vs. errores de acceso vs. errores de DOM
  - Estabilidad     -> rachas, recuperaciones y deriva de latencia (iter. 1 vs 99)
  - Manejo de excep.-> estrategia + desglose de errores por clase
  - Latencia        -> media, p50, p95, min, max por extraccion exitosa
"""
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import fmean

SUCCESS = "success"
BLOCKED = "blocked"
ERROR = "error"

# Campos que deben venir poblados en una captura limpia. Sirven para el
# criterio "que la informacion extraida no contenga ruido o valores nulos por
# fallos de carga dinamica".
CORE_FIELDS = (
  "product_id", "product_name", "product_url", "product_type",
  "vendor_name", "rating", "reviews_count", "description", "image_url",
)


@dataclass
class RequestRecord:
  category_slug: str
  status: str               # "success" | "error" | "blocked"
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


def dataset_quality(rows: list[dict]) -> dict:
  """Audita el dataset: cobertura por campo y muestras completas.

  Responde al criterio de "Calidad de Datos" de forma medible en lugar de
  declarativa.
  """
  total = len(rows)
  if not total:
    return {"samples": 0, "field_coverage_pct": {}, "fully_populated_pct": 0.0}

  coverage: dict[str, float] = {}
  for name in CORE_FIELDS:
    filled = sum(1 for r in rows if r.get(name) not in (None, "", []))
    coverage[name] = round(100.0 * filled / total, 2)

  complete = sum(
    1 for r in rows
    if all(r.get(name) not in (None, "", []) for name in CORE_FIELDS)
  )
  return {
    "samples": total,
    "field_coverage_pct": coverage,
    "fully_populated_pct": round(100.0 * complete / total, 2),
    "via_subcategory": sum(1 for r in rows if r.get("resolved_via_subcategory")),
    "unique_products": len({r.get("product_id") for r in rows if r.get("product_id")}),
  }


class MetricsCollector:
  def __init__(self) -> None:
    self.records: list[RequestRecord] = []
    self.started_at = time.time()
    self._ok_streak = 0
    self._fail_streak = 0
    self.max_ok_streak = 0
    self.max_fail_streak = 0
    self.recoveries = 0  # exitos inmediatamente posteriores a un fallo
    # Trazabilidad anti-bot
    self.block_signals: dict[str, int] = {}
    self.session_recycles = 0

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

  def note_block(self, signal: str) -> None:
    self.block_signals[signal] = self.block_signals.get(signal, 0) + 1

  def note_recycle(self) -> None:
    self.session_recycles += 1

  def _drift(self) -> dict:
    """Compara el arranque con el final: responde a "¿la iteracion 99 se
    comporta como la 1?" con numeros en vez de con una afirmacion."""
    ok = [r for r in self.records if r.status == SUCCESS]
    window = max(1, min(10, len(ok) // 2))
    if len(ok) < 2:
      return {"window": 0, "first_mean_ms": 0.0, "last_mean_ms": 0.0, "delta_pct": 0.0}
    first = fmean(r.latency_ms for r in ok[:window])
    last = fmean(r.latency_ms for r in ok[-window:])
    return {
      "window": window,
      "first_mean_ms": round(first, 1),
      "last_mean_ms": round(last, 1),
      "delta_pct": round(100.0 * (last - first) / first, 2) if first else 0.0,
    }

  def report(self) -> dict:
    total = len(self.records)
    ok = [r for r in self.records if r.status == SUCCESS]
    blocked = [r for r in self.records if r.status == BLOCKED]
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
        "blocked": len(blocked),
        "dom_errors": len(failed) - len(blocked),
        "success_rate_pct": round(100.0 * len(ok) / total, 2) if total else 0.0,
        "access_error_rate_pct": round(100.0 * len(blocked) / total, 2) if total else 0.0,
        "total_retries": sum(r.attempts - 1 for r in self.records),
        "throughput_req_per_min": (
          round(60.0 * total / (time.time() - self.started_at), 2)
          if time.time() > self.started_at else 0.0
        ),
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
        "latency_drift": self._drift(),
      },
      "anti_bot": {
        "provider": "DataDome (G2.com)",
        "blocks_detected": sum(self.block_signals.values()),
        "signals": self.block_signals,
        "session_recycles": self.session_recycles,
      },
      "exception_handling": {
        "strategy": (
          "taxonomia de errores con tratamiento opuesto: los bloqueos anti-bot "
          "NO se reintentan (se recicla el token datadome, se re-calienta la "
          "sesion y se enfria con escalada 120/300/600s), mientras los fallos de "
          "DOM/red si se reintentan con backoff exponencial (8/16s) y cascada de "
          "selectores. Ritmo log-normal con presupuesto por sesion, y checkpoint "
          "en JSONL que hace la corrida reanudable sin repetir categorias"
        ),
        "errors": errors,
      },
    }
