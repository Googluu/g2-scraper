# Documento de Arquitectura

Motor de extracción para G2.com: recorre el inventario de categorías y captura
la primera *product-card* de cada una, sosteniendo 100 solicitudes sin ser
interrumpido por el sistema anti-bot del sitio.

---

## 1. El problema real: DataDome, y el bloqueo va en la cookie

La primera versión funcional se bloqueó en la solicitud ~91. El diagnóstico
inicial fue equivocado en dos puntos, y corregirlos definió toda la
arquitectura.

**No es Cloudflare, es DataDome.** Los artefactos capturados
(`tests/fixtures/datadome_*.html`) lo muestran sin ambigüedad:

- respuesta `403` con el bootstrap `var dd = {...}` y `ct.captcha-delivery.com`
- interstitial en `<iframe src="geo.captcha-delivery.com/interstitial/...">`
- página de bloqueo con `<body data-dd-response-page="hard-block">`

El detector original solo conocía marcadores de Cloudflare
(`#challenge-running`, `iframe[src*='challenges.cloudflare.com']`, título
*"Just a moment"*), así que el 403 de DataDome **pasaba como página cargada
correctamente**. La consecuencia fue costosa: el bloqueo terminaba reportado
como `product_card_not_found`, lo que activaba la política de reintentos —
6 categorías × 3 intentos = 18 golpes adicionales contra un muro, cada uno
profundizando el flag.

**El bloqueo se liga al token `datadome`, no a la IP.** Se comprobó
experimentalmente:

| IP                    | token `datadome` | resultado  |
|-----------------------|------------------|------------|
| misma (residencial)   | otro navegador   | ✅ carga   |
| **otra** (ProtonVPN)  | el mismo, quemado| ❌ 403     |
| misma (residencial)   | nuevo (storage limpiado) | ✅ carga |

Si fuera un bloqueo de IP, la primera fila fallaría y la segunda funcionaría;
pasó exactamente lo contrario. DataDome emite una cookie que identifica al
dispositivo/sesión, le acumula un score y, al cruzar el umbral, marca **ese
token** como bloqueado. La IP alimenta el score pero no es la palanca de
enforcement.

De ahí se siguen dos conclusiones que gobiernan el diseño:

1. **Rotar IP no resuelve nada** si se arrastra el token quemado. Y una VPN
   comercial empeora el score de partida: sus rangos son datacenter y están
   etiquetados como VPN conocida. La IP residencial es el mejor activo
   disponible; el problema era *cómo* se estaba usando.
2. **Soltar el token es la vía de recuperación**, y es automatizable. Es lo
   único que restauró el acceso en las pruebas manuales.

---

## 2. Decisiones de arquitectura

### 2.1 Entorno desacoplado por CDP (lineamiento #2)

`scripts/launch_chrome.sh` lanza un Chrome **real** con
`--remote-debugging-port=9222` y perfil persistente en
`~/.g2-scraper/chrome-profile`. El scraper se conecta por CDP
(`connect_over_cdp`) y usa el contexto por defecto.

Por qué importa más allá de cumplir el requisito:

- **La sesión sobrevive al script.** Si el proceso muere, el token `datadome`
  tibio y las cookies siguen ahí. Reanudar no cuesta una sesión nueva en frío.
- **La superficie de fingerprint es auténtica**, no emulada: es Chrome de
  verdad, con su `Accept-Language` real, sus fuentes, su WebGL y su
  `navigator` sin parchar.
- **Al cerrar solo se desconecta** (`browser.close()` sobre una conexión CDP no
  mata el navegador externo).

Existe un fallback a `launch_persistent_context` si el puerto CDP no responde,
para que el proyecto sea ejecutable sin lanzar Chrome a mano.

### 2.2 Se eliminó el script de *stealth*

La versión anterior inyectaba parches de fingerprint. **Con un Chrome real por
CDP, esos parches empeoran la detección.** Se eliminaron por esto:

| Parche | Problema |
|---|---|
| `navigator.webdriver → undefined` | Chrome lanzado solo con `--remote-debugging-port` ya reporta `false`. `undefined` no lo devuelve **ningún** navegador real: el parche creaba la anomalía que pretendía tapar. |
| `Object.defineProperty(navigator, ...)` | Deja la propiedad como *own property* de la instancia cuando nativamente vive en `Navigator.prototype`. Un `getOwnPropertyDescriptor` lo delata, igual que el `toString()` del getter (código de flecha en vez de `[native code]`). |
| `navigator.plugins` | Quedaba como array plano de 2 objetos con solo `name`, en lugar de un `PluginArray` con `item()`/`namedItem()`. |
| `navigator.languages = ['en-US','en']` | Contradecía el header `Accept-Language` real del navegador. El mismatch header ↔ JS es uno de los checks más baratos y fiables que existen. |

El principio: **con un navegador real no hay que mentir sobre el entorno, hay
que corregir el comportamiento.** Eso es lo que hace `antibot.py`.

### 2.3 Taxonomía de errores con tratamiento opuesto

Es el cambio de mayor impacto. Antes existía una sola clase de fallo y una sola
política (reintentar). Ahora:

| Excepción | Significado | Política |
|---|---|---|
| `BlockedError` | anti-bot | **nunca se reintenta en caliente**: recicla el token, enfría con escalada 120/300/600 s y re-calienta la sesión |
| `NavigationError` | timeout / red | reintento con backoff exponencial (8/16 s) |
| `DomChangedError` | el DOM cambió | reintento + cascada de selectores |

`safe_goto()` lee el **status HTTP** de la respuesta principal (`page.goto()`
devuelve un `Response` que la versión anterior descartaba) y consulta
`detect_block()`, que resuelve en orden de especificidad: DOM → título →
status. El DOM va primero porque distingue un *device check* auto-resoluble de
un veredicto firme, y **ambos se sirven con 403**.

Solo las señales auto-resolubles (`datadome_js_challenge`,
`datadome_interstitial`, `cloudflare_challenge`) se esperan: el check corre en
el cliente y, si pasa, la página se recarga con una cookie válida. Ante un
`datadome_hard_block` esperar solo desperdicia tiempo.

Además, cuando no aparecen product-cards se **re-verifica bloqueo antes de
culpar al DOM**. Confundir un bloqueo con `product_card_not_found` fue el
origen de los reintentos que agravaron el flag.

### 2.4 Reciclaje de sesión: la recuperación real

`BrowserSession.clear_site_data()` automatiza lo que restauró el acceso a mano:

1. Navega a `about:blank` **primero**. Con el JS de G2 aún vivo, sus scripts de
   analítica vuelven a sembrar cookies mientras se limpia (se verificó: sin
   este paso quedaban 6 cookies re-creadas; con él, 0).
2. CDP `Storage.clearDataForOrigin` — localStorage, sessionStorage, IndexedDB,
   cache y service workers de los orígenes de G2.
3. `clear_cookies()` con patrón `(^|\.)g2\.com$` — el token puede estar
   sembrado en `.g2.com` o en `www.g2.com` según la respuesta que lo emitió.
4. Pestaña nueva: renderer limpio, sin estado en memoria.

Cada reciclaje verifica y reporta el resultado (`token liberado (quedan N
cookies de g2.com)`), y advierte explícitamente si `datadome` sobrevivió.

Se acota a los orígenes de G2 deliberadamente, para no destruir el resto del
perfil.

Un token recién emitido **no tiene crédito**, y entrar directo a
`/categories/<slug>` con una cookie recién nacida puntúa peor que llegar ahí
por un camino plausible. Por eso todo reciclaje termina en `warm_up()`:
`home → /categories`, con dwell real en cada parada. Esto es lo que hace que
una sesión reciclada aguante en lugar de volver a caer.

### 2.5 Rotación preventiva

Reaccionar al bloqueo no basta: 91 solicitudes con un mismo token cruzaron el
umbral. `SESSION_REQUEST_BUDGET = 30` rota el token **antes** de llegar ahí,
con una pausa de 75–165 s entre micro-sesiones.

> Rotar antes cuesta una pausa. Llegar al bloqueo cuesta la corrida.

### 2.6 Comportamiento: ritmo y cadena de navegación

Tres señales conductuales se corrigieron:

**Ritmo log-normal en vez de uniforme.** El `uniform(2.5, 6.0)` original tiene
una firma plana: nadie navega a cadencia fija. Se sustituyó por una
distribución log-normal (mediana 13 s, σ 0.55) más una pausa de lectura de
30–105 s con probabilidad 0.18. El resultado tiene la cola larga del
comportamiento real: mediana ~15 s, media ~27 s. Sumando extracción, dwell y
las pausas de rotación, el throughput medido baja de **3.6 req/min** (la tasa
que se bloqueó) a **1.06 req/min**.

**Cadena de `Referer` coherente.** Antes eran 100 cargas top-level sin referer:
un grafo de navegación imposible para una persona. Ahora cada `goto` declara su
origen (`/categories` para una categoría; la categoría padre para un salto a
sub-categoría).

**Eventos de interacción.** El cliente de DataDome (`c.js`) recolecta mouse,
scroll y teclado. Cien vistas de página con cero eventos es, por sí misma, una
anomalía. `human_dwell()` genera movimientos de mouse y scroll con pausas
variables.

El dwell se ejecuta **después** de la extracción, no antes, para que los
segundos de scroll simulado no contaminen el `latency_ms` que se reporta como
tiempo de respuesta. Desde la perspectiva del sitio la secuencia es idéntica
—la lectura del DOM por CDP es invisible para la página.

### 2.7 Normalización de URLs

`BASE_URL` terminaba en `/` y los `href` empiezan con `/`, así que toda
navegación iba a `https://www.g2.com//categories/...`. Ese doble slash quedó
registrado en el `referer` que recibió DataDome, y ningún navegador conducido
por una persona lo genera: es firma de máquina. `absolute_url()` resuelve con
`urljoin` y colapsa barras repetidas. El test que lo cubría ya existía y
estaba en rojo.

### 2.8 Durabilidad del flujo

- **Checkpoint incremental**: cada muestra se escribe a `products.jsonl` en el
  momento. Una interrupción nunca pierde trabajo hecho.
- **Reanudable**: `--limit` es el objetivo **total** del dataset; al arrancar se
  leen los `category_slug` ya capturados y solo se procesa lo que falta.
- **Cascada de selectores** (4 niveles) ante cambios de DOM.
- **Fallback de sub-categorías**: algunas categorías no listan productos sino
  enlaces `link--chevron`; se navega a la primera sub-categoría válida y se
  reintenta ahí, con límite de profundidad y detección de ciclos. Cada muestra
  registra `resolved_via_subcategory` y `subcategory_hops` para trazabilidad.
- **Aborto limpio**: tras `MAX_BLOCK_RECOVERIES` reciclajes sin éxito se
  detiene la corrida y se emite el reporte, en lugar de seguir golpeando. El
  progreso queda en disco para reanudar más tarde.

---

## 3. Métricas (lineamiento #3)

`data/report.json` se estructura sobre las cuatro secciones obligatorias:

- **Tasa de éxito** — separa *capturas limpias* de **errores de acceso**
  (`blocked`) y errores de DOM (`dom_errors`). La distinción importa: son
  fallos de naturaleza distinta y con estrategia distinta.
- **Estabilidad** — rachas máximas, recuperaciones tras fallo y
  `latency_drift`, que compara la latencia media de las primeras extracciones
  contra las últimas. Responde con números a *"¿la iteración 99 se comporta
  como la 1?"*.
- **Manejo de excepciones** — la estrategia aplicada más el desglose de errores
  por clase, y `anti_bot` con las señales de bloqueo detectadas y los reciclajes
  de sesión ejecutados.
- **Latencia** — media, p50, p95, mín y máx por extracción exitosa, medida
  sobre navegación + espera de la card, sin el dwell simulado.

Se añade **`data_quality`**, que audita el dataset en lugar de declararlo
limpio: cobertura por campo, porcentaje de muestras con todos los campos
poblados y conteo de productos únicos.

---

## 4. Resultados medidos

Corrida completa de 100 solicitudes (`data/report.json`):

| Métrica | Valor |
|---|---|
| Solicitudes | 100 |
| Exitosas | **100 (100 %)** |
| Errores de acceso (bloqueos) | **0** |
| Errores de DOM | 0 |
| Reintentos consumidos | 0 |
| Racha máxima de éxitos | 100 |
| Reciclajes de sesión | 3 (todos preventivos) |
| Latencia media / p50 / p95 | 10 935 / 7 725 / 44 247 ms |
| Deriva de latencia (inicio → fin) | 16 001 → 13 616 ms (**−14.9 %**) |
| Throughput | 1.06 req/min |
| Duración total | 5 646 s (94 min) |
| Dataset | 100 muestras, **100 % con todos los campos** |

**Consistencia.** La racha máxima de éxitos es 100: no hubo una sola
interrupción. La deriva de latencia es *negativa* (−14.9 %): la iteración 100
respondió más rápido que la 1, así que no hay degradación acumulada. Las tres
rotaciones de token cayeron en las solicitudes 30, 61 y 91 — la última justo
donde la versión anterior se bloqueaba.

**Sobre el p95 de 44 s.** La cola de latencia es enteramente atribuible a las
8 muestras resueltas vía sub-categoría, y es una ineficiencia del scraper, no
del sitio:

| | n | media | p50 | máx |
|---|---|---|---|---|
| extracción directa | 92 | 7 857 ms | 7 492 ms | 17 635 ms |
| vía sub-categoría | 8 | 46 329 ms | 45 442 ms | 51 640 ms |

Las 8 solicitudes por encima de 30 s son exactamente las 8 con salto de
sub-categoría. La causa es `_wait_first_card()`: en una página que **no** lista
productos, la cascada agota sus cuatro selectores en secuencia
(15 s + 5 s + 5 s + 5 s = 30 s) antes de concluir que hay que buscar
sub-categorías. Es tiempo de espera puro, no de red.

Es optimizable: en lugar de agotar la cascada y *después* buscar
sub-categorías, se puede esperar a que aparezca **lo primero** entre una
product-card y un enlace `link--chevron` (un único `wait_for_selector` con
ambos patrones), y ramificar según lo que resuelva. Eso recortaría ~30 s en
esas 8 muestras y llevaría la media global de ~10.9 s a ~8.2 s. No se aplicó en
esta corrida para no alterar los datos ya validados.

**Sobre los 73 productos únicos en 100 muestras.** No es un defecto de
extracción: el mismo producto legítimamente encabeza varias categorías
relacionadas (*Salesforce Agentforce* es la primera card en 5 categorías de IA;
*Accenture* en 4 de consultoría). Los 100 `category_slug` son únicos, que es la
invariante que importa: una muestra por categoría.

---

## 5. Pruebas

`pytest` corre sin red y de forma determinista:

- `test_antibot.py` valida el detector contra los **artefactos reales**
  capturados durante el bloqueo (hard-block, interstitial y 403). El caso más
  importante es el negativo: una página de categoría normal no debe dar falso
  positivo, porque eso dispararía reciclajes innecesarios.
- `test_urls.py` cubre la normalización de URLs.
- `test_extraction.py` valida la extracción contra un fixture del DOM real.
- `test_categories.py` y `test_metrics.py` cubren el parseo y el reporte.

---

## 6. Límites conocidos

- **Una sola IP.** La arquitectura reduce el score conductual, pero no cambia el
  origen. Para volumen muy superior al de esta prueba el siguiente paso serían
  proxies residenciales rotativos, coordinados con la rotación de token: cada
  sesión nueva desde una IP nueva. No se usó aquí porque para 100 solicitudes el
  ritmo y la rotación de token bastan, y una VPN de datacenter habría empeorado
  el score.
- **`MAX_SUBCATEGORY_HOPS = 3`.** Una categoría cuyo primer producto esté a más
  de 3 saltos se registra como fallo en lugar de seguir profundizando.
- **Los selectores son observados, no contractuales.** La cascada de 4 niveles
  amortigua cambios de DOM, pero un rediseño mayor de G2 requeriría revisarlos.
