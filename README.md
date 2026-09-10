# g2-scraper

Motor de extracción para G2.com. Recorre el inventario de categorías y captura
la primera *product-card* de cada una, sosteniendo 100 solicitudes sin ser
interrumpido por el sistema anti-bot del sitio (**DataDome**).

Prueba técnica — ver [docs/ARQUITECTURA.md](docs/ARQUITECTURA.md) para el
razonamiento detrás de cada decisión técnica.

## Requisitos

- Python 3.12+ y [uv](https://docs.astral.sh/uv/)
- Google Chrome instalado

## Instalación

```bash
uv sync
```

## Uso

El scraper se conecta por CDP a un navegador **externo**, así que primero se
lanza Chrome (una sola vez; queda corriendo entre ejecuciones):

```bash
./scripts/launch_chrome.sh          # Linux / macOS
scripts\launch_chrome.bat           # Windows
```

Luego, en otra terminal:

```bash
uv run g2-scraper --limit 5         # smoke test
uv run g2-scraper                   # completa hasta 100 muestras (~70 min)
uv run g2-scraper --no-resume       # empezar de cero (archiva el dataset previo)
```

`--limit` es el objetivo **total** de muestras del dataset: si ya hay 91 en
`data/products.jsonl`, una corrida con `--limit 100` procesa solo las 9 que
faltan. El progreso se escribe incrementalmente, así que una interrupción no
pierde trabajo hecho.

### Opciones

| Flag | Descripción |
|---|---|
| `--limit N` | muestras totales objetivo (default: 100) |
| `--cdp URL` | URL del navegador externo (default: `http://localhost:9222`) |
| `--session-budget N` | solicitudes por token `datadome` antes de rotar la sesión (default: 30) |
| `--refresh-categories` | volver a recolectar el inventario de categorías |
| `--no-resume` | ignorar progreso previo (archiva el dataset existente) |
| `--no-warmup` | omitir el calentamiento de sesión (no recomendado) |

## Salidas

| Archivo | Contenido |
|---|---|
| `data/categories.json` | inventario de categorías (2244) |
| `data/products.jsonl` | dataset normalizado, una muestra por línea |
| `data/failures.jsonl` | categorías que no se pudieron resolver, con su causa |
| `data/report.json` | reporte de rendimiento: éxito, estabilidad, excepciones, latencia y calidad de datos |

## Pruebas

```bash
uv run pytest
```

Corren sin red y de forma determinista. Incluyen la validación del detector de
bloqueos contra los artefactos reales capturados durante un bloqueo de DataDome
(`tests/fixtures/datadome_*.html`).

## Estructura

```
src/g2_scraper/
  main.py       CLI + orquestación (Runner: ciclo de vida de la sesión)
  scraper.py    navegación resiliente y extracción (fases 1 y 2)
  antibot.py    detección de bloqueos DataDome + ritmo y dwell humanos
  browser.py    sesión CDP contra el navegador externo + reciclaje
  metrics.py    reporte de rendimiento y auditoría de calidad del dataset
  models.py     modelos Pydantic del dataset
  config.py     parámetros centrales
```
