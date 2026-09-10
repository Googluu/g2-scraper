"""Punto de entrada CLI.

Uso:
  uv run g2-scraper --limit 5      # smoke test
  uv run g2-scraper                # completa hasta 100 muestras
  uv run g2-scraper --no-resume    # empezar de cero (archiva el dataset previo)

`--limit` es el objetivo TOTAL de muestras del dataset: si ya hay 91 en
products.jsonl, una corrida con --limit 100 procesa solo las 9 que faltan.
"""
import argparse
import asyncio
import json
import random
import time
from pathlib import Path

from playwright.async_api import async_playwright

from .antibot import BlockedError, human_delay, human_dwell
from .browser import BrowserSession
from .config import (
  BACKOFF_BASE_S, BLOCK_COOLDOWN_S, CATEGORIES_FILE, CATEGORIES_URL, CDP_URL,
  COOLDOWN_S, CONSECUTIVE_FAILURES_COOLDOWN, DATA_DIR, FAILURES_FILE,
  MAX_ATTEMPTS, MAX_BLOCK_RECOVERIES, MAX_REQUESTS,
  PRODUCTS_FILE, REPORT_FILE, SESSION_BREAK_MAX_S, SESSION_BREAK_MIN_S,
  SESSION_REQUEST_BUDGET,
)
from .metrics import BLOCKED, ERROR, SUCCESS, MetricsCollector, RequestRecord, dataset_quality
from .models import Category, ProductRecord
from .scraper import (
  DomChangedError, NavigationError, absolute_url,
  collect_categories, extract_first_product, warm_up,
)


def append_jsonl(path: Path, payload: dict) -> None:
  with path.open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[dict]:
  if not path.exists():
    return []
  rows = []
  for line in path.read_text(encoding="utf-8").splitlines():
    if line.strip():
      try:
        rows.append(json.loads(line))
      except json.JSONDecodeError:
        continue
  return rows


def load_completed_slugs() -> set[str]:
  """Permite reanudar una ejecucion interrumpida sin repetir categorias."""
  return {r["category_slug"] for r in read_jsonl(PRODUCTS_FILE) if "category_slug" in r}


def archive_previous_run() -> None:
  """Aparta (no borra) los datos de corridas anteriores para empezar de cero."""
  stamp = time.strftime("%Y%m%d-%H%M%S")
  for path in (PRODUCTS_FILE, FAILURES_FILE, REPORT_FILE):
    if path.exists():
      backup = path.with_suffix(path.suffix + f".bak-{stamp}")
      path.rename(backup)
      print(f"[reset] {path.name} -> {backup.name}")


def parse_args() -> argparse.Namespace:
  ap = argparse.ArgumentParser(description="Scraper resiliente de G2.com")
  ap.add_argument("--limit", type=int, default=MAX_REQUESTS,
                  help=f"muestras TOTALES objetivo en el dataset (default: {MAX_REQUESTS})")
  ap.add_argument("--cdp", default=CDP_URL, help="URL CDP del navegador externo")
  ap.add_argument("--session-budget", type=int, default=SESSION_REQUEST_BUDGET,
                  help="solicitudes por token datadome antes de rotar la sesion")
  ap.add_argument("--refresh-categories", action="store_true",
                  help="recolectar categorias aunque exista el archivo")
  ap.add_argument("--no-resume", action="store_true",
                  help="ignorar progreso previo (archiva el dataset existente)")
  ap.add_argument("--no-warmup", action="store_true",
                  help="omitir el calentamiento de sesion (no recomendado)")
  return ap.parse_args()


def fmt_eta(seconds: float) -> str:
  m, s = divmod(int(max(0, seconds)), 60)
  return f"{m}m{s:02d}s" if m else f"{s}s"


class Runner:
  """Orquesta la fase 2 y es dueño del ciclo de vida de la sesion.

  La pagina es un atributo mutable porque cada reciclaje la reemplaza: soltar
  el token `datadome` implica abrir una pestaña nueva.
  """

  def __init__(self, session: BrowserSession, metrics: MetricsCollector,
               args: argparse.Namespace) -> None:
    self.session = session
    self.metrics = metrics
    self.args = args
    self.page = None
    self.requests_in_session = 0
    self.block_recoveries = 0
    self.exhausted = False   # se agoto el presupuesto de reciclajes

  async def start(self) -> None:
    self.page = await self.session.new_page()
    if not self.args.no_warmup:
      await self._warm_up(label="warm-up")

  async def _warm_up(self, *, label: str) -> bool:
    try:
      await warm_up(self.page)
      print(f"[{label}] sesion tibia (home -> /categories)")
      return True
    except BlockedError as exc:
      print(f"[{label}] bloqueado durante el calentamiento: {exc.signal}")
      return False
    except Exception as exc:
      print(f"[{label}] incompleto: {type(exc).__name__}: {exc}")
      return True

  # -------------------------------------------------------------------------
  # Ciclo de vida de la sesion
  # -------------------------------------------------------------------------

  async def dwell(self) -> None:
    """Interaccion humana en la pagina ya extraida.

    Va despues de la extraccion, no antes, para no contaminar `latency_ms`.
    Desde la perspectiva del sitio la secuencia es identica: la pagina carga,
    se producen eventos de mouse/scroll y luego llega la navegacion siguiente
    (la lectura del DOM por CDP es invisible para la pagina).
    """
    await human_dwell(self.page)

  async def _reset_session(self) -> None:
    """Suelta el token `datadome` y abre una pestaña limpia."""
    await self.session.clear_site_data(self.page)
    self.page = await self.session.recycle_page(self.page)
    self.requests_in_session = 0
    self.metrics.note_recycle()

  async def rotate_if_needed(self) -> None:
    """Rotacion PREVENTIVA al agotar el presupuesto de la micro-sesion.

    El bloqueo se ligo al token, y 91 solicitudes con uno solo cruzaron el
    umbral. Rotar antes de llegar ahi cuesta una pausa; llegar al bloqueo
    cuesta la corrida.
    """
    if self.requests_in_session < self.args.session_budget:
      return
    pause = random.uniform(SESSION_BREAK_MIN_S, SESSION_BREAK_MAX_S)
    print(f"  [sesion] presupuesto agotado ({self.requests_in_session} req) -> "
          f"rotando token y pausando {pause:.0f}s")
    await self._reset_session()
    await asyncio.sleep(pause)
    await self._warm_up(label="re-warm")

  async def recover_from_block(self, signal: str) -> bool:
    """Recupera tras un bloqueo. Devuelve False si se agoto el presupuesto.

    Reproduce lo que restauro el acceso manualmente: soltar cookies y storage
    de g2.com, enfriar con escalada, y re-calentar la sesion nueva. Nunca
    reintenta la navegacion en caliente.
    """
    if self.block_recoveries >= MAX_BLOCK_RECOVERIES:
      self.exhausted = True
      return False

    cooldown = BLOCK_COOLDOWN_S[min(self.block_recoveries, len(BLOCK_COOLDOWN_S) - 1)]
    self.block_recoveries += 1
    print(f"    ! BLOQUEO ({signal}) -> reciclaje "
          f"{self.block_recoveries}/{MAX_BLOCK_RECOVERIES}: soltando token "
          f"datadome + enfriando {cooldown:.0f}s")

    await self._reset_session()
    await asyncio.sleep(cooldown)

    if await self._warm_up(label="re-warm"):
      return True
    # La sesion nueva tambien nacio bloqueada: escala el enfriamiento.
    return await self.recover_from_block(f"{signal}:persistente")

  # -------------------------------------------------------------------------
  # Extraccion de una categoria
  # -------------------------------------------------------------------------

  async def scrape_one(self, cat: Category) -> tuple[str, str | None, dict | None, float, int]:
    """-> (status, error, data, latency_ms, attempts).

    Los bloqueos y los fallos de DOM/red reciben tratamiento OPUESTO: el
    bloqueo recicla la sesion (sin reintento en caliente), el fallo tecnico
    reintenta con backoff exponencial.
    """
    status, error, data, latency = ERROR, None, None, 0.0
    attempts = 0

    for attempt in range(1, MAX_ATTEMPTS + 1):
      attempts = attempt
      t0 = time.perf_counter()
      try:
        data = await extract_first_product(self.page, cat)
        latency = (time.perf_counter() - t0) * 1000
        return SUCCESS, None, data, latency, attempts

      except BlockedError as exc:
        latency = (time.perf_counter() - t0) * 1000
        status, error = BLOCKED, exc.signal
        self.metrics.note_block(exc.signal)
        if not await self.recover_from_block(exc.signal):
          print("    x presupuesto de reciclajes agotado: se detiene la corrida")
          return BLOCKED, error, None, latency, attempts
        # Sesion nueva y tibia: se reintenta ESTA categoria

      except (NavigationError, DomChangedError) as exc:
        latency = (time.perf_counter() - t0) * 1000
        status, error = ERROR, str(exc)
        if attempt < MAX_ATTEMPTS:
          wait = BACKOFF_BASE_S * (2 ** (attempt - 1)) + random.uniform(0, 2)
          print(f"    intento {attempt} falló ({error}) -> reintento en {wait:.0f}s")
          await asyncio.sleep(wait)

      except Exception as exc:  # cualquier otro fallo no tumba el flujo
        latency = (time.perf_counter() - t0) * 1000
        status, error = ERROR, f"unexpected:{type(exc).__name__}"
        if attempt < MAX_ATTEMPTS:
          await asyncio.sleep(BACKOFF_BASE_S)

    return status, error, data, latency, attempts


async def run(args: argparse.Namespace) -> int:
  DATA_DIR.mkdir(parents=True, exist_ok=True)
  if args.no_resume:
    archive_previous_run()

  metrics = MetricsCollector()

  async with async_playwright() as pw:
    session = BrowserSession(args.cdp)
    await session.connect(pw)

    runner = Runner(session, metrics, args)
    await runner.start()

    # ------------------- Fase 1: inventario de categorias -------------------
    if CATEGORIES_FILE.exists() and not args.refresh_categories:
      raw = json.loads(CATEGORIES_FILE.read_text(encoding="utf-8"))
      # Normaliza URLs de corridas viejas (tenian "//categories/...")
      categories = [Category(**{**c, "url": absolute_url(c["url"])}) for c in raw]
      print(f"[fase 1] {len(categories)} categorias cargadas de {CATEGORIES_FILE}")
    else:
      print(f"[fase 1] recolectando categorias desde {CATEGORIES_URL} ...")
      categories = await collect_categories(runner.page)
      CATEGORIES_FILE.write_text(
        json.dumps([c.model_dump() for c in categories], indent=2, ensure_ascii=False),
        encoding="utf-8",
      )
      print(f"[fase 1] {len(categories)} categorias guardadas en {CATEGORIES_FILE}")

    done = set() if args.no_resume else load_completed_slugs()
    remaining = max(0, args.limit - len(done))
    todo = [c for c in categories if c.slug not in done][:remaining]
    if not todo:
      print(f"Dataset ya tiene {len(done)} muestras (objetivo {args.limit}). "
            f"Usa --limit mayor o --no-resume para empezar de cero.")
      return 0
    print(f"[fase 2] {len(todo)} solicitudes por ejecutar "
          f"({len(done)} ya en el dataset, objetivo {args.limit})")
    print(f"[fase 2] presupuesto de sesion: {args.session_budget} req/token\n")

    # ------------------- Fase 2: extraccion de las tarjetas -------------------
    consecutive_failures = 0
    started = time.time()

    for i, cat in enumerate(todo, start=1):
      await runner.rotate_if_needed()
      print(f"[{i}/{len(todo)}] {cat.slug}")

      status, error, data, latency, attempts = await runner.scrape_one(cat)
      runner.requests_in_session += 1

      if status == SUCCESS and data:
        record = ProductRecord(
          category_id=cat.category_id,
          category_slug=cat.slug,
          category_url=cat.url,
          category_name=cat.name,
          attempt=attempts,
          latency_ms=round(latency, 1),
          status=SUCCESS,
          **data,
        )
        append_jsonl(PRODUCTS_FILE, record.model_dump())
        print(f"    + {record.product_name} (rating={record.rating}, "
              f"reviews={record.reviews_count}, {record.latency_ms:.0f} ms)")
        if record.resolved_via_subcategory:
          print(f"      ↳ resuelto vía sub-categorías: "
                f"{' -> '.join(record.subcategory_hops)}")
        consecutive_failures = 0
        await runner.dwell()   # scroll/mouse en la pagina, ya fuera del cronometro
      else:
        append_jsonl(FAILURES_FILE, {
          "category_slug": cat.slug, "category_url": cat.url,
          "error": error, "status": status, "attempts": attempts, "ts": time.time(),
        })
        print(f"    x fallo definitivo ({status}): {error}")
        consecutive_failures += 1
        if status == ERROR and consecutive_failures >= CONSECUTIVE_FAILURES_COOLDOWN:
          print(f"    ! {consecutive_failures} fallos consecutivos -> "
                f"cooldown de {COOLDOWN_S:.0f}s")
          await asyncio.sleep(COOLDOWN_S)
          consecutive_failures = 0

      metrics.record(RequestRecord(
        category_slug=cat.slug, status=status,
        latency_ms=round(latency, 1), attempts=attempts, error=error,
      ))

      if runner.exhausted:
        print("\n[abortado] anti-bot persistente tras "
              f"{MAX_BLOCK_RECOVERIES} reciclajes. El progreso quedo en "
              f"{PRODUCTS_FILE}; relanza mas tarde para reanudar.")
        break

      if i < len(todo):
        # Ritmo "humano": log-normal con pausas de lectura ocasionales
        delay = human_delay()
        eta = (time.time() - started) / i * (len(todo) - i)
        print(f"    . pausa {delay:.0f}s | ETA {fmt_eta(eta)}")
        await asyncio.sleep(delay)

    # ------------------- Reporte final (requisito #3) -------------------
    report = metrics.report()
    report["data_quality"] = dataset_quality(read_jsonl(PRODUCTS_FILE))
    REPORT_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                           encoding="utf-8")

    reqs, lat = report["requests"], report["latency_ms"]
    stab, anti, qual = report["stability"], report["anti_bot"], report["data_quality"]
    drift = stab["latency_drift"]
    print("\n" + "=" * 62)
    print("REPORTE DE RENDIMIENTO (tambien en data/report.json)")
    print("=" * 62)
    print(f"Total solicitudes : {reqs['total']}")
    print(f"Exitosas          : {reqs['successful']} ({reqs['success_rate_pct']}%)")
    print(f"Errores de acceso : {reqs['blocked']} ({reqs['access_error_rate_pct']}%)")
    print(f"Errores de DOM    : {reqs['dom_errors']}")
    print(f"Reintentos        : {reqs['total_retries']}")
    print(f"Throughput        : {reqs['throughput_req_per_min']} req/min")
    print(f"Latencia          : media={lat['mean']} ms | p50={lat['p50']} ms | p95={lat['p95']} ms")
    print(f"Racha max. exitos : {stab['max_consecutive_successes']}")
    print(f"Recuperaciones    : {stab['recoveries_after_failure']}")
    print(f"Deriva latencia   : {drift['first_mean_ms']} -> {drift['last_mean_ms']} ms "
          f"({drift['delta_pct']:+}%)")
    print(f"Bloqueos / ciclos : {anti['blocks_detected']} / {anti['session_recycles']}")
    print(f"Errores           : {report['exception_handling']['errors']}")
    print(f"Dataset           : {qual['samples']} muestras, "
          f"{qual['fully_populated_pct']}% con todos los campos")
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
