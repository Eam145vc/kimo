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

# IDs de la cuenta Siigo de ESSKIMO COCKTAILS SAS (migrada 2026-07-08;
# la cuenta anterior era de Oscar persona natural y tenia OTROS ids)
DEFAULT_INVOICE_DOC_ID = int(_get("DEFAULT_INVOICE_DOC_ID", "7988"))  # FV tradicional
DEFAULT_PURCHASE_DOC_ID = int(_get("DEFAULT_PURCHASE_DOC_ID", "7993"))  # FC Compra (unico doc de compras)
DEFAULT_SELLER_ID = int(_get("DEFAULT_SELLER_ID", "206"))  # ESSKIMO COCKTAIL SAS
DEFAULT_IVA_TAX_ID = int(_get("DEFAULT_IVA_TAX_ID", "4294"))  # IVA 19%
DEFAULT_PAYMENT_ID = int(_get("DEFAULT_PAYMENT_ID", "1837"))  # Efectivo

# Doc-type alternativo: factura electronica
INVOICE_DOC_ID_ELECTRONIC = 42546
# La cuenta nueva solo tiene UN doc de compras (7993); antes habia uno aparte
# para materias primas. Ambas categorias van al mismo doc.
PURCHASE_DOC_ID_MATERIAS = 7993

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


# Hikvision DS-K1T321MFWX (terminal facial de asistencia)
HIK_HOST = _get("HIK_HOST", "")                       # ej: 192.168.128.32
HIK_PORT = int(_get("HIK_PORT", "80"))
HIK_USER = _get("HIK_USER", "admin")
HIK_PASSWORD = _get("HIK_PASSWORD", "")
HIK_TIMEOUT_SECONDS = int(_get("HIK_TIMEOUT_SECONDS", "10"))
HIK_ENABLED = bool(HIK_HOST and HIK_PASSWORD)


# Mapping Siigo de impuestos
IVA_TAX_IDS_BY_PCT: dict[float, int] = {
    0.0: 13865,
    5.0: 4295,
    19.0: 4294,
}

# Forms de pago disponibles (id -> nombre)
# OJO: la cuenta nueva NO tiene Nequi/Daviplata/Banco Ahorros creados en Siigo.
# Cuando se creen, agregarlos aqui y en PAYMENT_METHODS_CONTADO (siigo_writer/siigo_payments).
PAYMENT_METHODS: dict[int, str] = {
    1837: "Efectivo",
    1838: "Credito",
    1839: "Tarjeta Debito",
    1840: "Tarjeta Credito",
    8880: "Clientes Nacionales",
    8881: "Clientes Extranjero",
}
