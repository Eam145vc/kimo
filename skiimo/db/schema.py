"""Esquema SQLite. Espejo Siigo + estado del bot.

Toda fecha como TEXT ISO-8601. Todo payload de Siigo como TEXT JSON.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from skiimo.config import DB_PATH

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Espejo: clientes y proveedores Siigo
CREATE TABLE IF NOT EXISTS siigo_customers (
    id              TEXT PRIMARY KEY,           -- uuid Siigo
    type            TEXT NOT NULL,              -- Customer | Supplier
    identification  TEXT NOT NULL,              -- NIT / CC
    name            TEXT NOT NULL,              -- nombre completo, normalizado
    commercial_name TEXT,
    person_type     TEXT,                       -- Person | Company
    active          INTEGER NOT NULL DEFAULT 1,
    email           TEXT,
    phone           TEXT,
    address         TEXT,
    raw             TEXT NOT NULL,              -- JSON completo de Siigo
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_customers_identification ON siigo_customers(identification);
CREATE INDEX IF NOT EXISTS idx_customers_name ON siigo_customers(name);
CREATE INDEX IF NOT EXISTS idx_customers_type_active ON siigo_customers(type, active);

-- Espejo: productos Siigo
CREATE TABLE IF NOT EXISTS siigo_products (
    id                  TEXT PRIMARY KEY,
    code                TEXT NOT NULL,
    name                TEXT NOT NULL,
    account_group_id    INTEGER,
    account_group_name  TEXT,
    type                TEXT,
    active              INTEGER NOT NULL DEFAULT 1,
    tax_classification  TEXT,
    tax_included        INTEGER NOT NULL DEFAULT 0,
    iva_tax_id          INTEGER,                -- id del impuesto IVA principal
    iva_percentage      REAL,
    unit_label          TEXT,
    price_default       REAL,                   -- primer precio de la primera price-list
    available_quantity  REAL,
    reference           TEXT,
    description         TEXT,
    raw                 TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_products_code ON siigo_products(code);
CREATE INDEX IF NOT EXISTS idx_products_name ON siigo_products(name);
CREATE INDEX IF NOT EXISTS idx_products_active ON siigo_products(active);

-- Espejo: facturas de venta
CREATE TABLE IF NOT EXISTS siigo_invoices (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,              -- FV-1-5171
    number          INTEGER,
    prefix          TEXT,
    document_id     INTEGER,
    date            TEXT NOT NULL,              -- YYYY-MM-DD
    customer_id     TEXT,
    customer_ident  TEXT,
    seller_id       INTEGER,
    total           REAL NOT NULL,
    balance         REAL,
    stamp_status    TEXT,                       -- Accepted, null, etc.
    public_url      TEXT,
    observations    TEXT,
    items_json      TEXT NOT NULL,              -- JSON array
    payments_json   TEXT,                       -- JSON array
    raw             TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invoices_date ON siigo_invoices(date);
CREATE INDEX IF NOT EXISTS idx_invoices_customer ON siigo_invoices(customer_id);
CREATE INDEX IF NOT EXISTS idx_invoices_seller ON siigo_invoices(seller_id);

-- Espejo: facturas de compra
CREATE TABLE IF NOT EXISTS siigo_purchases (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,              -- FC-2-75
    number          INTEGER,
    document_id     INTEGER,
    date            TEXT NOT NULL,
    supplier_id     TEXT,
    supplier_ident  TEXT,
    total           REAL NOT NULL,
    balance         REAL,
    provider_inv_prefix TEXT,
    provider_inv_number TEXT,
    observations    TEXT,
    items_json      TEXT NOT NULL,
    payments_json   TEXT,
    raw             TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_purchases_date ON siigo_purchases(date);
CREATE INDEX IF NOT EXISTS idx_purchases_supplier ON siigo_purchases(supplier_id);

-- Estado del sync por entidad
CREATE TABLE IF NOT EXISTS sync_state (
    entity         TEXT PRIMARY KEY,             -- customers | products | invoices | purchases
    last_sync_at   TEXT NOT NULL,                -- ISO timestamp
    last_cursor    TEXT,                          -- created_start o modified_start usado
    items_synced   INTEGER NOT NULL DEFAULT 0
);

-- Vendedores autorizados a usar el bot (mapeo chat Telegram -> seller Siigo)
CREATE TABLE IF NOT EXISTS bot_vendedores (
    telegram_chat_id   INTEGER PRIMARY KEY,
    nombre             TEXT NOT NULL,
    siigo_seller_id    INTEGER NOT NULL,
    rol                TEXT NOT NULL DEFAULT 'vendedor',  -- vendedor | admin
    activo             INTEGER NOT NULL DEFAULT 1,
    created_at         TEXT NOT NULL
);

-- Pedidos creados desde el bot (antes/durante/despues de Siigo)
CREATE TABLE IF NOT EXISTS bot_pedidos (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_chat_id    INTEGER NOT NULL,
    telegram_msg_id     INTEGER,
    estado              TEXT NOT NULL,                  -- borrador | confirmado | enviado | error | cancelado
    payload_extraido    TEXT NOT NULL,                  -- JSON del Pedido extraido por Gemini
    customer_id         TEXT,                            -- uuid Siigo si se resolvio
    siigo_invoice_id    TEXT,                            -- uuid del invoice creado
    siigo_invoice_name  TEXT,
    idempotency_key     TEXT UNIQUE,
    error               TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pedidos_estado ON bot_pedidos(estado);
CREATE INDEX IF NOT EXISTS idx_pedidos_chat ON bot_pedidos(telegram_chat_id);

-- Usuarios del panel web (admin)
CREATE TABLE IF NOT EXISTS panel_users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT UNIQUE NOT NULL,
    password_hash   TEXT NOT NULL,                   -- bcrypt
    role            TEXT NOT NULL DEFAULT 'admin',
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL,
    last_login_at   TEXT
);

-- Sesiones activas del panel
CREATE TABLE IF NOT EXISTS panel_sessions (
    token           TEXT PRIMARY KEY,                -- secrets.token_urlsafe(32)
    user_id         INTEGER NOT NULL REFERENCES panel_users(id),
    created_at      TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    ip              TEXT
);
CREATE INDEX IF NOT EXISTS idx_panel_sessions_user ON panel_sessions(user_id);

-- Facturas de proveedor procesadas (chat foto o email)
CREATE TABLE IF NOT EXISTS bot_facturas_proveedor (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    origen              TEXT NOT NULL,                   -- chat | email
    telegram_chat_id    INTEGER,
    archivo_local       TEXT,
    payload_extraido    TEXT NOT NULL,
    proveedor_nit       TEXT,
    proveedor_factura   TEXT,
    total               REAL,
    confidence          REAL,
    estado              TEXT NOT NULL,                   -- pendiente | aprobada | enviada | error
    siigo_purchase_id   TEXT,
    idempotency_key     TEXT UNIQUE,
    error               TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_factprov_estado ON bot_facturas_proveedor(estado);

-- Precios oficiales por producto y lista (calculados desde historial + override manual)
CREATE TABLE IF NOT EXISTS precios_oficiales (
    product_id      TEXT NOT NULL,
    product_code    TEXT NOT NULL,
    lista           TEXT NOT NULL,             -- DETAL | MAYORISTA | DISTRIBUIDOR
    precio_pre_iva  REAL NOT NULL,
    precio_con_iva  REAL,                       -- redondeado para mostrar
    fuente          TEXT NOT NULL,              -- catalogo_siigo | moda_historica | manual
    ventas_referencia INTEGER DEFAULT 0,        -- cuantas ventas con este precio respaldan
    confirmed_by    TEXT,                       -- quien aprobo
    confirmed_at    TEXT,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (product_id, lista)
);
CREATE INDEX IF NOT EXISTS idx_precios_code ON precios_oficiales(product_code, lista);

-- Categoria asignada al cliente
CREATE TABLE IF NOT EXISTS clientes_categoria (
    customer_id     TEXT PRIMARY KEY,
    categoria       TEXT NOT NULL,              -- DETAL | MAYORISTA | DISTRIBUIDOR
    fuente          TEXT NOT NULL,              -- default | sugerido_historia | manual
    confirmed_by    TEXT,
    notas           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- Terminos de pronto pago por cliente
-- Multiples filas por cliente (un escalon por fila)
CREATE TABLE IF NOT EXISTS clientes_pronto_pago (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id     TEXT NOT NULL,
    dias_max        INTEGER NOT NULL,           -- pago en <= dias_max dias
    descuento_pct   REAL NOT NULL,              -- 15.0 = 15%
    notas           TEXT,
    activo          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pp_customer ON clientes_pronto_pago(customer_id);

-- Correos procesados (IMAP) para detectar duplicados de mensajes ya leidos
CREATE TABLE IF NOT EXISTS correos_procesados (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      TEXT UNIQUE NOT NULL,             -- header Message-ID del correo
    remitente       TEXT,
    asunto          TEXT,
    fecha_correo    TEXT,                              -- fecha del correo
    adjuntos_count  INTEGER NOT NULL DEFAULT 0,
    facturas_creadas INTEGER NOT NULL DEFAULT 0,       -- cuantos PDFs terminaron en factura Siigo
    estado          TEXT NOT NULL,                    -- pendiente | parcial | completo | descartado | sin_facturas
    error           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_correos_message_id ON correos_procesados(message_id);
CREATE INDEX IF NOT EXISTS idx_correos_estado ON correos_procesados(estado);

-- Adjuntos individuales (PDFs de facturas) extraidos de los correos
CREATE TABLE IF NOT EXISTS facturas_correo (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    correo_id       INTEGER NOT NULL REFERENCES correos_procesados(id),
    adjunto_hash    TEXT UNIQUE NOT NULL,             -- sha256 del PDF
    nombre_archivo  TEXT,
    proveedor_nit   TEXT,
    proveedor_nombre TEXT,
    numero_factura  TEXT,
    total           REAL,
    payload_extraido TEXT,                            -- JSON FacturaProveedor
    confidence      REAL,
    siigo_purchase_id TEXT,
    siigo_purchase_name TEXT,
    estado          TEXT NOT NULL,                    -- pendiente | aprobada | enviada | error | descartada
    error           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_factcorreo_correo ON facturas_correo(correo_id);
CREATE INDEX IF NOT EXISTS idx_factcorreo_estado ON facturas_correo(estado);
CREATE INDEX IF NOT EXISTS idx_factcorreo_proveedor ON facturas_correo(proveedor_nit, numero_factura);

-- Comprobantes de pago procesados (OCR) para detectar duplicados
CREATE TABLE IF NOT EXISTS comprobantes_procesados (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    img_hash        TEXT UNIQUE NOT NULL,            -- sha256 del archivo
    monto           REAL NOT NULL,
    metodo          TEXT,
    fecha_pago      TEXT,
    numero_referencia TEXT,
    titular_origen  TEXT,
    factura_aplicada TEXT,                            -- nombre factura si se aplico
    rc_name         TEXT,                              -- recibo de caja generado
    nc_name         TEXT,                              -- nota credito generada (si aplica)
    estado          TEXT NOT NULL,                    -- aplicado | descartado | pendiente
    actor           TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comp_hash ON comprobantes_procesados(img_hash);
CREATE INDEX IF NOT EXISTS idx_comp_ref ON comprobantes_procesados(numero_referencia);

-- Excepciones de precio: cuando el bot deja pasar un precio fuera de tabla
CREATE TABLE IF NOT EXISTS excepciones_precio (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pedido_id       INTEGER,
    customer_id     TEXT,
    product_code    TEXT NOT NULL,
    precio_oficial  REAL,
    precio_aplicado REAL NOT NULL,
    delta_pct       REAL,
    razon           TEXT,
    actor           TEXT,
    created_at      TEXT NOT NULL
);

-- Audit log: todo cambio en Siigo o estado del bot
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entity       TEXT NOT NULL,
    entity_id    TEXT,
    action       TEXT NOT NULL,
    actor        TEXT,
    payload      TEXT,
    created_at   TEXT NOT NULL
);
"""


def get_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"DB inicializada en {DB_PATH}")
