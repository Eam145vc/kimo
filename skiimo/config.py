"""Configuracion central. Lee .env y expone constantes tipadas."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _get(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Falta variable de entorno: {name}")
    return value or ""


# Siigo
SIIGO_USERNAME = _get("SIIGO_USERNAME", required=True)
SIIGO_ACCESS_KEY = _get("SIIGO_ACCESS_KEY", required=True)
SIIGO_BASE_URL = _get("SIIGO_BASE_URL", "https://api.siigo.com")
SIIGO_PARTNER_ID = _get("SIIGO_PARTNER_ID", "Skiimo")

# IDs descubiertos en la exploracion
DEFAULT_INVOICE_DOC_ID = int(_get("DEFAULT_INVOICE_DOC_ID", "13214"))  # FV tradicional
DEFAULT_PURCHASE_DOC_ID = int(_get("DEFAULT_PURCHASE_DOC_ID", "27394"))  # FC Gasto Admin
DEFAULT_SELLER_ID = int(_get("DEFAULT_SELLER_ID", "341"))  # Oscar (admin)
DEFAULT_IVA_TAX_ID = int(_get("DEFAULT_IVA_TAX_ID", "7108"))  # IVA 19%
DEFAULT_PAYMENT_ID = int(_get("DEFAULT_PAYMENT_ID", "3043"))  # Efectivo

# Doc-type alternativo: factura electronica
INVOICE_DOC_ID_ELECTRONIC = 27703
PURCHASE_DOC_ID_MATERIAS = 13219

# Modo prueba: si esta activo, las facturas se envian al cliente test y se etiquetan [TEST BOT].
# Default OFF: produccion real. Solo activar manualmente para pruebas de regresion locales.
SIIGO_INVOICE_TEST_MODE = _get("SIIGO_INVOICE_TEST_MODE", "false").lower() in ("1", "true", "yes")
SIIGO_TEST_CUSTOMER_ID = _get("SIIGO_TEST_CUSTOMER_ID", "")

# Telegram
TELEGRAM_BOT_TOKEN = _get("TELEGRAM_BOT_TOKEN", required=True)
ADMIN_TELEGRAM_CHAT_ID = _get("ADMIN_TELEGRAM_CHAT_ID", "")

# Gemini
GEMINI_API_KEY = _get("GEMINI_API_KEY", required=True)
GEMINI_MODEL = _get("GEMINI_MODEL", "gemini-2.5-flash")

# Storage: en Railway montaremos un Volume en /data, en local usamos ./data
_db_path_env = _get("DB_PATH", "")
if _db_path_env:
    DB_PATH = Path(_db_path_env)
else:
    DB_PATH = ROOT / "data" / "skiimo.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# IMAP (correo de facturas de proveedor)
IMAP_HOST = _get("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(_get("IMAP_PORT", "993"))
IMAP_USER = _get("IMAP_USER", "")
IMAP_APP_PASSWORD = _get("IMAP_APP_PASSWORD", "")
IMAP_FOLDER = _get("IMAP_FOLDER", "INBOX")
IMAP_PROCESSED_LABEL = _get("IMAP_PROCESSED_LABEL", "Kimo-Procesado")

IMAP_ENABLED = bool(IMAP_USER and IMAP_APP_PASSWORD)


# Mapping Siigo de impuestos
IVA_TAX_IDS_BY_PCT: dict[float, int] = {
    0.0: 13999,
    5.0: 7109,
    19.0: 7108,
}

# Forms de pago disponibles (id -> nombre)
PAYMENT_METHODS: dict[int, str] = {
    3043: "Efectivo",
    3044: "Credito",
    3045: "Tarjeta Debito",
    3046: "Tarjeta Credito",
    8102: "Nequi",
    8103: "Daviplata",
    8104: "Banco Ahorros",
    10766: "Clientes Nacionales",
    10767: "Clientes Extranjero",
}
