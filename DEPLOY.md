# Deploy en Railway

## 1. Crear servicio

1. Entrá a [railway.app](https://railway.app) y logueate (GitHub).
2. **New Project** → **Deploy from GitHub repo** → seleccioná `Eam145vc/kimo`.
3. Railway detecta el `nixpacks.toml` y arma el build automáticamente.

## 2. Configurar Volume para SQLite

Importante: por defecto Railway no persiste archivos entre deploys. Necesitamos un Volume.

1. En el servicio creado: **Settings** → **Volumes** → **+ New Volume**.
2. Mount path: `/data`
3. Size: 1 GB es más que suficiente al inicio.

## 3. Configurar variables de entorno

En el servicio: **Variables** → **Raw Editor** → pegá esto y completá los valores **copiándolos desde tu `.env` local**:

```
# Siigo — copiar desde .env local
SIIGO_USERNAME=<tu_correo_siigo>
SIIGO_ACCESS_KEY=<tu_access_key>
SIIGO_BASE_URL=https://api.siigo.com
SIIGO_PARTNER_ID=Skiimo

# Telegram — copiar desde .env local
TELEGRAM_BOT_TOKEN=<token_de_BotFather>

# Gemini — copiar desde .env local
GEMINI_API_KEY=<api_key_de_aistudio>

# Admin / Modo prueba (produccion: false)
ADMIN_TELEGRAM_CHAT_ID=<tu_chat_id>
SIIGO_INVOICE_TEST_MODE=false
SIIGO_TEST_CUSTOMER_ID=<uuid_cliente_test>

# Defaults Siigo (estos son fijos para esta cuenta)
DEFAULT_INVOICE_DOC_ID=13214
DEFAULT_PURCHASE_DOC_ID=27394
DEFAULT_SELLER_ID=341
DEFAULT_IVA_TAX_ID=7108
DEFAULT_PAYMENT_ID=3043

# DB en Volume persistente
DB_PATH=/data/skiimo.db

# IMAP (completar cuando se generen las credenciales)
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
IMAP_USER=
IMAP_APP_PASSWORD=
IMAP_FOLDER=INBOX
IMAP_PROCESSED_LABEL=Kimo-Procesado
```

> ⚠️ **NUNCA pegues secrets reales en este archivo** — Railway los acepta directamente desde el panel.
> Si quieres copiar fácil: en local hacé `type .env` (Windows) o `cat .env` (Linux/Mac) y copiá las líneas relevantes al Raw Editor de Railway.

## 4. Deploy

Railway detecta los archivos `nixpacks.toml` y `Procfile` automáticamente y empieza el build. Toma 2-3 minutos.

**Primer arranque**: el bot detecta que la DB está vacía y hace un sync inicial automático con Siigo (1162 clientes, ~330 productos, facturas y compras). Esto tarda ~30 segundos.

## 5. Verificar

En el dashboard de Railway → **Logs** deberías ver:

```
INFO skiimo.bot | Stats matcher: {'customers': 1161, 'products': 329}
INFO skiimo.bot | Job resumen diario programado: 8:00 hora Colombia (13:00 UTC)
INFO skiimo.bot | Bot arrancado en modo polling. Ctrl+C para detener.
```

Si lo ves: **el bot está corriendo en la nube**. Mandale algo por Telegram y debería responder.

## 6. Mantenimiento

| Cambio | Cómo aplicarlo |
|---|---|
| Cambiar precio / categoría | Por chat, sin redeploy |
| Cambiar código | `git push` a main → Railway redeploy automático |
| Cambiar variable env | Editar en Railway → service restart automático |
| Ver logs en tiempo real | Railway dashboard → Logs (siempre on) |
| Backup DB | Railway → Volume → snapshot manual o via CLI |

## Importante: cosas que NO subimos al repo

- `.env` con tus credenciales (gitignored).
- `data/*.db` (SQLite local).
- `explorations/*` (datos de exploración inicial).
- CSVs generados con info de clientes.

Si necesitás esos archivos en producción, configurás las variables en Railway o subís manualmente vía CLI:

```
railway link
railway run python -m skiimo.pricing.calibrador --apply
```

## Costo estimado

- Servicio (Hobby plan): $5/mes (incluye 500h compute + RAM)
- Volume 1 GB: $0.25/mes
- **Total Railway: ~$5.25/mes**
- + Gemini: $5-15/mes para volumen típico

## Troubleshooting

**El bot no responde**: revisar logs. Si dice "TimedOut" al arrancar, **otra instancia del bot está corriendo en local** (cerrá la local con `Ctrl+C`).

**Sync inicial fallido**: el bot arranca igual, podés forzarlo con `python -m skiimo.sync.siigo_sync --full` vía Railway CLI.

**Volume no persiste**: verificar que `DB_PATH=/data/skiimo.db` y que el mount path del volume es exactamente `/data`.
