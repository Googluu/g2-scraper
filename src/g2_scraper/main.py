"""Punto de entrada CLI.

Uso:
  uv run g2-scraper --limit 5      # smoke test
  uv run g2-scraper                # 100 solicitudes (por defecto)
  uv run g2-scraper --no-resume    # empezar de cero
"""
import argparse
import asyncio
import json
import random
import time
from pathlib import Path

from playwright.async_api import async_playwright

from .browser import BrowserSession
from .config import (
  BACKOFF_BASE_S, BASE_URL, CATEGORIES_FILE, CATEGORIES_URL, CDP_URL,
  COOLDOWN_S, CONSECUTIVE_FAILURES_COOLDOWN, DATA_DIR, FAILURES_FILE,
  MAX_ATTEMPTS, MAX_DELAY_S, MAX_REQUESTS, MIN_DELAY_S,
  PRODUCTS_FILE, REPORT_FILE,
)
from .metrics import MetricsCollector, RequestRecord
from .models import Category, ProductRecord
from .scraper import (
  DomChangedError, NavigationError, accept_cookies,
  collect_categories, extract_first_product, safe_goto,
)


def append_jsonl(path: Path, payload: dict) -> None:
  with path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def load_completed_slugs() -> set[str]:
  """Permite reanudar una ejecucion interrumpida sin repetir categorias."""
  if not PRODUCTS_FILE.exists():
    return set()
  slugs: set[str] = set()
  for line in PRODUCTS_FILE.read_text(encoding="utf-8").splitlines():
    if line.strip():
      try:
        slugs.add(json.loads(line)["category_slug"])
      except (json.JSONDecodeError, KeyError):
        continue
  return slugs


def parse_args() -> argparse.Namespace:
  ap = argparse.ArgumentParser(description="Scraper resiliente de G2.com")
  ap.add_argument("--limit", type=int, default=MAX_REQUESTS,
                  help=f"solicitudes objetivo (default: {MAX_REQUESTS})")
  ap.add_argument("--cdp", default=CDP_URL, help="URL CDP del navegador externo")
  ap.add_argument("--min-delay", type=float, default=MIN_DELAY_S)
  ap.add_argument("--max-delay", type=float, default=MAX_DELAY_S)
  ap.add_argument("--refresh-categories", action="store_true",
                  help="recolectar categorias aunque exista el archivo")
  ap.add_argument("--no-resume", action="store_true",
                  help="ignorar progreso previo")
  return ap.parse_args()


async def run(args: argparse.Namespace) -> int:
  DATA_DIR.mkdir(parents=True, exist_ok=True)
  metrics = MetricsCollector()

  async with async_playwright() as pw:
    session = BrowserSession(args.cdp)
    await session.connect(pw)
    page = await session.new_page()

    # Warm-up: establece cookies (y banner) en el perfil; si aparece un
    # challenge interactivo, resolver a mano mientras.
    try:
      await safe_goto(page, BASE_URL)
      await accept_cookies(page)
      print("[warm-up] sesion iniciada (cookies aceptadas)")
    except Exception as exc:
      print(f"[warm-up] omitido: {exc}")

    # ------------------- Fase 1: inventario de categorias -------------------
    if CATEGORIES_FILE.exists() and not args.refresh_categories:
      categories = [Category(**c) for c in json.loads(CATEGORIES_FILE.read_text())]
      print(f"[fase 1] {len(categories)} categorias cargadas de {CATEGORIES_FILE}")
    else:
      print(f"[fase 1] recolectando categorias desde {CATEGORIES_URL} ...")
      categories = await collect_categories(page)
      CATEGORIES_FILE.write_text(
        json.dumps([c.model_dump() for c in categories], indent=2, ensure_ascii=False),
        encoding="utf-8",
      )
      print(f"[fase 1] {len(categories)} categorias guardadas en {CATEGORIES_FILE}")

    done = set() if args.no_resume else load_completed_slugs()
    todo = [c for c in categories if c.slug not in done][: args.limit]
    if not todo:
      print("No hay categorias pendientes. Usa --no-resume para empezar de cero.")
      return 0
    print(f"[fase 2] {len(todo)} solicitudes por ejecutar "
        f"({len(done)} ya completadas de ejecuciones previas)\n")

    # ------------------- Fase 2: extraccion de las tarjetas -------------------
    consecutive_failures = 0
    for i, cat in enumerate(todo, start=1):
      print(f"[{i}/{len(todo)}] {cat.slug}")

      status, error, data, latency, attempts = "error", None, None, 0.0, 0
      for attempt in range(1, MAX_ATTEMPTS + 1):
        attempts = attempt
        t0 = time.perf_counter()
        try:
          data = await extract_first_product(page, cat)
          latency = (time.perf_counter() - t0) * 1000
          status, error = "success", None
          break
        except (NavigationError, DomChangedError) as exc:
          latency = (time.perf_counter() - t0) * 1000
          status, error = "error", str(exc)
          if attempt < MAX_ATTEMPTS:
            wait = BACKOFF_BASE_S * (2 ** (attempt - 1)) + random.uniform(0, 2)
            print(f"    intento {attempt} falló ({error}) -> reintento en {wait:.0f}s")
            await asyncio.sleep(wait)
        except Exception as exc:  # cualquier otro fallo no tumba el flujo
          latency = (time.perf_counter() - t0) * 1000
          status, error = "error", f"unexpected:{type(exc).__name__}"
          if attempt < MAX_ATTEMPTS:
            await asyncio.sleep(BACKOFF_BASE_S)

      if status == "success" and data:
        record = ProductRecord(
          category_id=cat.category_id,
          category_slug=cat.slug,
          category_url=cat.url,
          category_name=cat.name,
          attempt=attempts,
          latency_ms=round(latency, 1),
          status="success",
          **data,
        )
        append_jsonl(PRODUCTS_FILE, record.model_dump())
        print(f"    + {record.product_name} (rating={record.rating}, "
              f"reviews={record.reviews_count}, {record.latency_ms:.0f} ms)")
        if record.resolved_via_subcategory:
          print(f"      ↳ resuelto vía sub-categorías: "
                f"{' -> '.join(record.subcategory_hops)}")
        consecutive_failures = 0
      else:
        append_jsonl(FAILURES_FILE, {
          "category_slug": cat.slug, "category_url": cat.url,
          "error": error, "attempts": attempts, "ts": time.time(),
        })
        print(f"    x fallo definitivo: {error}")
        consecutive_failures += 1
        if consecutive_failures >= CONSECUTIVE_FAILURES_COOLDOWN:
          print(f"    ! {consecutive_failures} fallos consecutivos -> "
                f"cooldown de {COOLDOWN_S:.0f}s")
          await asyncio.sleep(COOLDOWN_S)
          consecutive_failures = 0

      metrics.record(RequestRecord(
        category_slug=cat.slug, status=status,
        latency_ms=round(latency, 1), attempts=attempts, error=error,
      ))

      # Ritmo "humano": pausa aleatoria entre solicitudes
      await asyncio.sleep(random.uniform(args.min_delay, args.max_delay))

    # ------------------- Reporte final (requisito #3) -------------------
    report = metrics.report()
    REPORT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                            encoding="utf-8")

    reqs, lat, stab = report["requests"], report["latency_ms"], report["stability"]
    print("\n" + "=" * 62)
    print("REPORTE DE RENDIMIENTO (tambien en data/report.json)")
    print("=" * 62)
    print(f"Total solicitudes : {reqs['total']}")
    print(f"Exitosas          : {reqs['successful']} ({reqs['success_rate_pct']}%)")
    print(f"Fallidas          : {reqs['failed']}")
    print(f"Reintentos        : {reqs['total_retries']}")
    print(f"Latencia          : media={lat['mean']} ms | p50={lat['p50']} ms | p95={lat['p95']} ms")
    print(f"Racha max. exitos : {stab['max_consecutive_successes']}")
    print(f"Recuperaciones    : {stab['recoveries_after_failure']}")
    print(f"Errores           : {report['exception_handling']['errors']}")
    print(f"Duracion total    : {report['duration_s']} s")
    print(f"\nDataset   -> {PRODUCTS_FILE}")
    print(f"Fallidos  -> {FAILURES_FILE}")
    print(f"Reporte   -> {REPORT_FILE}")

    await session.close()

  return 0 if reqs["failed"] == 0 else 1


def cli() -> None:
  raise SystemExit(asyncio.run(run(parse_args())))


if __name__ == "__main__":
  cli()