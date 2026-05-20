# Skiimo — Agente de pedidos para Siigo (Fase 0/1 local)

Bot Telegram que extrae pedidos en lenguaje natural y los crea en Siigo automáticamente. Funciona con texto, audio y fotos. Hoy corre en local; en fase 2 sube a Railway.

## Estado actual

- Conexión Siigo verificada (creación de facturas, anulación vía nota crédito, descarga de PDF).
- Sync local Siigo → SQLite (1162 clientes, 450 productos, 1248 facturas, 483 compras).
- Extracción de pedidos con Gemini 2.5 Flash (texto y audio).
- Matching difuso de productos y clientes (fuzzy).
- Bot Telegram funcional con botones de confirmación, edición y elección de candidatos.
- Modo prueba activado: las facturas usan tipo tradicional (no DIAN) y cliente test.

## Cómo correr todo

### 1. Setup (una sola vez)

```
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m skiimo.db.schema
```

### 2. Variables de entorno

Editar `.env`:
- `SIIGO_USERNAME`, `SIIGO_ACCESS_KEY` (ya cargadas)
- `TELEGRAM_BOT_TOKEN` (ya cargado, bot @soykiimo_bot)
- `GEMINI_API_KEY` (ya cargada)
- `ADMIN_TELEGRAM_CHAT_ID` — opcional, te identifica como admin
- `SIIGO_INVOICE_TEST_MODE=true` — fuerza factura tradicional + cliente test

### 3. Sincronizar catálogos Siigo → SQLite

```
.venv/Scripts/python.exe -m skiimo.sync.siigo_sync --full
```

Esto baja todo desde Siigo. En siguientes corridas, sin `--full`, solo trae deltas.

Para sync incremental cada cierto tiempo (recomendado: cron cada 15 min):

```
.venv/Scripts/python.exe -m skiimo.sync.siigo_sync
```

### 4. Registrar tu usuario como vendedor/admin

Primero hay que saber tu `chat_id` de Telegram. Dos opciones:

**Opción A**: arrancá el helper y mandale `/start` al bot:
```
.venv/Scripts/python.exe get_my_chat_id.py
```
Va a imprimir tu chat_id en consola. Apretás Ctrl+C cuando lo veas.

**Opción B**: arrancá el bot normal y mandale `/start`. El bot te contesta con tu chat_id.

Después te registrás:
```
.venv/Scripts/python.exe register_user.py 123456789 "Oscar" --rol admin
```

### 5. Arrancar el bot

```
.venv/Scripts/python.exe -m skiimo.bot.app
```

El bot se conecta a Telegram en modo polling. Ctrl+C para detener.

## Probar el bot

Una vez registrado tu chat_id, mandale al bot `@soykiimo_bot`:

- **Texto**: `"10 P23 a $100 cada uno"` → te muestra resumen con botones.
- **Texto descriptivo**: `"5 bolsas chicle 6L para Tienda La 35"` → el matcher fuzzy busca candidatos. Si hay 4 bolsas de chicle distintas, te las ofrece para que elijas.
- **Audio**: grabás un audio diciendo el pedido. Gemini transcribe + extrae en una llamada.
- **`/reporte ventas`**: te dice ventas del mes.
- **`/reporte gastos`**: te dice gastos del mes.
- **`/cancelar`**: cancela el pedido en curso.

## Modo prueba

Mientras `SIIGO_INVOICE_TEST_MODE=true`:
- Todas las facturas usan tipo **tradicional (13214)**, no electrónica DIAN.
- Todas las facturas se asignan al **cliente ZZZ TEST BOT PRUEBAS** (id `406be39e-...`).
- Las observaciones llevan prefijo `[TEST BOT]`.
- El consecutivo DIAN sigue avanzando (es la única limitación de Siigo).

Para producción cambiar `SIIGO_INVOICE_TEST_MODE=false`.

## Arquitectura local

```
skiimo/
  config.py             — env + IDs Siigo
  db/schema.py          — SQLite schema
  sync/siigo_sync.py    — sync Siigo -> SQLite
  matcher.py            — fuzzy search clientes y productos
  llm/
    schemas.py          — modelos Pydantic
    gemini.py           — wrapper Gemini structured output
  pipeline.py           — mensaje -> Pedido -> ResolvedPedido
  siigo_writer.py       — crear factura, anular, PDF, audit log
  bot/app.py            — Telegram bot (polling)

data/skiimo.db          — base local (no commit)
explorations/           — JSON crudos de la API Siigo (referencia)
```

## Scripts auxiliares en raíz

- `siigo_client.py` — cliente HTTP base
- `explore_siigo.py` — exploración inicial de catálogos
- `test_capabilities.py` — suite de capacidades (lectura/escritura)
- `test_full_pipeline.py` — test end-to-end SIN Telegram
- `get_my_chat_id.py` — descubrir tu chat_id
- `register_user.py` — dar de alta vendedor

## Próximos pasos sugeridos

1. **Vos**: registrarte como admin, mandar pedidos de prueba al bot.
2. Probar con audio.
3. Probar `/reporte ventas` y `/reporte gastos`.
4. Cuando confirmes que funciona, dar de alta a Frank y Manuela.
5. Después: agregar OCR de facturas de proveedor (foto/PDF) y sync por email.
6. Después: deploy a Railway con Postgres + cron de sync.
