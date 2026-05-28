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

-- ==========================================================================
-- ASISTENCIA (Hikvision DS-K1T321MFWX + calculo horas Colombia)
-- ==========================================================================

-- Plantillas de horario: para reutilizar configuracion entre empleados similares
CREATE TABLE IF NOT EXISTS plantillas_turno (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre                  TEXT NOT NULL,          -- "Turno produccion", "Administrativo"
    descripcion             TEXT,
    hora_entrada            TEXT NOT NULL,          -- "07:00"
    hora_salida             TEXT NOT NULL,          -- "16:00"
    almuerzo_inicio         TEXT,                   -- "12:00" o NULL (sin descuento auto)
    almuerzo_fin            TEXT,                   -- "13:00"
    almuerzo_minutos_auto   INTEGER DEFAULT 60,     -- si no marcan almuerzo y jornada >6h
    dias_semana             TEXT NOT NULL,          -- "1,2,3,4,5" (lun=1 .. dom=7)
    tolerancia_entrada_min  INTEGER NOT NULL DEFAULT 10,
    activa                  INTEGER NOT NULL DEFAULT 1,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

-- Empleados que marcan en el terminal facial
CREATE TABLE IF NOT EXISTS empleados (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    hik_employee_no     TEXT UNIQUE,                -- ID asignado en el equipo Hikvision
    cedula              TEXT,
    nombre              TEXT NOT NULL,
    cargo               TEXT,
    telegram_chat_id    TEXT,                       -- opcional: para avisarle al empleado
    salario_mensual     REAL,                       -- COP. Obligatorio al crear (no hay default)
    valor_hora_ord      REAL,                       -- COP/hora ordinaria (calculado o sobrescrito)
    fecha_ingreso       TEXT,                       -- YYYY-MM-DD
    plantilla_id        INTEGER REFERENCES plantillas_turno(id),  -- horario asignado
    activo              INTEGER NOT NULL DEFAULT 1,
    foto_path           TEXT,                       -- ruta de la foto facial subida al equipo
    observaciones       TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_emp_hik ON empleados(hik_employee_no);
CREATE INDEX IF NOT EXISTS idx_emp_activo ON empleados(activo);
CREATE INDEX IF NOT EXISTS idx_emp_cedula ON empleados(cedula);

-- Turnos: override puntual de plantilla para un empleado (si rota, vacaciones, etc).
-- Para la mayoria de los casos, el empleado tiene una plantilla_id y no necesita filas aca.
CREATE TABLE IF NOT EXISTS turnos (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    empleado_id             INTEGER NOT NULL REFERENCES empleados(id) ON DELETE CASCADE,
    plantilla_id            INTEGER REFERENCES plantillas_turno(id),
    nombre                  TEXT NOT NULL,
    hora_entrada            TEXT NOT NULL,
    hora_salida             TEXT NOT NULL,
    almuerzo_inicio         TEXT,
    almuerzo_fin            TEXT,
    almuerzo_minutos_auto   INTEGER DEFAULT 60,
    dias_semana             TEXT NOT NULL,
    tolerancia_entrada_min  INTEGER NOT NULL DEFAULT 10,
    fecha_desde             TEXT NOT NULL,
    fecha_hasta             TEXT,
    activo                  INTEGER NOT NULL DEFAULT 1,
    created_at              TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turnos_empleado ON turnos(empleado_id, activo);

-- Marcajes: del Hikvision (hik_event_id NOT NULL) o manuales (hik_event_id NULL).
-- Los del equipo NO se borran salvo error grave; los manuales si.
-- editado=1 cuando un admin corrige el ts/tipo (queda copia en raw_event).
CREATE TABLE IF NOT EXISTS marcajes (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    hik_event_id        TEXT UNIQUE,                -- NULL = marcaje manual
    empleado_id         INTEGER REFERENCES empleados(id),
    hik_employee_no     TEXT,
    ts                  TEXT NOT NULL,
    fecha               TEXT NOT NULL,
    tipo                TEXT,                       -- entrada | salida | almuerzo_in | almuerzo_out | desconocido
    metodo              TEXT,                       -- face | fingerprint | card | pin | manual
    major               INTEGER,
    minor               INTEGER,
    nombre_hik          TEXT,
    foto_url            TEXT,
    raw_event           TEXT,                       -- JSON crudo del evento original
    origen              TEXT NOT NULL DEFAULT 'hikvision',  -- hikvision | manual | corregido
    editado             INTEGER NOT NULL DEFAULT 0, -- 1 si admin lo corrigio
    editado_por         TEXT,
    editado_at          TEXT,
    nota_admin          TEXT,                       -- "Olvido marcar salida", "Permiso autorizado", etc.
    ignorar_nomina      INTEGER NOT NULL DEFAULT 0, -- 1 = no contar para horas calculadas
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_marc_empleado_fecha ON marcajes(empleado_id, fecha);
CREATE INDEX IF NOT EXISTS idx_marc_ts ON marcajes(ts);
CREATE INDEX IF NOT EXISTS idx_marc_event_id ON marcajes(hik_event_id);

-- Excepciones / novedades de nomina: permisos, ausencias justificadas, vacaciones,
-- incapacidades, horas extra autorizadas anticipadamente, ajustes manuales, etc.
-- No estan vinculadas a marcajes especificos sino a un dia o rango.
CREATE TABLE IF NOT EXISTS excepciones_asistencia (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    empleado_id     INTEGER NOT NULL REFERENCES empleados(id) ON DELETE CASCADE,
    fecha_desde     TEXT NOT NULL,                  -- YYYY-MM-DD
    fecha_hasta     TEXT NOT NULL,                  -- YYYY-MM-DD (mismo dia si es 1 solo)
    tipo            TEXT NOT NULL,                  -- permiso | vacaciones | incapacidad | ausencia_justificada | ausencia_injustificada | hora_extra_aprobada | ajuste_horas | tardanza_perdonada
    horas_ajuste    REAL DEFAULT 0,                 -- horas a sumar/restar (puede ser negativo)
    paga            INTEGER NOT NULL DEFAULT 1,     -- 1 = se paga, 0 = descuenta de nomina
    motivo          TEXT,                           -- texto libre
    aprobado_por    TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_excep_empleado_fecha ON excepciones_asistencia(empleado_id, fecha_desde, fecha_hasta);
CREATE INDEX IF NOT EXISTS idx_excep_tipo ON excepciones_asistencia(tipo);

-- Horas calculadas por dia (resumen diario derivado de marcajes)
CREATE TABLE IF NOT EXISTS horas_calculadas (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    empleado_id             INTEGER NOT NULL REFERENCES empleados(id) ON DELETE CASCADE,
    fecha                   TEXT NOT NULL,          -- YYYY-MM-DD
    primera_entrada         TEXT,                   -- HH:MM:SS
    ultima_salida           TEXT,
    horas_ordinarias        REAL NOT NULL DEFAULT 0,
    horas_extra_diurna      REAL NOT NULL DEFAULT 0, -- +25%
    horas_extra_nocturna    REAL NOT NULL DEFAULT 0, -- +75%
    horas_nocturnas_ord     REAL NOT NULL DEFAULT 0, -- +35% recargo nocturno
    horas_dom_fest_ord      REAL NOT NULL DEFAULT 0, -- +75%
    horas_dom_fest_extra_d  REAL NOT NULL DEFAULT 0, -- +100%
    horas_dom_fest_extra_n  REAL NOT NULL DEFAULT 0, -- +150%
    minutos_tarde           INTEGER NOT NULL DEFAULT 0,
    minutos_almuerzo        INTEGER NOT NULL DEFAULT 0,
    estado                  TEXT NOT NULL DEFAULT 'calculado', -- calculado | aprobado | pagado
    aprobada_por            TEXT,                   -- "auto" o user_id del dueno
    aprobada_at             TEXT,
    nota                    TEXT,
    updated_at              TEXT NOT NULL,
    UNIQUE(empleado_id, fecha)
);
CREATE INDEX IF NOT EXISTS idx_horas_fecha ON horas_calculadas(fecha);
CREATE INDEX IF NOT EXISTS idx_horas_estado ON horas_calculadas(estado);

-- Festivos colombianos (precarga + edicion manual)
CREATE TABLE IF NOT EXISTS festivos_colombia (
    fecha           TEXT PRIMARY KEY,               -- YYYY-MM-DD
    nombre          TEXT NOT NULL,
    fuente          TEXT NOT NULL DEFAULT 'precargado'  -- precargado | manual
);

-- Estado del sync: para saber desde cuando jalar la proxima vez
CREATE TABLE IF NOT EXISTS asistencia_sync (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton
    last_event_ts       TEXT,                       -- ISO-8601 del ultimo evento procesado
    last_sync_at        TEXT,                       -- ultima ejecucion del cron
    last_sync_status    TEXT,                       -- ok | error
    last_sync_error     TEXT,
    eventos_procesados  INTEGER NOT NULL DEFAULT 0
);

-- Config global de asistencia (defaults editables desde panel)
CREATE TABLE IF NOT EXISTS asistencia_config (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    descripcion     TEXT,
    updated_at      TEXT NOT NULL
);

-- Ventanas de horas extra autorizadas por el admin (un dia o rango).
-- Las extras SOLO se pagan si caen dentro de una ventana autorizada,
-- topadas a hora_fin (anti-robo de minutos).
CREATE TABLE IF NOT EXISTS extras_autorizadas (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha_desde     TEXT NOT NULL,              -- YYYY-MM-DD
    fecha_hasta     TEXT NOT NULL,              -- YYYY-MM-DD (igual a desde si es 1 dia)
    hora_inicio     TEXT NOT NULL DEFAULT '17:30',
    hora_fin        TEXT NOT NULL,              -- HH:MM
    nota            TEXT,
    creado_por      TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_extras_fechas ON extras_autorizadas(fecha_desde, fecha_hasta);
"""


def get_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Migraciones idempotentes: agregar columnas que se sumaron despues de la creacion inicial.
# Solo necesario porque SQLite no soporta IF NOT EXISTS en ALTER TABLE ADD COLUMN.
MIGRATIONS = [
    # Empleados: columna plantilla_id
    ("empleados", "plantilla_id", "INTEGER REFERENCES plantillas_turno(id)"),
    # Marcajes: columnas nuevas para correccion manual
    ("marcajes", "origen", "TEXT NOT NULL DEFAULT 'hikvision'"),
    ("marcajes", "editado", "INTEGER NOT NULL DEFAULT 0"),
    ("marcajes", "editado_por", "TEXT"),
    ("marcajes", "editado_at", "TEXT"),
    ("marcajes", "nota_admin", "TEXT"),
    ("marcajes", "ignorar_nomina", "INTEGER NOT NULL DEFAULT 0"),
    # Turnos: link a plantilla
    ("turnos", "plantilla_id", "INTEGER REFERENCES plantillas_turno(id)"),
    # Excepciones: archivo adjunto (incapacidad escaneada, etc)
    ("excepciones_asistencia", "adjunto_path", "TEXT"),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, column, ddl in MIGRATIONS:
        # Chequear si la columna ya existe
        cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
        existing = {c["name"] for c in cols}
        if column not in existing:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            except sqlite3.OperationalError as e:
                # Si la tabla todavia no existe (DB nueva), el CREATE TABLE de SCHEMA la creara
                # con la columna ya incluida. Ignoramos.
                if "no such table" not in str(e).lower():
                    raise

    # Caso especial: hik_event_id tenia NOT NULL, lo aflojamos.
    # SQLite no soporta DROP NOT NULL directo; rehacemos la tabla si detectamos el constraint.
    try:
        info = conn.execute("PRAGMA table_info(marcajes)").fetchall()
        for c in info:
            if c["name"] == "hik_event_id" and c["notnull"] == 1:
                # Renombrar tabla vieja
                conn.execute("ALTER TABLE marcajes RENAME TO marcajes_old")
                # Crear nueva con hik_event_id UNIQUE pero permitiendo NULL
                conn.execute("""
                    CREATE TABLE marcajes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        hik_event_id TEXT UNIQUE,
                        empleado_id INTEGER REFERENCES empleados(id),
                        hik_employee_no TEXT,
                        ts TEXT NOT NULL,
                        fecha TEXT NOT NULL,
                        tipo TEXT,
                        metodo TEXT,
                        major INTEGER,
                        minor INTEGER,
                        nombre_hik TEXT,
                        foto_url TEXT,
                        raw_event TEXT,
                        origen TEXT NOT NULL DEFAULT 'hikvision',
                        editado INTEGER NOT NULL DEFAULT 0,
                        editado_por TEXT,
                        editado_at TEXT,
                        nota_admin TEXT,
                        ignorar_nomina INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL
                    )
                """)
                conn.execute("""
                    INSERT INTO marcajes
                    (id, hik_event_id, empleado_id, hik_employee_no, ts, fecha, tipo, metodo,
                     major, minor, nombre_hik, foto_url, raw_event, origen, editado,
                     editado_por, editado_at, nota_admin, ignorar_nomina, created_at)
                    SELECT id, hik_event_id, empleado_id, hik_employee_no, ts, fecha, tipo, metodo,
                           major, minor, nombre_hik, foto_url, raw_event,
                           COALESCE(origen, 'hikvision'),
                           COALESCE(editado, 0),
                           editado_por, editado_at, nota_admin,
                           COALESCE(ignorar_nomina, 0),
                           created_at
                    FROM marcajes_old
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_marc_empleado_fecha ON marcajes(empleado_id, fecha)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_marc_ts ON marcajes(ts)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_marc_event_id ON marcajes(hik_event_id)")
                conn.execute("DROP TABLE marcajes_old")
                break
    except sqlite3.OperationalError as e:
        # Si la tabla no existia, la migracion no aplica
        if "no such table" not in str(e).lower():
            raise


def init_db(db_path: Path | str | None = None) -> None:
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA)
        _apply_migrations(conn)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"DB inicializada en {DB_PATH}")
