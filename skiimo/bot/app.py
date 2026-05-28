"""Bot Telegram en polling.

Comandos:
  /start             — bienvenida y muestra chat_id
  /yo                — quien soy (info del registro)
  /reporte ventas    — ventas del mes (resumen)
  /reporte gastos    — gastos del mes (resumen)
  /cancelar          — cancela el pedido en curso

Mensajes:
  texto      — Gemini extrae pedido -> matcher -> resumen -> botones
  voz/audio  — Gemini transcribe + extrae en una sola llamada
  foto       — (futuro) factura de proveedor
  documento  — (futuro) PDF de factura

Botones inline (callback_data):
  conf:<pedido_id>           confirmar pedido y enviar a Siigo
  edit:<pedido_id>           entrar a modo edicion (placeholder)
  canc:<pedido_id>           cancelar
  pickc:<pedido_id>:<cust_id>   elegir un candidato de cliente
  picki:<pedido_id>:<idx>:<prod_id>   elegir candidato de producto
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from datetime import datetime

from telegram import (
    BotCommand,
    BotCommandScopeDefault,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from skiimo.config import ADMIN_TELEGRAM_CHAT_ID, TELEGRAM_BOT_TOKEN
from skiimo.db.schema import get_conn, init_db
from skiimo.llm.agent import process_message, reset_history
from skiimo.matcher import Matcher
from skiimo.pipeline import ResolvedPedido, format_summary, resolve_pedido
from skiimo.siigo_writer import crear_factura_venta, get_invoice_pdf

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("skiimo.bot")
# Reducir ruido
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)


async def _reply_md_safe(update: Update, texto: str, **kwargs):
    """Responde intentando Markdown; si Telegram no puede parsear las entidades
    (texto del LLM/OCR con `*` o `_` sin balancear) reintenta en texto plano.

    Evita que el bot quede mudo con 'BadRequest: can't parse entities'.
    """
    try:
        return await update.message.reply_text(texto, parse_mode="Markdown", **kwargs)
    except BadRequest as e:
        if "parse entities" in str(e).lower() or "end of the entity" in str(e).lower():
            log.warning("Markdown invalido, reenvio en texto plano: %s", e)
            return await update.message.reply_text(texto, **kwargs)
        raise


async def _edit_md_safe(cb, texto: str, **kwargs):
    """Igual que _reply_md_safe pero para editar un mensaje (callback query)."""
    try:
        return await cb.edit_message_text(texto, parse_mode="Markdown", **kwargs)
    except BadRequest as e:
        msg = str(e).lower()
        if "parse entities" in msg or "end of the entity" in msg:
            log.warning("Markdown invalido (edit), reenvio en texto plano: %s", e)
            return await cb.edit_message_text(texto, **kwargs)
        raise

# Matcher singleton recargable (inicializacion lazy para que bootstrap pueda crear la DB primero)
_matcher: Matcher | None = None


def _get_matcher() -> Matcher:
    global _matcher
    if _matcher is None:
        _matcher = Matcher()
    return _matcher


# =============================================================================
# AUTORIZACION
# =============================================================================

def _is_authorized(chat_id: int) -> tuple[bool, dict | None]:
    """Devuelve (autorizado, info) para pedidos/ventas y rol del agente.

    El rol sale del CARGO del empleado vinculado (fuente de verdad):
      - Administrativo -> admin (todo: ventas, extras, marcajes, supervision)
      - Venta          -> vendedor (pedidos/ventas)
      - Operario/Coordinador -> NO autorizados aca (solo /mi_* de asistencia)
    Respaldo: admin-env (.env) y bot_vendedores siguen funcionando.
    """
    from skiimo.config import DEFAULT_SELLER_ID
    # admin via env (respaldo)
    if ADMIN_TELEGRAM_CHAT_ID and str(chat_id) == str(ADMIN_TELEGRAM_CHAT_ID):
        return True, {"telegram_chat_id": chat_id, "nombre": "Admin", "rol": "admin", "siigo_seller_id": DEFAULT_SELLER_ID}
    conn = get_conn()
    try:
        # 1) Empleado vinculado: el cargo define el rol
        emp = conn.execute(
            "SELECT nombre, cargo FROM empleados WHERE telegram_chat_id = ? AND activo = 1",
            (str(chat_id),),
        ).fetchone()
        if emp:
            cargo = (emp["cargo"] or "").strip().lower()
            if cargo == "administrativo":
                return True, {"telegram_chat_id": chat_id, "nombre": emp["nombre"],
                              "rol": "admin", "siigo_seller_id": DEFAULT_SELLER_ID}
            if cargo == "venta":
                return True, {"telegram_chat_id": chat_id, "nombre": emp["nombre"],
                              "rol": "vendedor", "siigo_seller_id": DEFAULT_SELLER_ID}
            # operario / coordinador -> no autorizados para ventas (solo asistencia)
        # 2) bot_vendedores (respaldo / usuarios legacy)
        row = conn.execute(
            "SELECT * FROM bot_vendedores WHERE telegram_chat_id = ? AND activo = 1",
            (chat_id,),
        ).fetchone()
        return (True, dict(row)) if row else (False, None)
    finally:
        conn.close()


def _empleado_vinculado(chat_id: int) -> dict | None:
    """Empleado de asistencia vinculado a este chat (rol trabajador), o None."""
    from skiimo.bot.asistencia_commands import empleado_por_chat
    return empleado_por_chat(chat_id)


# =============================================================================
# PERSISTENCIA DE PEDIDO
# =============================================================================

def _save_pedido(chat_id: int, msg_id: int, rp: ResolvedPedido) -> int:
    _d = dataclasses.asdict
    payload = {
        "raw": rp.raw.model_dump(),
        "cliente": _d(rp.cliente_elegido) if rp.cliente_elegido else None,
        "cliente_candidatos": [_d(c) for c in rp.cliente_candidatos],
        "items": [
            {
                "raw": i.raw.model_dump(),
                "elegido": _d(i.elegido) if i.elegido else None,
                "candidatos": [_d(c) for c in i.candidatos],
                "cantidad": i.cantidad,
                "precio_unitario": i.precio_unitario,
            }
            for i in rp.items
        ],
        "necesita_input_humano": rp.necesita_input_humano,
        "test_mode": rp.test_mode,
    }
    conn = get_conn()
    try:
        # Si ya existe un pedido con misma idempotency_key:
        # - estado 'enviado' -> idempotente real, devolver el id existente
        # - estado 'borrador' -> actualizar payload y devolver el mismo id (no duplicar)
        # - estado 'cancelado'/'error' -> liberar la key para permitir un nuevo pedido
        existente = conn.execute(
            "SELECT id, estado FROM bot_pedidos WHERE idempotency_key = ?",
            (rp.idempotency_key,),
        ).fetchone()
        if existente:
            if existente["estado"] == "enviado":
                return existente["id"]
            if existente["estado"] == "borrador":
                conn.execute(
                    "UPDATE bot_pedidos SET payload_extraido = ?, customer_id = ?, "
                    "telegram_msg_id = ?, updated_at = ? WHERE id = ?",
                    (
                        json.dumps(payload, ensure_ascii=False, default=str),
                        rp.cliente_elegido.id if rp.cliente_elegido else None,
                        msg_id,
                        datetime.now().isoformat(timespec="seconds"),
                        existente["id"],
                    ),
                )
                conn.commit()
                return existente["id"]
            # Cancelado o error: liberar la key (set NULL) para permitir nueva fila
            conn.execute(
                "UPDATE bot_pedidos SET idempotency_key = NULL WHERE id = ?",
                (existente["id"],),
            )
            conn.commit()

        cur = conn.execute(
            """INSERT INTO bot_pedidos (
                telegram_chat_id, telegram_msg_id, estado, payload_extraido,
                customer_id, idempotency_key, created_at, updated_at
            ) VALUES (?, ?, 'borrador', ?, ?, ?, ?, ?)""",
            (
                chat_id, msg_id,
                json.dumps(payload, ensure_ascii=False, default=str),
                rp.cliente_elegido.id if rp.cliente_elegido else None,
                rp.idempotency_key,
                datetime.now().isoformat(timespec="seconds"),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _load_pedido(pedido_id: int) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM bot_pedidos WHERE id = ?", (pedido_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _update_pedido_estado(pedido_id: int, estado: str, **extras) -> None:
    fields = ["estado = ?", "updated_at = ?"]
    values: list = [estado, datetime.now().isoformat(timespec="seconds")]
    for k, v in extras.items():
        fields.append(f"{k} = ?")
        values.append(v)
    values.append(pedido_id)
    conn = get_conn()
    try:
        conn.execute(f"UPDATE bot_pedidos SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# HANDLERS DE COMANDOS
# =============================================================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message and update.effective_chat
    chat_id = update.effective_chat.id
    ok, info = _is_authorized(chat_id)
    if ok:
        await update.message.reply_text(
            f"Hola {info.get('nombre', 'vendedor')}! Soy Kimo, tu asistente.\n\n"
            f"Puedes:\n"
            f"  - Mandarme un pedido (texto o audio):\n"
            f"    'Para Tienda La 35: 10 bolsas chicle'\n"
            f"  - Preguntarme cualquier cosa:\n"
            f"    'cuanto vendi este mes?'\n"
            f"    'cual fue mi ultima venta?'\n"
            f"    'top 5 productos'\n"
            f"    'quien me debe?'\n\n"
            f"Comandos:\n"
            f"  /nuevo — limpiar conversacion\n"
            f"  /cancelar — cancelar pedido en curso\n"
            f"  /yo — ver mi info"
        )
        await _aplicar_menu_por_rol(ctx.bot, chat_id)
    else:
        # ¿Es un TRABAJADOR vinculado? -> menu de asistencia segun su cargo.
        emp = _empleado_vinculado(chat_id)
        if emp:
            from skiimo.bot.asistencia_commands import cmd_mi_help
            await cmd_mi_help(update, ctx)
            await _aplicar_menu_por_rol(ctx.bot, chat_id)
        else:
            await update.message.reply_text(
                "Hola. Para identificarte, envíame tu número de cédula."
            )
            await _aplicar_menu_por_rol(ctx.bot, chat_id)


async def cmd_yo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message and update.effective_chat
    chat_id = update.effective_chat.id
    ok, info = _is_authorized(chat_id)
    if ok:
        await update.message.reply_text(
            f"chat_id: {chat_id}\n"
            f"nombre: {info.get('nombre')}\n"
            f"rol: {info.get('rol', 'vendedor')}\n"
            f"siigo_seller_id: {info.get('siigo_seller_id')}"
        )
    else:
        await update.message.reply_text(f"No autorizado. chat_id: {chat_id}")


async def cmd_nuevo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message and update.effective_chat
    reset_history(update.effective_chat.id)
    await update.message.reply_text("Listo, empezamos de cero.")


async def cmd_agregar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/agregar <chat_id> <nombre...> [admin|vendedor]
    Atajo para registrar usuarios. Solo admins.
    """
    assert update.message and update.effective_chat
    chat_id = update.effective_chat.id
    ok, info = _is_authorized(chat_id)
    if not ok or (info and info.get("rol") != "admin"):
        await update.message.reply_text("Solo el admin puede registrar usuarios.")
        return

    args = ctx.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "Uso:\n"
            "  /agregar <chat_id> <nombre...> [admin|vendedor]\n\n"
            "Ejemplos:\n"
            "  /agregar 8162731878 Maria admin\n"
            "  /agregar 999111222 Frank Tabares vendedor\n"
            "  /agregar 8162731878 Maria  (default: vendedor)",
            parse_mode="Markdown",
        )
        return

    try:
        new_chat_id = int(args[0])
    except ValueError:
        await update.message.reply_text(f"chat_id invalido: {args[0]} (debe ser numero)")
        return

    # Detectar rol al final
    rol = "vendedor"
    nombre_parts = args[1:]
    if nombre_parts and nombre_parts[-1].lower() in ("admin", "vendedor"):
        rol = nombre_parts[-1].lower()
        nombre_parts = nombre_parts[:-1]
    if not nombre_parts:
        await update.message.reply_text("Falta el nombre.")
        return
    nombre = " ".join(nombre_parts)

    from skiimo.llm.tools import agregar_usuario
    r = await asyncio.to_thread(agregar_usuario, new_chat_id, nombre, rol)
    if r.get("error"):
        await update.message.reply_text(f"Error: {r['error']}")
        return
    accion = r.get("accion", "creado")
    emoji = "✅" if accion == "creado" else "🔄"
    await update.message.reply_text(
        f"{emoji} Usuario {accion}:\n\n"
        f"chat_id: `{r['chat_id']}`\n"
        f"nombre: *{r['nombre']}*\n"
        f"rol: *{r['rol']}*\n"
        f"siigo_seller_id: `{r['siigo_seller_id']}`",
        parse_mode="Markdown",
    )


async def cmd_resumen(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Genera y manda el resumen diario al chat actual."""
    assert update.message and update.effective_chat
    chat_id = update.effective_chat.id
    ok, _info = _is_authorized(chat_id)
    if not ok:
        await update.message.reply_text("No autorizado")
        return
    from skiimo.daily_summary import construir_resumen_diario
    texto = await asyncio.to_thread(construir_resumen_diario)
    await update.message.reply_text(texto, parse_mode="Markdown")


async def _job_sync_periodico(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sincroniza datos modificados de Siigo cada N minutos.

    - Invoices/Purchases ultimos 2 dias (cobertura corta)
    - Customers y Products incrementales (usa modified_start via state)
    Mantiene el espejo local al dia con cambios hechos desde Siigo web.
    """
    try:
        from skiimo.llm.tools import _sync_invoices_recientes, _sync_purchases_recientes
        ni = await asyncio.to_thread(_sync_invoices_recientes, 2)
        nf = await asyncio.to_thread(_sync_purchases_recientes, 2)
        if ni > 0 or nf > 0:
            log.info("Sync periodico: %d invoices, %d purchases", ni, nf)
    except Exception:
        log.exception("Error en sync periodico (invoices/purchases)")

    # Sync incremental de customers + products (usa modified_start desde last_cursor)
    try:
        nc, np = await asyncio.to_thread(_sync_incremental_catalogos)
        if nc > 0 or np > 0:
            log.info("Sync periodico catalogos: %d customers, %d products", nc, np)
    except Exception:
        log.exception("Error en sync periodico catalogos")


def _sync_incremental_catalogos() -> tuple[int, int]:
    """Incrementa siigo_customers y siigo_products desde Siigo usando modified_start.
    Devuelve (n_customers, n_products). No falla si Siigo no responde.
    """
    try:
        from siigo_client import SiigoClient
        from skiimo.sync.siigo_sync import sync_customers, sync_products
        conn = get_conn()
        try:
            with SiigoClient() as siigo:
                nc = sync_customers(siigo, conn, full=False)
                np = sync_products(siigo, conn, full=False)
            return nc, np
        finally:
            conn.close()
    except Exception:
        log.exception("Error sync_incremental_catalogos")
        return 0, 0


async def _job_resumen_diario(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job que corre cada mañana y manda el resumen a ADMIN_TELEGRAM_CHAT_ID."""
    from skiimo.config import ADMIN_TELEGRAM_CHAT_ID
    if not ADMIN_TELEGRAM_CHAT_ID:
        log.warning("Resumen diario: no hay ADMIN_TELEGRAM_CHAT_ID configurado")
        return
    try:
        from skiimo.daily_summary import construir_resumen_diario
        texto = await asyncio.to_thread(construir_resumen_diario)
        await context.bot.send_message(
            chat_id=int(ADMIN_TELEGRAM_CHAT_ID),
            text=texto,
            parse_mode="Markdown",
        )
        log.info("Resumen diario enviado al admin")
    except Exception:
        log.exception("Error mandando resumen diario")


# FSM per-chat para gasto manual conversacional.
# chat_id -> {"step": "monto"|"nit"|"nombre"|"desc"|"confirm", "monto":..., "nit":..., "nombre":..., "desc":...}
_GASTO_MANUAL_POR_CHAT: dict[int, dict] = {}


async def cmd_gastomanual(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Inicia FSM conversacional para crear un DS sin foto/PDF."""
    assert update.message and update.effective_chat
    chat_id = update.effective_chat.id
    ok, _info = _is_authorized(chat_id)
    if not ok:
        await update.message.reply_text("No autorizado")
        return
    _GASTO_MANUAL_POR_CHAT[chat_id] = {"step": "monto"}
    await update.message.reply_text(
        "💸 *Gasto manual sin factura (DS)*\n\n"
        "¿Cuánto? Mandame solo el número en pesos.\n"
        "_Ej: 30000_\n\n"
        "Para cancelar mandá /cancelar",
        parse_mode="Markdown",
    )


async def _continuar_gasto_manual(update: Update, ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, texto: str) -> None:
    """Maneja respuestas del FSM de /gastomanual. Devuelve cuando termino el flujo."""
    estado = _GASTO_MANUAL_POR_CHAT.get(chat_id)
    if not estado:
        return
    step = estado.get("step")

    if texto.lower() in ("/cancelar", "cancelar"):
        _GASTO_MANUAL_POR_CHAT.pop(chat_id, None)
        await update.message.reply_text("❌ Gasto manual cancelado.")
        return

    if step == "monto":
        try:
            limpio = "".join(c for c in texto if c.isdigit() or c == ".")
            monto = float(limpio)
            if monto <= 0:
                raise ValueError("debe ser positivo")
        except (ValueError, TypeError):
            await update.message.reply_text(
                "⚠️ Mandame solo el número. Ej: `30000`. /cancelar para salir.",
                parse_mode="Markdown",
            )
            return
        estado["monto"] = monto
        estado["step"] = "nit"
        await update.message.reply_text(
            f"💰 Monto: `${monto:,.0f}`\n\n"
            f"Ahora el *NIT o cédula* del proveedor (solo dígitos):",
            parse_mode="Markdown",
        )
        return

    if step == "nit":
        nit_clean = "".join(c for c in texto if c.isdigit())
        if not nit_clean:
            await update.message.reply_text("⚠️ Necesito el NIT o cédula en números. /cancelar para salir.")
            return
        estado["nit"] = nit_clean
        # Verificar si ya existe en Siigo
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT name FROM siigo_customers WHERE identification = ? LIMIT 1",
                (nit_clean,),
            ).fetchone()
        finally:
            conn.close()
        if row:
            estado["nombre"] = row["name"]
            estado["step"] = "desc"
            await update.message.reply_text(
                f"✅ Proveedor existente: *{row['name']}*\n\n"
                f"¿Qué fue el gasto? (descripción breve)",
                parse_mode="Markdown",
            )
        else:
            estado["step"] = "nombre"
            await update.message.reply_text(
                f"🆕 NIT `{nit_clean}` no está en Siigo.\n\n"
                f"Mandame el *nombre del proveedor* (lo creo como persona natural):",
                parse_mode="Markdown",
            )
        return

    if step == "nombre":
        nombre = texto.strip()[:200]
        if len(nombre) < 2:
            await update.message.reply_text("⚠️ Nombre muy corto. Mandame el nombre del proveedor:")
            return
        estado["nombre"] = nombre
        estado["step"] = "desc"
        await update.message.reply_text(
            f"OK, proveedor: *{nombre}*\n\n"
            f"¿Qué fue el gasto? (descripción breve)",
            parse_mode="Markdown",
        )
        return

    if step == "desc":
        desc = texto.strip()[:200]
        if len(desc) < 3:
            await update.message.reply_text("⚠️ Descripción muy corta. Probá algo como 'taxi al aeropuerto':")
            return
        estado["desc"] = desc
        estado["step"] = "pago"
        await update.message.reply_text(
            f"📝 _{desc}_\n\n"
            f"¿De qué caja salió el dinero?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💵 Efectivo", callback_data="gmpay:efectivo")],
                [InlineKeyboardButton("📱 Nequi", callback_data="gmpay:nequi")],
                [InlineKeyboardButton("📱 Daviplata", callback_data="gmpay:daviplata")],
                [InlineKeyboardButton("🏦 Banco Ahorros", callback_data="gmpay:banco_ahorros")],
                [InlineKeyboardButton("⏳ Quedó pendiente (crédito proveedores)", callback_data="gmpay:credito")],
                [InlineKeyboardButton("❌ Cancelar", callback_data="gmcanc")],
            ]),
            parse_mode="Markdown",
        )
        return


# =============================================================================
# FSM: crear cliente nuevo desde pedido
# =============================================================================

async def _iniciar_cliente_nuevo(update_or_cb, ctx, chat_id: int, pedido_id: int, nombre_sugerido: str) -> None:
    """Inicia el FSM de creacion de cliente desde un pedido."""
    _CLIENTE_NUEVO_POR_CHAT[chat_id] = {
        "pedido_id": pedido_id,
        "nombre": nombre_sugerido,
        "step": "nit",
    }
    msg = (
        f"🆕 *Nuevo cliente: {nombre_sugerido}*\n\n"
        f"Mandame el *NIT o cédula* (solo dígitos).\n\n"
        f"/cancelar para abortar."
    )
    if hasattr(update_or_cb, "edit_message_text"):
        await update_or_cb.edit_message_text(msg, parse_mode="Markdown")
    else:
        await update_or_cb.message.reply_text(msg, parse_mode="Markdown")


async def _continuar_cliente_nuevo(update: Update, ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, texto: str) -> None:
    """Recibe respuestas del FSM de creacion de cliente."""
    estado = _CLIENTE_NUEVO_POR_CHAT.get(chat_id)
    if not estado:
        return

    if texto.lower() in ("/cancelar", "cancelar"):
        _CLIENTE_NUEVO_POR_CHAT.pop(chat_id, None)
        await update.message.reply_text("❌ Creación de cliente cancelada.")
        return

    step = estado.get("step")

    if step == "nit":
        nit_clean = "".join(c for c in texto if c.isdigit())
        if not nit_clean or len(nit_clean) < 5:
            await update.message.reply_text(
                "⚠️ Necesito el NIT o cédula en dígitos (mínimo 5). /cancelar para abortar."
            )
            return
        # Verificar si ya existe
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT id, name FROM siigo_customers WHERE identification = ?",
                (nit_clean,),
            ).fetchone()
        finally:
            conn.close()
        if row:
            await update.message.reply_text(
                f"⚠️ Ese NIT ya existe como *{row['name']}*. ¿Querés cancelar y elegirlo del listado?\n\n"
                f"Si querés agregar otro NIT, mandalo. /cancelar para abortar.",
                parse_mode="Markdown",
            )
            return
        estado["nit"] = nit_clean
        estado["step"] = "tipo"
        await update.message.reply_text(
            f"📋 NIT: `{nit_clean}`\n*Nombre:* {estado['nombre']}\n\n"
            f"¿Es empresa o persona natural?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏢 Empresa", callback_data="newclitipo:empresa")],
                [InlineKeyboardButton("👤 Persona natural", callback_data="newclitipo:persona")],
                [InlineKeyboardButton("❌ Cancelar", callback_data="newclicanc")],
            ]),
            parse_mode="Markdown",
        )
        return


async def cmd_factura(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Activa modo 'esperando factura'. La proxima foto/PDF se procesa como factura proveedor."""
    assert update.message and update.effective_chat
    chat_id = update.effective_chat.id
    ok, _info = _is_authorized(chat_id)
    if not ok:
        await update.message.reply_text("No autorizado")
        return
    _activar_modo_factura(chat_id)
    await update.message.reply_text(
        "📸 *Modo factura activo (5 min)*\n\n"
        "Mandame la *foto* o *PDF* de la factura del proveedor.\n\n"
        "_También sirve para gastos administrativos o cuentas de cobro._",
        parse_mode="Markdown",
    )


async def cmd_correos(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Lee correos no leidos via IMAP, procesa adjuntos y muestra facturas extraidas."""
    assert update.message and update.effective_chat
    chat_id = update.effective_chat.id
    ok, info = _is_authorized(chat_id)
    if not ok or (info and info.get("rol") != "admin"):
        await update.message.reply_text("Solo el admin puede revisar correos.")
        return

    from skiimo.config import IMAP_ENABLED
    if not IMAP_ENABLED:
        await update.message.reply_text(
            "⚠️ IMAP no está configurado.\n\n"
            "Completá en `.env`:\n"
            "  IMAP_USER=...\n"
            "  IMAP_APP_PASSWORD=...\n\n"
            "Y reiniciá el bot.",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text("📥 Revisando correos...")
    await update.message.reply_chat_action("typing")

    from skiimo.gmail_imap import leer_correos
    try:
        r = await asyncio.to_thread(leer_correos, limit=15, marcar_leidos=True)
    except Exception as e:
        log.exception("Error leyendo correos")
        await update.message.reply_text(f"⚠️ Error: {e}")
        return

    if r.errores:
        err_txt = "\n".join(f"  • {e}" for e in r.errores[:3])
        await update.message.reply_text(f"⚠️ Errores:\n{err_txt}")

    if not r.facturas_extraidas:
        msg = (
            f"📭 Sin facturas nuevas.\n\n"
            f"_Correos leidos: {r.correos_leidos} · nuevos: {r.correos_nuevos}_"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
        return

    await update.message.reply_text(
        f"📨 *{len(r.facturas_extraidas)} facturas extraídas del correo*\n"
        f"_Las muestro una por una_",
        parse_mode="Markdown",
    )

    for f in r.facturas_extraidas:
        await _mostrar_factura_correo(update, ctx, f.id)


async def _mostrar_factura_correo(update_or_cb, ctx, factura_id: int) -> None:
    """Muestra una factura de correo con botones para confirmar/descartar/cambiar categoria."""
    from skiimo.gmail_imap import get_factura_correo
    fc = get_factura_correo(factura_id)
    if not fc:
        return

    payload = json.loads(fc["payload_extraido"] or "{}")
    cat = payload.get("categoria") or "gasto_administrativo"
    cat_labels = {
        "materias_primas": "📦 MATERIAS PRIMAS",
        "gasto_administrativo": "🧾 GASTO ADMINISTRATIVO",
        "documento_soporte": "📄 DOCUMENTO SOPORTE",
    }
    cat_label = cat_labels.get(cat, cat)

    # Total recalculado desde items (en caso de que se hayan editado)
    items = payload.get("items") or []
    total_recalc = sum(
        float(it.get("cantidad") or 0) * float(it.get("precio_unitario") or 0)
        for it in items
    )
    # Si el total recalculado difiere mucho del OCR, usamos el recalc (refleja ediciones)
    total_show = total_recalc if total_recalc > 0 else float(fc.get('total') or 0)

    lines = [
        f"📨 *Factura de correo #{factura_id}*",
        f"",
        f"📤 De: _{(fc.get('remitente') or '?')[:50]}_",
        f"📋 Asunto: _{(fc.get('asunto') or '?')[:60]}_",
        f"",
        f"*Proveedor:* {fc.get('proveedor_nombre') or '?'}",
        f"*NIT:* `{fc.get('proveedor_nit') or '?'}`",
        f"*Número factura:* `{fc.get('numero_factura') or '?'}`",
        f"*Total:* `${total_show:,.0f}`",
    ]
    # Lista de items si los hay
    if items:
        lines.append("")
        lines.append("*Items:*")
        for i, it in enumerate(items, start=1):
            qty = float(it.get("cantidad") or 0)
            pre = float(it.get("precio_unitario") or 0)
            sub = qty * pre
            desc = (it.get("descripcion") or "?")[:40]
            qty_str = f"{int(qty)}" if qty == int(qty) else f"{qty:g}"
            lines.append(f"  {i}. {desc}")
            lines.append(f"     `{qty_str} x ${pre:,.0f} = ${sub:,.0f}`")
    lines.append("")
    lines.append(f"*Categoría detectada:* {cat_label}")
    lines.append(f"_Confianza IA: {float(fc.get('confidence') or 0):.0%}_")

    if fc.get("estado") == "duplicada":
        lines.append("")
        lines.append(f"⚠️ *DUPLICADA*: {fc.get('error')}")

    buttons: list[list[InlineKeyboardButton]] = []
    if fc["estado"] == "pendiente":
        # Boton recomendado primero (la categoria detectada)
        buttons.append([InlineKeyboardButton(
            f"✅ Crear como {cat_label}",
            callback_data=f"fcok:{factura_id}:{cat}",
        )])
        # Botones para cambiar a las otras 2 categorias
        opciones_cambio = [
            ("materias_primas", "📦 Materias primas"),
            ("gasto_administrativo", "🧾 Gasto admin"),
            ("documento_soporte", "📄 Doc. soporte (persona natural)"),
        ]
        for opt_cat, opt_label in opciones_cambio:
            if opt_cat == cat:
                continue
            buttons.append([InlineKeyboardButton(
                f"🔄 Cambiar a {opt_label}",
                callback_data=f"fcok:{factura_id}:{opt_cat}",
            )])
        # Botones de edicion por item (max 5 items mostrados)
        for i, _it in enumerate(items[:5]):
            buttons.append([InlineKeyboardButton(
                f"✏️ Editar item {i+1}",
                callback_data=f"iedi:{factura_id}:{i}",
            )])
        buttons.append([InlineKeyboardButton(
            "❌ Descartar",
            callback_data=f"fcno:{factura_id}",
        )])

    target = update_or_cb.message if hasattr(update_or_cb, "message") else update_or_cb
    if hasattr(update_or_cb, "edit_message_text"):
        # Es un callback query
        await update_or_cb.edit_message_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )
    else:
        await target.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode="Markdown",
        )


async def cmd_cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message and update.effective_chat
    chat_id = update.effective_chat.id
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM bot_pedidos WHERE telegram_chat_id = ? AND estado = 'borrador' "
            "ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    finally:
        conn.close()
    if row:
        _update_pedido_estado(row["id"], "cancelado")
        await update.message.reply_text(f"Pedido #{row['id']} cancelado")
    else:
        await update.message.reply_text("No hay pedido en curso")


# =============================================================================
# HANDLERS DE MENSAJES
# =============================================================================

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message and update.effective_chat
    chat_id = update.effective_chat.id
    ok, info = _is_authorized(chat_id)
    if not ok:
        # ¿Es un TRABAJADOR (empleado vinculado)? -> solo menu de asistencia.
        emp = _empleado_vinculado(chat_id)
        if emp:
            from skiimo.bot.asistencia_commands import cmd_mi_help
            await cmd_mi_help(update, ctx)
            return
        # No vinculado: si el texto parece una cedula, intentar vincular.
        posible_ced = (update.message.text or "").strip().replace(".", "").replace(" ", "")
        if posible_ced.isdigit() and len(posible_ced) >= 5:
            from skiimo.bot.asistencia_commands import vincular_por_cedula
            emp = vincular_por_cedula(chat_id, posible_ced)
            if emp:
                await update.message.reply_text(
                    f"¡Listo, {emp['nombre']}! Quedaste identificado.\n\n"
                    "Puedes consultar:\n"
                    "/mi_asistencia — tus marcajes de hoy\n"
                    "/mi_nomina — cuánto llevas esta quincena\n"
                    "/mis_kpis — tu asistencia y tardanzas del mes"
                )
                await _aplicar_menu_por_rol(ctx.bot, chat_id)
            else:
                await update.message.reply_text(
                    "No encontré esa cédula en el sistema. Verifica el número "
                    "o pídele al administrador que te registre."
                )
            return
        await update.message.reply_text(
            "Para identificarte, envíame tu número de cédula."
        )
        return

    texto = (update.message.text or "").strip()

    # Si hay un comprobante esperando cliente para este chat, este texto se interpreta como busqueda
    comp_id_esperando = _ESPERANDO_CLIENTE_POR_CHAT.get(chat_id)
    if comp_id_esperando is not None:
        await _resolver_busqueda_cliente(update, comp_id_esperando, texto)
        return

    # Si hay un FSM de gasto manual activo, las respuestas van por ahi
    if chat_id in _GASTO_MANUAL_POR_CHAT:
        await _continuar_gasto_manual(update, ctx, chat_id, texto)
        return

    # Si hay edicion de item activa, este texto es el numero nuevo
    if chat_id in _EDIT_ITEM_POR_CHAT:
        await _continuar_edit_item(update, ctx, chat_id, texto)
        return

    # Si hay flujo de creacion de cliente nuevo, este texto es la respuesta
    if chat_id in _CLIENTE_NUEVO_POR_CHAT:
        await _continuar_cliente_nuevo(update, ctx, chat_id, texto)
        return

    # Si hay un proveedor pendiente esperando nombre, este texto es el nombre nuevo
    prv_pend = _PROVEEDOR_PENDIENTE_POR_CHAT.get(chat_id)
    if prv_pend and prv_pend.get("esperando_nombre"):
        prv_pend["nombre"] = texto[:200]
        prv_pend.pop("esperando_nombre", None)
        await update.message.reply_text(
            f"📝 Nombre actualizado: *{texto[:80]}*\n\n"
            f"NIT/Cedula: `{prv_pend['nit']}`\n\n"
            f"¿Es empresa o persona natural?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏢 Empresa", callback_data=f"prvcrear:{prv_pend['factura_id']}:empresa")],
                [InlineKeyboardButton("👤 Persona natural", callback_data=f"prvcrear:{prv_pend['factura_id']}:persona")],
                [InlineKeyboardButton("❌ Cancelar", callback_data=f"fcno:{prv_pend['factura_id']}")],
            ]),
            parse_mode="Markdown",
        )
        return

    role = info.get("rol", "vendedor") if info else "vendedor"
    await update.message.reply_chat_action("typing")

    try:
        reply = await asyncio.to_thread(process_message, chat_id, texto, user_role=role)
    except Exception as e:
        log.exception("Error en agente")
        await update.message.reply_text(f"Error: {e}")
        return

    await _dispatch_agent_reply(update, ctx, reply)


async def _resolver_busqueda_cliente(update: Update, comp_id: int, query: str) -> None:
    """Cuando el usuario escribe nombre/NIT/factura despues de tocar 'Buscar otro cliente'."""
    assert update.message and update.effective_chat
    chat_id = update.effective_chat.id
    comp = _COMPROBANTE_CACHE.get(comp_id)
    if not comp:
        _ESPERANDO_CLIENTE_POR_CHAT.pop(chat_id, None)
        await update.message.reply_text("⚠️ Comprobante expirado, manda la foto de nuevo.")
        return

    # Permitir cancelar
    if query.lower() in ("cancelar", "cancel", "salir", "nada"):
        _ESPERANDO_CLIENTE_POR_CHAT.pop(chat_id, None)
        await update.message.reply_text("OK, busqueda cancelada. El comprobante sigue pendiente.")
        return

    # ¿Es nombre de factura? (formato FV-X-XXXX o solo numero)
    import re
    is_factura = bool(re.match(r"^(FV-\d+-)?\d{2,6}$", query.upper()))

    if is_factura:
        # Buscar directamente la factura
        from skiimo.siigo_payments import _buscar_factura_por_nombre
        inv = _buscar_factura_por_nombre(query)
        if not inv:
            await update.message.reply_text(
                f"⚠️ No encontré factura '{query}'.\n"
                "Probá con el nombre del cliente o un NIT."
            )
            return
        # Ya tenemos factura: aplicar directamente
        _ESPERANDO_CLIENTE_POR_CHAT.pop(chat_id, None)
        await _aplicar_comprobante_a_factura(update, comp_id, inv["name"])
        return

    # Buscar cliente por nombre/NIT
    from skiimo.matcher import Matcher
    m = Matcher()
    hits = m.search_customer(query, limit=5)
    if not hits:
        await update.message.reply_text(
            f"⚠️ No encontré clientes que coincidan con '{query}'.\n"
            "Intentá con el nombre completo, NIT, o escribí 'cancelar'."
        )
        return

    # Re-rankear por historial de compras
    conn = get_conn()
    try:
        scored = []
        for h in hits:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM siigo_invoices WHERE customer_id = ? AND balance > 0",
                (h.id,),
            ).fetchone()[0]
            scored.append((cnt, h))
        scored.sort(key=lambda x: x[0], reverse=True)
    finally:
        conn.close()

    # Tomar el cliente con mas facturas pendientes
    top = scored[0]
    if top[0] == 0:
        # Ningun candidato tiene facturas pendientes
        await update.message.reply_text(
            f"⚠️ Encontré clientes pero ninguno tiene facturas pendientes:\n"
            + "\n".join(f"  • {h.name}" for _, h in scored[:3])
            + "\n\nProbá con otro nombre o NIT."
        )
        return

    c = top[1]
    # Buscar facturas pendientes del cliente y mostrar para elegir
    conn = get_conn()
    try:
        facturas = conn.execute(
            """SELECT id, name, date, total, balance FROM siigo_invoices
               WHERE customer_id = ? AND balance > 0
               ORDER BY date DESC LIMIT 10""",
            (c.id,),
        ).fetchall()
    finally:
        conn.close()

    if not facturas:
        await update.message.reply_text(
            f"⚠️ {c.name} no tiene facturas pendientes."
        )
        return

    # Mostrar lista con botones
    msg_lines = [
        f"*Cliente:* {c.name} (NIT {c.identification})",
        f"*Pago recibido:* `${comp['monto']:,.0f}` ({comp['metodo_detectado']})",
        "",
        "*Elegí la factura:*",
    ]
    buttons: list[list[InlineKeyboardButton]] = []
    for f in facturas:
        saldo = float(f["balance"])
        # Marca si el saldo coincide cercano al monto
        diff_pct = abs(saldo - comp["monto"]) / saldo * 100 if saldo else 100
        marca = ""
        if abs(saldo - comp["monto"]) < 1.0:
            marca = " ✓"
        elif diff_pct < 15:
            marca = " ~"
        label = f"{f['name']}{marca} — ${saldo:,.0f}"
        buttons.append([InlineKeyboardButton(
            label[:60],
            callback_data=f"cpapply:{comp_id}:{f['id']}",
        )])
    buttons.append([InlineKeyboardButton(
        "🔍 Otro cliente", callback_data=f"cpsearch:{comp_id}",
    )])
    buttons.append([InlineKeyboardButton(
        "❌ Descartar", callback_data=f"cpcancel:{comp_id}",
    )])

    # Limpiar el flag de "esperando" porque ya resolvimos
    _ESPERANDO_CLIENTE_POR_CHAT.pop(chat_id, None)

    await update.message.reply_text(
        "\n".join(msg_lines),
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


async def _aplicar_comprobante_a_factura(
    update: Update, comp_id: int, factura_name: str,
) -> None:
    """Despacha el comprobante al flujo de pagos sobre una factura especifica.
    Esto reproduce lo que hace _handle_comprobante_callback con accion='cpapply',
    pero invocado desde texto.
    """
    assert update.message
    comp = _COMPROBANTE_CACHE.get(comp_id)
    if not comp:
        await update.message.reply_text("⚠️ Comprobante expirado.")
        return

    from skiimo.siigo_payments import analizar_pago
    a = await asyncio.to_thread(analizar_pago, factura_name, comp["monto"])
    if a is None:
        await update.message.reply_text(f"⚠️ No pude analizar {factura_name}.")
        return

    analisis = {
        "pendiente_confirmacion": True,
        "factura": a.factura_name,
        "factura_id": a.factura_id,
        "cliente": a.cliente_nombre,
        "total_factura": a.factura_total,
        "saldo_actual": a.factura_balance,
        "monto_pagado": a.monto_pagado,
        "diferencia": round(a.diferencia, 2),
        "diferencia_pct": round(a.diferencia_pct, 1),
        "fecha_factura": a.fecha_factura.isoformat(),
        "fecha_pago": a.fecha_pago.isoformat(),
        "dias_transcurridos": a.dias_transcurridos,
        "metodo_pago": comp["metodo"],
        "opciones": a.opciones,
        "_img_hash": comp.get("img_hash"),  # propagar para marcar despues
    }
    propuesta_id = _save_pago_propuesta(analisis)
    texto = (
        f"📥 *Aplicar pago a {factura_name}*\n\n"
        f"Cliente: {a.cliente_nombre}\n"
        f"Saldo factura: `${a.factura_balance:,.0f}`\n"
        f"Pago recibido: `${a.monto_pagado:,.0f}` ({comp['metodo_detectado']})\n"
        f"Días: {a.dias_transcurridos}\n\n"
        f"_¿Cómo lo registro?_"
    )
    buttons: list[list[InlineKeyboardButton]] = []
    for i, opt in enumerate(a.opciones):
        tipo = opt.get("tipo")
        label = opt.get("label", "?")
        if tipo == "error":
            continue
        emoji = {"completo": "✅", "pp": "🎯", "pp_forzar": "⚠️", "abono": "💵"}.get(tipo, "•")
        buttons.append([InlineKeyboardButton(
            f"{emoji}  {label}",
            callback_data=f"payopt:{propuesta_id}:{i}",
        )])
    buttons.append([InlineKeyboardButton("❌  Cancelar", callback_data=f"paycanc:{propuesta_id}")])
    await update.message.reply_text(
        texto,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )
    _COMPROBANTE_CACHE.pop(comp_id, None)


async def handle_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    assert update.message and update.effective_chat
    chat_id = update.effective_chat.id
    ok, info = _is_authorized(chat_id)
    if not ok:
        await update.message.reply_text("No autorizado")
        return

    await update.message.reply_chat_action("typing")
    voice = update.message.voice or update.message.audio
    if not voice:
        return

    tg_file = await voice.get_file()
    audio_bytes = bytes(await tg_file.download_as_bytearray())
    role = info.get("rol", "vendedor") if info else "vendedor"
    try:
        reply = await asyncio.to_thread(
            process_message, chat_id, "", user_role=role,
            media_bytes=audio_bytes, media_mime="audio/ogg",
        )
    except Exception as e:
        log.exception("Error procesando audio")
        await update.message.reply_text(f"No pude procesar el audio: {e}")
        return
    await _dispatch_agent_reply(update, ctx, reply)


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Recibe foto o documento (imagen / PDF).

    Routing:
      - Si el chat esta en modo factura (via /factura): procesar como factura proveedor.
      - En otro caso: tratar como comprobante de pago.
    """
    import hashlib
    assert update.message and update.effective_chat
    chat_id = update.effective_chat.id
    ok, _info = _is_authorized(chat_id)
    if not ok:
        await update.message.reply_text("No autorizado")
        return

    await update.message.reply_chat_action("typing")

    # Obtener bytes (foto = imagen comprimida; document = archivo crudo, puede ser PDF)
    img_bytes: bytes
    mime = "image/jpeg"
    filename = "imagen.jpg"
    if update.message.photo:
        biggest = update.message.photo[-1]
        f = await biggest.get_file()
        img_bytes = bytes(await f.download_as_bytearray())
    elif update.message.document:
        doc = update.message.document
        mime = doc.mime_type or "image/jpeg"
        filename = doc.file_name or "archivo"
        f = await doc.get_file()
        img_bytes = bytes(await f.download_as_bytearray())
    else:
        return

    # Si el chat esta en "modo factura" (comando /factura previo), rutear a factura proveedor
    if _consumir_modo_factura(chat_id):
        await _procesar_foto_como_factura(update, ctx, chat_id, img_bytes, mime, filename)
        return

    # Si es PDF y NO esta en modo factura, no es comprobante valido
    if mime == "application/pdf":
        await update.message.reply_text(
            "📄 Recibí un PDF. Si es una factura de proveedor, mandá `/factura` antes y luego el PDF.",
            parse_mode="Markdown",
        )
        return

    # Detectar duplicado por hash del archivo
    img_hash = hashlib.sha256(img_bytes).hexdigest()
    dup = _get_comprobante_by_hash(img_hash)
    if dup:
        estado = dup.get("estado")
        if estado == "aplicado":
            msg = (
                f"⚠️ *Este comprobante ya fue procesado*\n\n"
                f"Monto: `${dup['monto']:,.0f}`\n"
                f"Factura aplicada: {dup.get('factura_aplicada')}\n"
                f"Recibo: {dup.get('rc_name')}\n"
            )
            if dup.get("nc_name"):
                msg += f"Nota crédito: {dup['nc_name']}\n"
            msg += f"\n_Procesado el {dup.get('created_at')}_"
            await update.message.reply_text(msg, parse_mode="Markdown")
            return
        elif estado == "descartado":
            await update.message.reply_text(
                f"⚠️ Este comprobante ya había sido *descartado*. "
                f"Si querés reprocesarlo, decímelo en chat.",
                parse_mode="Markdown",
            )
            return
        # Si esta pendiente, lo dejamos seguir (re-procesamos)

    try:
        from skiimo.llm.gemini import extract_comprobante_pago
        comp = await asyncio.to_thread(extract_comprobante_pago, img_bytes, mime)
    except Exception as e:
        log.exception("Error OCR")
        await update.message.reply_text(f"No pude leer la imagen: {e}")
        return

    if not comp.es_comprobante_valido:
        await update.message.reply_text(
            "🤔 No identifiqué esto como un comprobante de pago válido.\n\n"
            f"_Detalle: {comp.observaciones or 'imagen poco clara'}_",
            parse_mode="Markdown",
        )
        return

    # Mapear metodo Gemini -> id Siigo
    metodo_map = {
        "nequi": "nequi", "daviplata": "daviplata",
        "bancolombia": "banco_ahorros", "davivienda": "banco_ahorros",
        "banco_otro": "banco_ahorros",
        "efectivo": "efectivo",
        "tarjeta_debito": "tarjeta_debito",
        "tarjeta_credito": "tarjeta_credito",
    }
    metodo = metodo_map.get(comp.metodo_pago, "banco_ahorros")

    # Guardar comprobante en cache para los botones
    comp_data = {
        "monto": comp.monto,
        "metodo": metodo,
        "metodo_detectado": comp.metodo_pago,
        "fecha": comp.fecha_pago,
        "referencia": comp.numero_referencia,
        "titular_origen": comp.titular_origen,
        "confidence": comp.confidence,
        "img_hash": img_hash,
        "chat_id": chat_id,
    }
    comp_id = _save_comprobante(comp_data)
    # Persistir en DB (estado pendiente)
    _save_comprobante_db(comp_data, img_hash, estado="pendiente", actor=f"chat:{chat_id}")

    # Buscar clientes candidatos con saldo cercano al monto
    candidatos = _candidatos_para_pago(comp.monto)

    # Escapar caracteres especiales de Markdown en los valores del OCR.
    # El OCR de Gemini puede devolver texto con _ * ` [ ] que rompen el parser.
    def _md_esc(s: str) -> str:
        if not s:
            return ""
        out = str(s)
        for ch in ("\\", "_", "*", "`", "[", "]"):
            out = out.replace(ch, "\\" + ch)
        return out

    # Armar mensaje
    msg_lines = [
        f"📥 *Comprobante de pago detectado*",
        f"",
        f"💰 Monto: `${comp.monto:,.0f}`",
        f"💳 Método: _{_md_esc(comp.metodo_pago)}_",
    ]
    if comp.fecha_pago:
        msg_lines.append(f"📅 Fecha: {_md_esc(comp.fecha_pago)}")
    if comp.numero_referencia:
        msg_lines.append(f"🔢 Ref: `{_md_esc(comp.numero_referencia)}`")
    if comp.titular_origen:
        msg_lines.append(f"👤 De: {_md_esc(comp.titular_origen)}")
    msg_lines.append(f"")
    msg_lines.append(f"_Confianza IA: {comp.confidence:.0%}_")
    msg_lines.append("")

    buttons: list[list[InlineKeyboardButton]] = []
    if candidatos:
        msg_lines.append("*¿A quién aplico el pago?*")
        msg_lines.append("Candidatos con saldo similar:")
        for cand in candidatos[:5]:
            diff_label = ""
            if cand.get("diff_pct") is not None:
                diff_label = f" ({cand['diff_pct']:+.0f}%)"
            cliente_safe = _md_esc(cand['cliente'])
            msg_lines.append(f"  • {cliente_safe} — saldo `${cand['saldo']:,.0f}`{diff_label}")
            label = f"{cand['cliente'][:25]} ${cand['saldo']:,.0f}"
            buttons.append([InlineKeyboardButton(
                label[:60],
                callback_data=f"cpapply:{comp_id}:{cand['factura_id']}",
            )])
    else:
        msg_lines.append("_No encontré facturas con saldo cercano. Decime el nombre del cliente o NIT._")

    # Boton manual: escribir cliente
    buttons.append([InlineKeyboardButton(
        "🔍  Buscar otro cliente",
        callback_data=f"cpsearch:{comp_id}",
    )])
    buttons.append([InlineKeyboardButton("❌  Descartar", callback_data=f"cpcancel:{comp_id}")])

    await _reply_md_safe(
        update,
        "\n".join(msg_lines),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _procesar_foto_como_factura(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    file_bytes: bytes,
    mime: str,
    filename: str,
) -> None:
    """OCR de factura de proveedor subida por chat. Si el proveedor no existe en
    Siigo, abre un sub-flujo para crearlo antes de mostrar el resumen normal."""
    assert update.message

    from skiimo.gmail_imap import procesar_factura_subida_manual

    await update.message.reply_text("📥 *Procesando factura...*", parse_mode="Markdown")
    factura_id, error, dup = await asyncio.to_thread(
        procesar_factura_subida_manual,
        chat_id=chat_id,
        file_bytes=file_bytes,
        mime=mime,
        filename=filename,
    )

    if error:
        await update.message.reply_text(f"⚠️ {error}")
        return

    if dup:
        motivo = dup.get("motivo", "?")
        if motivo == "hash_duplicado":
            await update.message.reply_text(
                f"⚠️ *Esta factura ya fue procesada* (estado: {dup.get('estado')})\n"
                f"NIT: `{dup.get('proveedor_nit') or '?'}`  ·  Numero: `{dup.get('numero_factura') or '?'}`",
                parse_mode="Markdown",
            )
        else:
            siigo_name = dup.get("siigo_name") or dup.get("siigo_purchase_name") or "?"
            await update.message.reply_text(
                f"⚠️ *Factura duplicada*\n\n"
                f"Ya existe en {dup.get('fuente', 'Siigo')}: `{siigo_name}`",
                parse_mode="Markdown",
            )
        return

    if not factura_id:
        await update.message.reply_text("⚠️ No pude procesar la factura.")
        return

    # Verificar si el proveedor existe en el espejo Siigo
    from skiimo.gmail_imap import get_factura_correo
    fc = get_factura_correo(factura_id)
    if not fc:
        await update.message.reply_text("⚠️ Factura guardada pero no la encuentro de vuelta.")
        return

    nit = (fc.get("proveedor_nit") or "").strip()
    nombre = (fc.get("proveedor_nombre") or "").strip()

    existe_proveedor = False
    if nit:
        nit_clean = "".join(c for c in nit if c.isdigit())
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT id, name FROM siigo_customers WHERE identification = ? LIMIT 1",
                (nit_clean,),
            ).fetchone()
            existe_proveedor = row is not None
        finally:
            conn.close()

    if not existe_proveedor and nit:
        # Mostrar paso de creacion de proveedor
        _PROVEEDOR_PENDIENTE_POR_CHAT[chat_id] = {
            "factura_id": factura_id,
            "nit": nit,
            "nombre": nombre,
        }
        await update.message.reply_text(
            f"🆕 *Proveedor desconocido*\n\n"
            f"NIT/Cedula: `{nit}`\n"
            f"Nombre detectado: _{nombre or '?'}_\n\n"
            f"No existe en Siigo. ¿Lo creo?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🏢 Crear como empresa",
                    callback_data=f"prvcrear:{factura_id}:empresa",
                )],
                [InlineKeyboardButton(
                    "👤 Crear como persona natural",
                    callback_data=f"prvcrear:{factura_id}:persona",
                )],
                [InlineKeyboardButton(
                    "✏️ Cambiar nombre",
                    callback_data=f"prvedit:{factura_id}",
                )],
                [InlineKeyboardButton(
                    "❌ Cancelar",
                    callback_data=f"fcno:{factura_id}",
                )],
            ]),
            parse_mode="Markdown",
        )
        return

    if not nit:
        await update.message.reply_text(
            "⚠️ La factura no tiene NIT detectado. Mandala con más calidad o decímelo a mano."
        )
        return

    # Flujo normal: mostrar resumen + categorias
    await _mostrar_factura_correo(update, ctx, factura_id)


_COMPROBANTE_CACHE: dict[int, dict] = {}
_COMPROBANTE_COUNTER = [0]
# chat_id -> comprobante_id (en cache) cuando se esta esperando que el usuario
# escriba el nombre/NIT del cliente para aplicar un comprobante
_ESPERANDO_CLIENTE_POR_CHAT: dict[int, int] = {}

# chat_id -> timestamp expira_at (epoch). Si el chat esta en este dict y no expiro,
# la proxima foto/PDF se trata como factura de proveedor (no comprobante de pago).
_MODO_FACTURA_POR_CHAT: dict[int, float] = {}
MODO_FACTURA_TTL = 5 * 60  # 5 minutos

# chat_id -> dict con datos de proveedor pendiente de crear
# {factura_id, nit, nombre_sugerido, mensaje_id}
_PROVEEDOR_PENDIENTE_POR_CHAT: dict[int, dict] = {}

# FSM creacion de cliente nuevo desde pedido: chat_id -> {pedido_id, nombre, step, nit, tipo, categoria}
# steps: 'nit' -> 'tipo' -> 'categoria' -> 'confirm'
_CLIENTE_NUEVO_POR_CHAT: dict[int, dict] = {}


def _activar_modo_factura(chat_id: int) -> None:
    import time as _time
    _MODO_FACTURA_POR_CHAT[chat_id] = _time.time() + MODO_FACTURA_TTL


def _consumir_modo_factura(chat_id: int) -> bool:
    """Devuelve True si el chat estaba en modo factura (y lo limpia)."""
    import time as _time
    exp = _MODO_FACTURA_POR_CHAT.get(chat_id)
    if exp is None:
        return False
    if _time.time() > exp:
        _MODO_FACTURA_POR_CHAT.pop(chat_id, None)
        return False
    _MODO_FACTURA_POR_CHAT.pop(chat_id, None)
    return True


def _save_comprobante(comp: dict) -> int:
    _COMPROBANTE_COUNTER[0] += 1
    cid = _COMPROBANTE_COUNTER[0]
    _COMPROBANTE_CACHE[cid] = comp
    return cid


def _get_comprobante_by_hash(img_hash: str) -> dict | None:
    """Busca en DB si el hash del comprobante ya fue procesado."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM comprobantes_procesados WHERE img_hash = ? ORDER BY id DESC LIMIT 1",
            (img_hash,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _save_comprobante_db(comp: dict, img_hash: str, estado: str = "pendiente",
                          actor: str = "bot") -> None:
    """Persiste el comprobante en DB con estado pendiente.
    Si ya existe (mismo hash), actualiza."""
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO comprobantes_procesados
               (img_hash, monto, metodo, fecha_pago, numero_referencia, titular_origen,
                estado, actor, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(img_hash) DO UPDATE SET
                 monto = excluded.monto,
                 metodo = excluded.metodo,
                 fecha_pago = excluded.fecha_pago,
                 numero_referencia = excluded.numero_referencia,
                 titular_origen = excluded.titular_origen,
                 estado = excluded.estado""",
            (img_hash, comp.get("monto"), comp.get("metodo"),
             comp.get("fecha"), comp.get("referencia"), comp.get("titular_origen"),
             estado, actor, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()


def _marcar_comprobante_aplicado(img_hash: str, factura_name: str,
                                  rc_name: str | None, nc_name: str | None) -> None:
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE comprobantes_procesados
               SET estado = 'aplicado',
                   factura_aplicada = ?,
                   rc_name = ?,
                   nc_name = ?
               WHERE img_hash = ?""",
            (factura_name, rc_name, nc_name, img_hash),
        )
        conn.commit()
    finally:
        conn.close()


def _marcar_comprobante_descartado(img_hash: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE comprobantes_procesados SET estado = 'descartado' WHERE img_hash = ?",
            (img_hash,),
        )
        conn.commit()
    finally:
        conn.close()


def _candidatos_para_pago(monto: float, tolerancia_pct: float = 15.0) -> list[dict]:
    """Devuelve facturas con balance cercano al monto pagado.
    Incluye match exacto, match con pronto pago aplicado, y match con tolerancia.
    """
    conn = get_conn()
    try:
        # Trae todas las facturas con saldo > 0
        rows = conn.execute(
            """SELECT i.id, i.name, i.balance, i.date, i.customer_id, c.name as cname
               FROM siigo_invoices i LEFT JOIN siigo_customers c ON c.id = i.customer_id
               WHERE i.balance > 0
               ORDER BY i.date DESC"""
        ).fetchall()
    finally:
        conn.close()

    candidatos: list[dict] = []
    for r in rows:
        saldo = float(r["balance"])
        if saldo <= 0:
            continue
        # 1. Match exacto
        if abs(saldo - monto) < 1.0:
            candidatos.append({
                "factura_id": r["id"], "factura": r["name"],
                "cliente": r["cname"] or "(sin nombre)",
                "saldo": saldo, "tipo": "exacto",
                "diff_pct": 0,
            })
            continue
        # 2. Match con descuento pronto pago (5%, 10%, 15%)
        for pct in (5, 10, 15):
            esperado = saldo * (1 - pct / 100)
            if abs(esperado - monto) < 5.0:  # tolerancia $5
                candidatos.append({
                    "factura_id": r["id"], "factura": r["name"],
                    "cliente": r["cname"] or "(sin nombre)",
                    "saldo": saldo, "tipo": f"pp_{pct}",
                    "diff_pct": -pct,
                })
                break
        else:
            # 3. Tolerancia general (15%)
            diff_pct = abs(saldo - monto) / saldo * 100 if saldo else 100
            if diff_pct <= tolerancia_pct:
                candidatos.append({
                    "factura_id": r["id"], "factura": r["name"],
                    "cliente": r["cname"] or "(sin nombre)",
                    "saldo": saldo, "tipo": "cercano",
                    "diff_pct": ((monto - saldo) / saldo * 100),
                })

    # Ordenar: exactos primero, despues pp, despues cercanos
    orden_tipo = {"exacto": 0, "pp_5": 1, "pp_10": 1, "pp_15": 1, "cercano": 2}
    candidatos.sort(key=lambda x: (orden_tipo.get(x["tipo"], 99), abs(x.get("diff_pct", 0))))
    return candidatos[:10]


async def _dispatch_agent_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE, reply) -> None:
    """Routea la respuesta del agente: si es pedido -> proposal con botones, si es texto -> manda texto."""
    assert update.message
    if reply.kind == "pedido" and reply.pedido:
        await _send_proposal(update, ctx, reply.pedido)
        return

    if reply.kind == "texto":
        msg = (reply.texto or "(sin respuesta)")[:4000]
        # Si se programaron extras por conversacion -> notificar a operarios/coordinadores
        if (reply.tools_used and "programar_extras" in reply.tools_used
                and reply.last_tool_result and reply.last_tool_result.get("_notificar_extras")):
            r = reply.last_tool_result
            try:
                from skiimo.bot.asistencia_commands import _notificar_extras
                n = await _notificar_extras(
                    ctx, r["fecha_desde"], r["fecha_hasta"], r["hora_inicio"], r["hora_fin"]
                )
                msg += f"\n\n📣 Notifiqué a {n} empleado(s)."
            except Exception:
                log.exception("No se pudo notificar extras (conversacional)")
        # Detectar si la ultima tool fue analizar_pago_factura -> mostrar botones
        if (reply.tools_used and "analizar_pago_factura" in reply.tools_used
                and reply.last_tool_result and reply.last_tool_result.get("pendiente_confirmacion")):
            await _send_pago_proposal(update, ctx, msg, reply.last_tool_result)
            return
        # Detectar pago a proveedor
        if (reply.tools_used and "analizar_pago_a_proveedor" in reply.tools_used
                and reply.last_tool_result
                and reply.last_tool_result.get("pendiente_confirmacion_pago_proveedor")):
            await _send_pago_proveedor_proposal(update, ctx, msg, reply.last_tool_result)
            return
        # Detectar propuesta de anulacion de factura (por numero o por cliente)
        if (reply.tools_used and reply.last_tool_result
                and any(t in reply.tools_used for t in (
                    "proponer_anular_factura", "proponer_anular_ultima_factura_cliente",
                ))
                and reply.last_tool_result.get("pendiente_confirmacion_anulacion")):
            await _send_anulacion_proposal(update, ctx, msg, reply.last_tool_result)
            return
        # Detectar modificacion de pedido en curso -> mostrar resumen actualizado
        if (reply.tools_used and "modificar_pedido_actual" in reply.tools_used
                and reply.last_tool_result and reply.last_tool_result.get("ok")):
            pedido_id = reply.last_tool_result.get("pedido_id")
            if pedido_id:
                pedido_row = _load_pedido(pedido_id)
                if pedido_row:
                    rp = _rehydrate_resolved(pedido_row)
                    await update.message.reply_text(
                        f"{msg}\n\n*Pedido #{pedido_id} actualizado*\n\n{format_summary(rp)}",
                        reply_markup=InlineKeyboardMarkup(_build_pedido_buttons(pedido_id, rp)),
                        parse_mode="Markdown",
                    )
                    return
        await update.message.reply_text(msg)
        return

    await update.message.reply_text(reply.texto or "Error desconocido")


async def _send_pago_proposal(update: Update, ctx: ContextTypes.DEFAULT_TYPE, texto: str, analisis: dict) -> None:
    """Muestra propuesta de pago con botones para confirmar opcion."""
    # Guardar el analisis en memoria (cache) asociado a un id corto
    propuesta_id = _save_pago_propuesta(analisis)

    buttons: list[list[InlineKeyboardButton]] = []
    for i, opt in enumerate(analisis.get("opciones", [])):
        tipo = opt.get("tipo")
        label = opt.get("label", "?")
        if tipo == "error":
            continue
        emoji = {"completo": "✅", "pp": "🎯", "pp_forzar": "⚠️", "abono": "💵"}.get(tipo, "•")
        buttons.append([InlineKeyboardButton(
            f"{emoji}  {label}",
            callback_data=f"payopt:{propuesta_id}:{i}",
        )])
    buttons.append([InlineKeyboardButton("❌  Cancelar", callback_data=f"paycanc:{propuesta_id}")])

    await _reply_md_safe(
        update,
        texto[:3500],
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _send_pago_proveedor_proposal(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                                          texto: str, analisis: dict) -> None:
    """Muestra propuesta de pago A PROVEEDOR con botones."""
    propuesta_id = _save_pago_propuesta(analisis)
    buttons: list[list[InlineKeyboardButton]] = []
    for i, opt in enumerate(analisis.get("opciones", [])):
        tipo = opt.get("tipo")
        label = opt.get("label", "?")
        if tipo == "error":
            continue
        emoji = {"completo": "✅", "abono": "💵"}.get(tipo, "•")
        buttons.append([InlineKeyboardButton(
            f"{emoji}  {label}",
            callback_data=f"prvopt:{propuesta_id}:{i}",
        )])
    buttons.append([InlineKeyboardButton("❌  Cancelar", callback_data=f"prvcanc:{propuesta_id}")])

    await _reply_md_safe(
        update,
        texto[:3500],
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _send_anulacion_proposal(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                                     texto: str, analisis: dict) -> None:
    """Muestra propuesta de anulacion de factura con botones de confirmacion."""
    propuesta_id = _save_pago_propuesta(analisis)  # reusamos el cache

    factura_name = analisis.get("factura_name", "?")
    total = analisis.get("total", 0)
    saldo = analisis.get("saldo", 0)
    metodo_rec = analisis.get("metodo_recomendado", "annul")
    razones = analisis.get("razones", []) or []
    es_electronica = analisis.get("es_electronica", False)
    fue_pagada = analisis.get("fue_pagada", False)
    cliente_match = analisis.get("cliente_match")
    ordinal = analisis.get("ordinal")

    lines = [
        f"🗑 *Anular factura {factura_name}*",
    ]
    if cliente_match and ordinal:
        lines.append(f"_(la {ordinal} factura de {cliente_match})_")
    lines.append("")
    lines.append(f"Total factura: `${total:,.0f}`")
    lines.append(f"Saldo actual: `${saldo:,.0f}`")
    lines.append("")
    if es_electronica:
        lines.append("⚠️ Factura electronica con CUFE DIAN")
    if fue_pagada:
        lines.append("⚠️ Factura ya tiene cobros aplicados")

    if metodo_rec == "credit_note":
        lines.append("")
        lines.append("📋 *Recomiendo: Nota Credito*")
        for r in razones:
            lines.append(f"  • {r}")
    else:
        lines.append("")
        lines.append("✅ *Recomiendo: Anulacion directa* (factura limpia, sin cobros)")

    lines.append("")
    lines.append("_Operacion destructiva. Elegi metodo:_")

    buttons: list[list[InlineKeyboardButton]] = []
    # Boton recomendado primero
    if metodo_rec == "annul":
        buttons.append([InlineKeyboardButton(
            "✅ Anular directo (recomendado)",
            callback_data=f"anul:{propuesta_id}:annul",
        )])
        buttons.append([InlineKeyboardButton(
            "📋 Forzar nota credito",
            callback_data=f"anul:{propuesta_id}:credit_note",
        )])
    else:
        buttons.append([InlineKeyboardButton(
            "📋 Nota credito (recomendado)",
            callback_data=f"anul:{propuesta_id}:credit_note",
        )])
        buttons.append([InlineKeyboardButton(
            "⚠️ Intentar /annul igual",
            callback_data=f"anul:{propuesta_id}:annul",
        )])
    buttons.append([InlineKeyboardButton(
        "❌ Cancelar (no hacer nada)",
        callback_data=f"anul:{propuesta_id}:cancel",
    )])

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


_PAGO_CACHE: dict[int, dict] = {}
_PAGO_COUNTER = [0]


def _save_pago_propuesta(analisis: dict) -> int:
    _PAGO_COUNTER[0] += 1
    pid = _PAGO_COUNTER[0]
    _PAGO_CACHE[pid] = analisis
    return pid


async def _handle_pago_callback(cb, ctx, accion: str, parts: list[str]) -> None:
    """Maneja callbacks de propuestas de pago.

    payopt:<propuesta_id>:<opcion_idx>  -> ejecutar opcion elegida
    paycanc:<propuesta_id>               -> cancelar
    """
    pid = int(parts[1])
    analisis = _PAGO_CACHE.get(pid)
    if not analisis:
        await cb.edit_message_text("⚠️ Propuesta de pago expirada. Vuelve a mandar el mensaje.")
        return

    if accion == "paycanc":
        _PAGO_CACHE.pop(pid, None)
        await cb.edit_message_text("❌ Pago cancelado, no se registró nada.")
        return

    # payopt: ejecutar la opcion elegida
    opt_idx = int(parts[2])
    opciones = analisis.get("opciones") or []
    if opt_idx >= len(opciones):
        await cb.edit_message_text("⚠️ Opción inválida")
        return
    opt = opciones[opt_idx]
    tipo = opt.get("tipo")

    factura_id = analisis["factura_id"]
    factura_name = analisis["factura"]
    cliente_ident = _get_cliente_ident_from_invoice(factura_id)
    if not cliente_ident:
        await cb.edit_message_text("⚠️ No pude resolver el cliente de la factura.")
        return

    metodo = analisis.get("metodo_pago", "efectivo")
    await cb.edit_message_text(
        f"⏳ Registrando pago en Siigo...",
        parse_mode="Markdown",
    )

    from skiimo.siigo_payments import (
        registrar_pago_completo, registrar_pago_con_pp, registrar_abono,
    )

    img_hash = analisis.get("_img_hash")  # si vino desde un comprobante OCR

    if tipo == "completo":
        result = await asyncio.to_thread(
            registrar_pago_completo,
            factura_id, factura_name, cliente_ident,
            opt["monto_recibo"], metodo,
        )
        await _show_pago_result(cb, result, factura_name, "Pago completo")
    elif tipo in ("pp", "pp_forzar"):
        result = await asyncio.to_thread(
            registrar_pago_con_pp,
            factura_id, factura_name, cliente_ident,
            opt["monto_recibo"], opt["monto_nc"], metodo, opt["descuento_pct"],
        )
        nota = "" if tipo == "pp" else " (forzado fuera de plazo)"
        await _show_pago_result(cb, result, factura_name, f"Pronto pago {opt['descuento_pct']:.0f}%{nota}")
    elif tipo == "abono":
        result = await asyncio.to_thread(
            registrar_abono,
            factura_id, factura_name, cliente_ident,
            opt["monto_recibo"], metodo,
        )
        saldo = opt.get("saldo_restante", 0)
        await _show_pago_result(cb, result, factura_name, f"Abono (saldo restante ${saldo:,.0f})")
    else:
        await cb.edit_message_text(f"⚠️ Tipo de opción no soportado: {tipo}")
        return

    # Si el pago vino de un comprobante OCR, marcarlo como aplicado en DB
    if img_hash and result.ok:
        _marcar_comprobante_aplicado(
            img_hash,
            factura_name=factura_name,
            rc_name=result.rc_name,
            nc_name=result.nc_name,
        )

    _PAGO_CACHE.pop(pid, None)


async def _handle_anulacion_callback(cb, ctx, parts: list[str]) -> None:
    """anul:<propuesta_id>:<metodo>  donde metodo = annul | credit_note | cancel"""
    pid = int(parts[1])
    metodo = parts[2]
    analisis = _PAGO_CACHE.get(pid)
    if not analisis:
        await cb.edit_message_text("⚠️ Propuesta expirada. Pide la anulacion de nuevo.")
        return

    factura_id = analisis["factura_id"]
    factura_name = analisis["factura_name"]
    motivo = analisis.get("motivo", "Anulacion solicitada")

    if metodo == "cancel":
        _PAGO_CACHE.pop(pid, None)
        await cb.edit_message_text(
            f"❌ Anulacion cancelada. La factura {factura_name} sigue activa.",
            parse_mode="Markdown",
        )
        return

    await cb.edit_message_text(
        f"⏳ Procesando anulacion de {factura_name}...",
        parse_mode="Markdown",
    )

    from skiimo.siigo_writer import anular_factura, crear_nota_credito_anulacion

    if metodo == "annul":
        result = await asyncio.to_thread(anular_factura, factura_id, actor=f"chat:admin")
        if result.ok:
            msg = (
                f"✅ *Factura {factura_name} anulada*\n\n"
                f"Metodo: `/annul` directo\n"
                f"_Motivo: {motivo}_"
            )
            await cb.edit_message_text(msg, parse_mode="Markdown")
        else:
            err = (result.error or "")[:300]
            # Si /annul fallo, sugerir nota credito
            buttons = [
                [InlineKeyboardButton("📋 Probar con nota credito", callback_data=f"anul:{pid}:credit_note")],
                [InlineKeyboardButton("❌ Cerrar", callback_data=f"anul:{pid}:cancel")],
            ]
            await cb.edit_message_text(
                f"⚠️ *No pude anular directamente*\n\n`{err}`\n\n"
                f"_Probable: factura ya tiene cobros o es electronica DIAN._",
                reply_markup=InlineKeyboardMarkup(buttons),
                parse_mode="Markdown",
            )
        return

    if metodo == "credit_note":
        result = await asyncio.to_thread(
            crear_nota_credito_anulacion, factura_id, motivo, "chat:admin",
        )
        if result.ok:
            msg = (
                f"✅ *Nota credito creada*\n\n"
                f"Factura original: {factura_name}\n"
                f"Nota credito: `{result.siigo_name}`\n"
                f"Valor: `${(result.total or 0):,.0f}`\n"
                f"Saldo neto factura: $0\n"
                f"_Motivo: {motivo}_"
            )
            await cb.edit_message_text(msg, parse_mode="Markdown")
            _PAGO_CACHE.pop(pid, None)
        else:
            err = (result.error or "")[:400]
            await cb.edit_message_text(
                f"⚠️ *Error al crear nota credito*\n\n`{err}`",
                parse_mode="Markdown",
            )
        return


async def _handle_pago_proveedor_callback(cb, ctx, accion: str, parts: list[str]) -> None:
    """Maneja callbacks de propuestas de pago A PROVEEDOR.

    prvopt:<propuesta_id>:<opcion_idx>  -> ejecutar opcion elegida
    prvcanc:<propuesta_id>               -> cancelar
    """
    pid = int(parts[1])
    analisis = _PAGO_CACHE.get(pid)
    if not analisis:
        await cb.edit_message_text("⚠️ Propuesta de pago a proveedor expirada.")
        return

    if accion == "prvcanc":
        _PAGO_CACHE.pop(pid, None)
        await cb.edit_message_text("❌ Pago a proveedor cancelado, no se registró nada.")
        return

    # prvopt: ejecutar opcion
    opt_idx = int(parts[2])
    opciones = analisis.get("opciones") or []
    if opt_idx >= len(opciones):
        await cb.edit_message_text("⚠️ Opción inválida")
        return
    opt = opciones[opt_idx]
    tipo = opt.get("tipo")

    compra_id = analisis["compra_id"]
    compra_name = analisis["compra"]
    proveedor_ident = _get_proveedor_ident_from_purchase(compra_id)
    if not proveedor_ident:
        await cb.edit_message_text("⚠️ No pude resolver el proveedor de la factura.")
        return

    metodo = analisis.get("metodo_pago", "banco_ahorros")
    await cb.edit_message_text(
        "⏳ Registrando pago en Siigo...",
        parse_mode="Markdown",
    )

    from skiimo.siigo_payments import registrar_pago_proveedor

    if tipo not in ("completo", "abono"):
        await cb.edit_message_text(f"⚠️ Tipo de opción no soportado: {tipo}")
        return

    result = await asyncio.to_thread(
        registrar_pago_proveedor,
        compra_id, compra_name, proveedor_ident,
        opt["monto_recibo"], metodo,
    )
    descripcion = "Pago completo" if tipo == "completo" else f"Abono (saldo restante ${opt.get('saldo_restante', 0):,.0f})"
    if result.ok:
        msg = f"✅ *Pago a proveedor registrado*\n\n"
        msg += f"Factura compra: {compra_name}\n"
        msg += f"Modo: _{descripcion}_\n"
        msg += f"Proveedor: {analisis.get('proveedor', '?')}\n"
        if result.rc_name:
            msg += f"\n🧾 Recibo de pago: `{result.rc_name}`"
        await cb.edit_message_text(msg, parse_mode="Markdown")
    else:
        await cb.edit_message_text(
            f"⚠️ *Error al registrar pago*\n\n`{result.error}`",
            parse_mode="Markdown",
        )

    _PAGO_CACHE.pop(pid, None)


def _get_proveedor_ident_from_purchase(compra_id: str) -> str | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT supplier_ident FROM siigo_purchases WHERE id = ?",
            (compra_id,),
        ).fetchone()
    finally:
        conn.close()
    return row["supplier_ident"] if row else None


def _get_cliente_ident_from_invoice(factura_id: str) -> str | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT customer_ident FROM siigo_invoices WHERE id = ?",
            (factura_id,),
        ).fetchone()
    finally:
        conn.close()
    return row["customer_ident"] if row else None


async def _handle_factura_correo_callback(cb, ctx, accion: str, parts: list[str]) -> None:
    """Maneja callbacks de facturas extraidas de correo.

    fcok:<factura_id>:<categoria>             -> seleccion de categoria
                                                 - DS: ejecuta directo
                                                 - FC: muestra paso intermedio elec/trad
    fcok:<factura_id>:<categoria>:<tipo>      -> ejecuta con tipo (elec|trad)
    fcno:<factura_id>                          -> descartar
    """
    from skiimo.gmail_imap import get_factura_correo, marcar_factura_correo
    from skiimo.siigo_writer import crear_factura_compra
    from skiimo.config import DEFAULT_PURCHASE_DOC_ID, PURCHASE_DOC_ID_MATERIAS

    factura_id = int(parts[1])
    fc = get_factura_correo(factura_id)
    if not fc:
        await cb.edit_message_text("⚠️ Factura no encontrada.")
        return

    if fc["estado"] != "pendiente":
        await cb.edit_message_text(f"⚠️ Esta factura ya está en estado: {fc['estado']}")
        return

    if accion == "fcno":
        marcar_factura_correo(factura_id, "descartada")
        await cb.edit_message_text(
            f"❌ *Factura #{factura_id} descartada*\n\n"
            f"_Proveedor: {fc.get('proveedor_nombre') or '?'}_",
            parse_mode="Markdown",
        )
        return

    if accion == "fcok":
        categoria = parts[2] if len(parts) > 2 else "gasto_administrativo"
        tipo_factura = parts[3] if len(parts) > 3 else None

        cat_labels = {
            "materias_primas": "MATERIAS PRIMAS",
            "gasto_administrativo": "GASTO ADMINISTRATIVO",
            "documento_soporte": "DOCUMENTO SOPORTE",
        }
        cat_label = cat_labels.get(categoria, categoria)

        # Paso intermedio: si es FC (no DS) y no eligio tipo aun, preguntar elec/trad
        if categoria != "documento_soporte" and tipo_factura is None:
            await cb.edit_message_text(
                f"📨 *Factura #{factura_id}* — {cat_label}\n\n"
                f"*Proveedor:* {fc.get('proveedor_nombre') or '?'}\n"
                f"*NIT:* `{fc.get('proveedor_nit') or '?'}`\n"
                f"*Total:* `${float(fc.get('total') or 0):,.0f}`\n\n"
                f"¿El proveedor te emitió factura electrónica DIAN o tradicional?",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📧 Electrónica DIAN", callback_data=f"fcok:{factura_id}:{categoria}:elec")],
                    [InlineKeyboardButton("🧾 Tradicional", callback_data=f"fcok:{factura_id}:{categoria}:trad")],
                    [InlineKeyboardButton("← Cambiar categoría", callback_data=f"fcback:{factura_id}")],
                    [InlineKeyboardButton("❌ Descartar", callback_data=f"fcno:{factura_id}")],
                ]),
                parse_mode="Markdown",
            )
            return

        # tipo default si es DS
        if tipo_factura is None:
            tipo_factura = "elec"
        tipo_label = "📧 Electrónica DIAN" if tipo_factura == "elec" else "🧾 Tradicional"

        await cb.edit_message_text(
            f"⏳ Creando {cat_label} ({tipo_label}) en Siigo (#{factura_id})...",
            parse_mode="Markdown",
        )

        payload = json.loads(fc["payload_extraido"] or "{}")
        payload["categoria"] = categoria
        payload["origen_obs"] = "[CORREO]"

        # Routing al endpoint correcto segun categoria
        if categoria == "documento_soporte":
            from skiimo.siigo_writer import crear_documento_soporte
            result = await asyncio.to_thread(
                crear_documento_soporte, payload, actor=f"correo:{factura_id}",
            )
        else:
            doc_id = PURCHASE_DOC_ID_MATERIAS if categoria == "materias_primas" else DEFAULT_PURCHASE_DOC_ID
            result = await asyncio.to_thread(
                crear_factura_compra, payload, doc_id=doc_id,
                actor=f"correo:{factura_id}", tipo_factura=tipo_factura,
            )

        if result.ok:
            marcar_factura_correo(
                factura_id, "enviada",
                siigo_purchase_id=result.siigo_id,
                siigo_purchase_name=result.siigo_name,
            )
            msg = (
                f"✅ *Documento creado en Siigo*\n\n"
                f"Documento: `{result.siigo_name}`\n"
                f"Tipo: _{cat_label}_\n"
                f"Total: `${(result.total or 0):,.0f}`\n"
                f"Proveedor: {fc.get('proveedor_nombre') or '?'}"
            )
            await cb.edit_message_text(msg, parse_mode="Markdown")
        else:
            marcar_factura_correo(factura_id, "error", error=(result.error or "")[:500])
            await cb.edit_message_text(
                f"⚠️ *Error al crear*\n\n`{(result.error or '')[:400]}`",
                parse_mode="Markdown",
            )


# FSM per-chat para edicion de item: {"factura_id":..., "idx":..., "campo": "qty"|"price"}
_EDIT_ITEM_POR_CHAT: dict[int, dict] = {}


def _factura_correo_actualizar_payload(factura_id: int, payload: dict) -> None:
    """Persiste cambios al payload_extraido + total de una factura_correo."""
    total = sum(
        float(it.get("cantidad") or 0) * float(it.get("precio_unitario") or 0)
        for it in payload.get("items") or []
    )
    payload["total"] = round(total, 2)
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE facturas_correo SET payload_extraido = ?, total = ?, updated_at = ? "
            "WHERE id = ?",
            (
                json.dumps(payload, ensure_ascii=False, default=str),
                payload["total"],
                datetime.now().isoformat(timespec="seconds"),
                factura_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def _handle_edit_item_callback(cb, ctx, accion: str, parts: list[str]) -> None:
    """Maneja los botones de edicion de items de factura.

    iedi:<factura_id>:<idx>    submenu del item N
    ieqt:<factura_id>:<idx>    pedir nueva cantidad por texto
    iepr:<factura_id>:<idx>    pedir nuevo precio por texto
    iedl:<factura_id>:<idx>    eliminar item N
    """
    from skiimo.gmail_imap import get_factura_correo
    factura_id = int(parts[1])
    idx = int(parts[2])
    chat_id = cb.message.chat.id if cb.message else None
    fc = get_factura_correo(factura_id)
    if not fc:
        await cb.edit_message_text("⚠️ Factura no encontrada.")
        return
    payload = json.loads(fc["payload_extraido"] or "{}")
    items = payload.get("items") or []
    if idx < 0 or idx >= len(items):
        await cb.edit_message_text("⚠️ Item ya no existe.")
        return
    item = items[idx]
    qty = float(item.get("cantidad") or 0)
    pre = float(item.get("precio_unitario") or 0)
    sub = qty * pre
    desc = (item.get("descripcion") or "?")[:60]
    qty_str = f"{int(qty)}" if qty == int(qty) else f"{qty:g}"

    if accion == "iedi":
        await cb.edit_message_text(
            f"✏️ *Editar item {idx+1}*\n\n"
            f"_{desc}_\n"
            f"`{qty_str} x ${pre:,.0f} = ${sub:,.0f}`\n\n"
            f"¿Qué cambias?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔢 Cambiar cantidad", callback_data=f"ieqt:{factura_id}:{idx}")],
                [InlineKeyboardButton("💵 Cambiar precio", callback_data=f"iepr:{factura_id}:{idx}")],
                [InlineKeyboardButton("🗑 Eliminar item", callback_data=f"iedl:{factura_id}:{idx}")],
                [InlineKeyboardButton("← Volver", callback_data=f"fcback:{factura_id}")],
            ]),
            parse_mode="Markdown",
        )
        return

    if accion == "ieqt":
        _EDIT_ITEM_POR_CHAT[chat_id] = {"factura_id": factura_id, "idx": idx, "campo": "qty"}
        await cb.edit_message_text(
            f"🔢 *Nueva cantidad para item {idx+1}*\n\n"
            f"_{desc}_\n"
            f"Actual: `{qty_str}`\n\n"
            f"Mandame el número nuevo. /cancelar para salir.",
            parse_mode="Markdown",
        )
        return

    if accion == "iepr":
        _EDIT_ITEM_POR_CHAT[chat_id] = {"factura_id": factura_id, "idx": idx, "campo": "price"}
        await cb.edit_message_text(
            f"💵 *Nuevo precio unitario para item {idx+1}*\n\n"
            f"_{desc}_\n"
            f"Actual: `${pre:,.0f}`\n\n"
            f"Mandame el número nuevo (sin signo $). /cancelar para salir.",
            parse_mode="Markdown",
        )
        return

    if accion == "iedl":
        # Eliminar item del payload
        items.pop(idx)
        payload["items"] = items
        _factura_correo_actualizar_payload(factura_id, payload)
        await cb.edit_message_text(
            f"🗑 Item eliminado.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("← Volver al resumen", callback_data=f"fcback:{factura_id}")],
            ]),
        )
        return


async def _continuar_edit_item(update: Update, ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, texto: str) -> None:
    """Recibe el numero del usuario para actualizar cantidad/precio del item en edicion."""
    estado = _EDIT_ITEM_POR_CHAT.get(chat_id)
    if not estado:
        return
    if texto.lower() in ("/cancelar", "cancelar"):
        _EDIT_ITEM_POR_CHAT.pop(chat_id, None)
        await update.message.reply_text("❌ Edición cancelada.")
        return

    try:
        limpio = "".join(c for c in texto if c.isdigit() or c in ".,").replace(",", ".")
        valor = float(limpio)
        if valor <= 0:
            raise ValueError("debe ser > 0")
    except (ValueError, TypeError):
        await update.message.reply_text(
            "⚠️ Mandame solo el número. /cancelar para salir.",
            parse_mode="Markdown",
        )
        return

    from skiimo.gmail_imap import get_factura_correo
    factura_id = estado["factura_id"]
    idx = estado["idx"]
    campo = estado["campo"]
    fc = get_factura_correo(factura_id)
    if not fc:
        _EDIT_ITEM_POR_CHAT.pop(chat_id, None)
        await update.message.reply_text("⚠️ Factura ya no existe.")
        return
    payload = json.loads(fc["payload_extraido"] or "{}")
    items = payload.get("items") or []
    if idx >= len(items):
        _EDIT_ITEM_POR_CHAT.pop(chat_id, None)
        await update.message.reply_text("⚠️ Item ya no existe.")
        return

    if campo == "qty":
        items[idx]["cantidad"] = valor
    elif campo == "price":
        items[idx]["precio_unitario"] = valor
    payload["items"] = items
    _factura_correo_actualizar_payload(factura_id, payload)
    _EDIT_ITEM_POR_CHAT.pop(chat_id, None)

    nuevo_total = payload["total"]
    await update.message.reply_text(
        f"✅ Item {idx+1} actualizado.\n"
        f"Total nuevo: `${nuevo_total:,.0f}`",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("← Volver al resumen", callback_data=f"fcback:{factura_id}")],
        ]),
        parse_mode="Markdown",
    )


async def _handle_gasto_manual_callback(cb, ctx, accion: str, parts: list[str]) -> None:
    """Maneja los botones del FSM /gastomanual: seleccion de pago + confirmacion."""
    chat_id = cb.message.chat.id if cb.message else None
    estado = _GASTO_MANUAL_POR_CHAT.get(chat_id) if chat_id else None
    if not estado:
        await cb.edit_message_text("⚠️ Sesión expirada. Mandá /gastomanual de nuevo.")
        return

    if accion == "gmcanc":
        _GASTO_MANUAL_POR_CHAT.pop(chat_id, None)
        await cb.edit_message_text("❌ Gasto manual cancelado.")
        return

    if accion == "gmback":
        estado["step"] = "pago"
        estado.pop("payment_mode", None)
        estado.pop("payment_method", None)
        estado.pop("metodo_label", None)
        await cb.edit_message_text(
            f"📝 _{estado['desc']}_\n\n¿De qué caja salió el dinero?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💵 Efectivo", callback_data="gmpay:efectivo")],
                [InlineKeyboardButton("📱 Nequi", callback_data="gmpay:nequi")],
                [InlineKeyboardButton("📱 Daviplata", callback_data="gmpay:daviplata")],
                [InlineKeyboardButton("🏦 Banco Ahorros", callback_data="gmpay:banco_ahorros")],
                [InlineKeyboardButton("⏳ Quedó pendiente (crédito proveedores)", callback_data="gmpay:credito")],
                [InlineKeyboardButton("❌ Cancelar", callback_data="gmcanc")],
            ]),
            parse_mode="Markdown",
        )
        return

    if accion == "gmpay":
        metodo = parts[1] if len(parts) > 1 else "efectivo"
        if metodo == "credito":
            estado["payment_mode"] = "credito"
            estado["payment_method"] = "efectivo"  # placeholder
            metodo_label = "⏳ Quedó pendiente (Crédito proveedores 30 días)"
        else:
            estado["payment_mode"] = "contado"
            estado["payment_method"] = metodo
            metodo_label = {
                "efectivo": "💵 Efectivo",
                "nequi": "📱 Nequi",
                "daviplata": "📱 Daviplata",
                "banco_ahorros": "🏦 Banco Ahorros",
            }.get(metodo, metodo)
        estado["step"] = "confirm"
        estado["metodo_label"] = metodo_label
        await cb.edit_message_text(
            f"📋 *Confirmar gasto manual (DS)*\n\n"
            f"💰 Monto: `${estado['monto']:,.0f}`\n"
            f"👤 Proveedor: *{estado['nombre']}*\n"
            f"   NIT: `{estado['nit']}`\n"
            f"📝 Descripción: _{estado['desc']}_\n"
            f"💳 Pago: {metodo_label}\n",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Crear DS en Siigo", callback_data="gmok")],
                [InlineKeyboardButton("← Cambiar caja", callback_data="gmback")],
                [InlineKeyboardButton("❌ Cancelar", callback_data="gmcanc")],
            ]),
            parse_mode="Markdown",
        )
        return

    if accion == "gmok":
        await cb.edit_message_text(
            f"⏳ Creando DS en Siigo por `${estado['monto']:,.0f}`...",
            parse_mode="Markdown",
        )
        from skiimo.siigo_writer import crear_gasto_manual_ds
        result = await asyncio.to_thread(
            crear_gasto_manual_ds,
            monto=estado["monto"],
            descripcion=estado["desc"],
            proveedor_nit=estado["nit"],
            proveedor_nombre=estado["nombre"],
            payment_mode=estado.get("payment_mode", "contado"),
            payment_method=estado.get("payment_method", "efectivo"),
            actor=f"chat:{chat_id}",
        )
        metodo_label = estado.get("metodo_label", "Efectivo")
        _GASTO_MANUAL_POR_CHAT.pop(chat_id, None)
        if result.ok:
            await cb.edit_message_text(
                f"✅ *DS creado*\n\n"
                f"Documento: `{result.siigo_name or '?'}`\n"
                f"Proveedor: *{estado['nombre']}*\n"
                f"Monto: `${(result.total or estado['monto']):,.0f}`\n"
                f"Pago: {metodo_label}\n"
                f"Descripción: _{estado['desc']}_",
                parse_mode="Markdown",
            )
        else:
            await cb.edit_message_text(
                f"⚠️ *Error al crear DS*\n\n`{(result.error or '')[:400]}`",
                parse_mode="Markdown",
            )


async def _handle_iva_confirm(cb, ctx, parts: list[str]) -> None:
    """Maneja la respuesta al 'son con IVA o sin IVA'.

    ivac:<pedido_id>:siiva  -> dividir precios manuales por 1.19
    ivac:<pedido_id>:noiva  -> mantener precios tal cual
    """
    pedido_id = int(parts[1])
    decision = parts[2] if len(parts) > 2 else "noiva"
    pedido_row = _load_pedido(pedido_id)
    if not pedido_row:
        await cb.edit_message_text("⚠️ Pedido no encontrado.")
        return

    payload = json.loads(pedido_row["payload_extraido"] or "{}")
    items = payload.get("items") or []

    # Aplicar conversión a los items con precio manual
    if decision == "siiva":
        for it in items:
            raw = it.get("raw") or {}
            if raw.get("precio_unitario") is not None and raw.get("precio_unitario") > 0:
                # Precio del vendedor es CON IVA. Dividimos para guardar como SIN IVA.
                # (asumimos IVA 19% que es el de granizados)
                precio_con_iva = float(raw["precio_unitario"])
                precio_sin_iva = round(precio_con_iva / 1.19, 2)
                raw["precio_unitario"] = precio_sin_iva
                it["raw"] = raw
                it["precio_unitario"] = precio_sin_iva
    # Si decision == 'noiva', dejamos tal cual (el precio ya es sin IVA)

    # Guardar payload actualizado
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE bot_pedidos SET payload_extraido = ?, updated_at = ? WHERE id = ?",
            (
                json.dumps(payload, ensure_ascii=False, default=str),
                datetime.now().isoformat(timespec="seconds"),
                pedido_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    # Re-rehidratar y mostrar resumen normal
    pedido_row_nuevo = _load_pedido(pedido_id)
    rp = _rehydrate_resolved(pedido_row_nuevo)
    label = "✅ Precios CON IVA registrados" if decision == "siiva" else "🚫 Precios SIN IVA registrados"
    await cb.edit_message_text(
        f"{label}\n\n*Pedido #{pedido_id}*\n\n{format_summary(rp)}",
        reply_markup=InlineKeyboardMarkup(_build_pedido_buttons(pedido_id, rp)),
        parse_mode="Markdown",
    )


async def _handle_newcli_iniciar(cb, ctx, parts: list[str]) -> None:
    """Inicia el FSM de creacion de cliente desde el boton 'Crear cliente nuevo' del pedido."""
    pedido_id = int(parts[1])
    chat_id = cb.message.chat.id if cb.message else None
    pedido_row = _load_pedido(pedido_id)
    if not pedido_row:
        await cb.edit_message_text("⚠️ Pedido no encontrado.")
        return
    payload = json.loads(pedido_row["payload_extraido"] or "{}")
    nombre_sugerido = (payload.get("raw", {}) or {}).get("cliente_nombre") or ""
    await _iniciar_cliente_nuevo(cb, ctx, chat_id, pedido_id, nombre_sugerido)


async def _handle_newcli_flow(cb, ctx, accion: str, parts: list[str]) -> None:
    """Maneja los pasos del FSM cliente nuevo:
    newclitipo:<empresa|persona>  -> registra tipo, muestra categorias
    newclicat:<DETAL|MAYORISTA|DISTRIBUIDOR> -> registra categoria, muestra confirm
    newcliok                       -> ejecuta creacion en Siigo
    newclicanc                     -> cancela
    """
    chat_id = cb.message.chat.id if cb.message else None
    estado = _CLIENTE_NUEVO_POR_CHAT.get(chat_id) if chat_id else None
    if not estado:
        await cb.edit_message_text("⚠️ Sesión expirada. Volvé al pedido y mandá 'Crear cliente nuevo' de nuevo.")
        return

    if accion == "newclicanc":
        _CLIENTE_NUEVO_POR_CHAT.pop(chat_id, None)
        await cb.edit_message_text("❌ Creación de cliente cancelada.")
        return

    if accion == "newclitipo":
        tipo = parts[1] if len(parts) > 1 else "empresa"
        if tipo not in ("empresa", "persona"):
            tipo = "empresa"
        estado["tipo"] = tipo
        estado["step"] = "categoria"
        tipo_label = "🏢 Empresa" if tipo == "empresa" else "👤 Persona natural"
        await cb.edit_message_text(
            f"📋 *Nuevo cliente*\n\n"
            f"Nombre: {estado['nombre']}\n"
            f"NIT/Cédula: `{estado['nit']}`\n"
            f"Tipo: {tipo_label}\n\n"
            f"¿Qué categoría comercial le asigno? (afecta los precios)",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛒 DETAL (precio lista)", callback_data="newclicat:DETAL")],
                [InlineKeyboardButton("📦 MAYORISTA (~10% off)", callback_data="newclicat:MAYORISTA")],
                [InlineKeyboardButton("🏭 DISTRIBUIDOR (~15-20% off)", callback_data="newclicat:DISTRIBUIDOR")],
                [InlineKeyboardButton("❌ Cancelar", callback_data="newclicanc")],
            ]),
            parse_mode="Markdown",
        )
        return

    if accion == "newclicat":
        cat = parts[1] if len(parts) > 1 else "DETAL"
        if cat not in ("DETAL", "MAYORISTA", "DISTRIBUIDOR"):
            cat = "DETAL"
        estado["categoria"] = cat
        estado["step"] = "confirm"
        tipo_label = "🏢 Empresa" if estado.get("tipo") == "empresa" else "👤 Persona natural"
        await cb.edit_message_text(
            f"📋 *Confirmar nuevo cliente*\n\n"
            f"Nombre: *{estado['nombre']}*\n"
            f"NIT/Cédula: `{estado['nit']}`\n"
            f"Tipo: {tipo_label}\n"
            f"Categoría: *{cat}*\n",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Crear en Siigo", callback_data="newcliok")],
                [InlineKeyboardButton("← Cambiar categoría", callback_data=f"newclitipo:{estado.get('tipo', 'empresa')}")],
                [InlineKeyboardButton("❌ Cancelar", callback_data="newclicanc")],
            ]),
            parse_mode="Markdown",
        )
        return

    if accion == "newcliok":
        pedido_id = estado["pedido_id"]
        await cb.edit_message_text(
            f"⏳ Creando *{estado['nombre']}* en Siigo...",
            parse_mode="Markdown",
        )
        from skiimo.siigo_writer import crear_cliente
        result = await asyncio.to_thread(
            crear_cliente,
            nit=estado["nit"], nombre=estado["nombre"],
            tipo=estado.get("tipo", "empresa"),
            categoria=estado.get("categoria", "DETAL"),
            actor=f"chat:{chat_id}",
        )
        if not result.ok:
            await cb.edit_message_text(
                f"⚠️ *Error al crear cliente*\n\n`{(result.error or '')[:400]}`",
                parse_mode="Markdown",
            )
            _CLIENTE_NUEVO_POR_CHAT.pop(chat_id, None)
            return

        # Recargar el matcher para que tenga el cliente nuevo
        try:
            _get_matcher().reload()
        except Exception:
            pass

        # Asignar el nuevo cliente al pedido (update payload_extraido)
        pedido_row = _load_pedido(pedido_id)
        if pedido_row:
            payload = json.loads(pedido_row["payload_extraido"] or "{}")
            payload["cliente"] = {
                "id": result.siigo_id,
                "identification": result.identification,
                "name": result.name,
                "commercial_name": result.name,
                "email": "",
                "score": 100.0,
            }
            payload["cliente_candidatos"] = []
            # Quitar problemas de cliente
            pend = [p for p in payload.get("necesita_input_humano", [])
                    if "cliente" not in p.lower() and "NIT" not in p]
            payload["necesita_input_humano"] = pend
            conn = get_conn()
            try:
                conn.execute(
                    "UPDATE bot_pedidos SET payload_extraido = ?, customer_id = ?, updated_at = ? WHERE id = ?",
                    (
                        json.dumps(payload, ensure_ascii=False, default=str),
                        result.siigo_id,
                        datetime.now().isoformat(timespec="seconds"),
                        pedido_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

        _CLIENTE_NUEVO_POR_CHAT.pop(chat_id, None)

        await cb.edit_message_text(
            f"✅ *Cliente creado*: {result.name}\n"
            f"NIT: `{result.identification}` · Categoría: *{estado.get('categoria', 'DETAL')}*\n\n"
            f"_Continuando con el pedido..._",
            parse_mode="Markdown",
        )

        # Re-mostrar el pedido con el cliente nuevo asignado
        try:
            pedido_row_nuevo = _load_pedido(pedido_id)
            rp = _rehydrate_resolved(pedido_row_nuevo)
            await ctx.bot.send_message(
                chat_id=chat_id,
                text=f"*Pedido #{pedido_id}*\n\n{format_summary(rp)}",
                reply_markup=InlineKeyboardMarkup(_build_pedido_buttons(pedido_id, rp)),
                parse_mode="Markdown",
            )
        except Exception:
            log.exception("Error re-mostrando pedido despues de crear cliente")
        return


async def _handle_proveedor_callback(cb, ctx, accion: str, parts: list[str]) -> None:
    """Maneja la creacion de un proveedor desconocido al subir factura.

    prvcrear:<factura_id>:<tipo>  -> crear empresa/persona y seguir al flujo de aprobacion
    prvedit:<factura_id>          -> pedir nombre por chat
    """
    from skiimo.gmail_imap import get_factura_correo
    factura_id = int(parts[1])
    chat_id = cb.message.chat.id if cb.message else None
    pend = _PROVEEDOR_PENDIENTE_POR_CHAT.get(chat_id) if chat_id else None

    if accion == "prvedit":
        if pend is None:
            await cb.edit_message_text("⚠️ Sesión expirada. Volvé a mandar la factura.")
            return
        pend["esperando_nombre"] = True
        await cb.edit_message_text(
            f"✏️ *Escribí el nombre correcto del proveedor*\n\n"
            f"NIT/Cedula: `{pend['nit']}`\n"
            f"_Mandalo como mensaje de texto._",
            parse_mode="Markdown",
        )
        return

    if accion == "prvcrear":
        tipo = parts[2] if len(parts) > 2 else "empresa"
        if pend is None:
            # Fallback: leer NIT/nombre de la factura
            fc = get_factura_correo(factura_id)
            if not fc:
                await cb.edit_message_text("⚠️ Factura no encontrada.")
                return
            pend = {
                "factura_id": factura_id,
                "nit": fc.get("proveedor_nit") or "",
                "nombre": fc.get("proveedor_nombre") or "",
            }
        nit = pend["nit"]
        nombre = pend["nombre"]
        if not nombre:
            await cb.edit_message_text(
                "⚠️ Falta el nombre. Tocá *Cambiar nombre* y mandámelo.",
                parse_mode="Markdown",
            )
            return

        tipo_label = "🏢 Empresa" if tipo == "empresa" else "👤 Persona"
        await cb.edit_message_text(
            f"⏳ Creando proveedor en Siigo ({tipo_label})...\n\n"
            f"NIT: `{nit}`  ·  Nombre: _{nombre[:60]}_",
            parse_mode="Markdown",
        )

        from skiimo.siigo_writer import crear_proveedor
        result = await asyncio.to_thread(
            crear_proveedor,
            nit=nit, nombre=nombre, tipo=tipo,
            actor=f"chat:{chat_id}",
        )

        if not result.ok:
            await cb.edit_message_text(
                f"⚠️ *No se pudo crear el proveedor*\n\n`{(result.error or '')[:400]}`",
                parse_mode="Markdown",
            )
            return

        # Limpiar pending
        if chat_id:
            _PROVEEDOR_PENDIENTE_POR_CHAT.pop(chat_id, None)

        await cb.edit_message_text(
            f"✅ *Proveedor creado en Siigo*\n\n"
            f"NIT: `{nit}`\n"
            f"Nombre: _{nombre[:80]}_\n"
            f"Tipo: {tipo_label}\n\n"
            f"_Continuando con la factura..._",
            parse_mode="Markdown",
        )
        # Mostrar la factura para seguir el flujo normal
        await _mostrar_factura_correo(cb, ctx, factura_id)
        return


async def _handle_comprobante_callback(cb, ctx, accion: str, parts: list[str]) -> None:
    """Maneja callbacks de comprobantes OCR.

    cpapply:<comp_id>:<factura_id>  -> aplicar pago a esa factura
    cpsearch:<comp_id>               -> pedir al usuario nombre del cliente
    cpcancel:<comp_id>               -> descartar
    """
    cid = int(parts[1])
    comp = _COMPROBANTE_CACHE.get(cid)
    if not comp:
        await cb.edit_message_text("⚠️ Comprobante expirado. Volvé a enviar la foto.")
        return

    if accion == "cpcancel":
        if comp.get("img_hash"):
            _marcar_comprobante_descartado(comp["img_hash"])
        _COMPROBANTE_CACHE.pop(cid, None)
        await cb.edit_message_text("❌ Comprobante descartado.")
        return

    if accion == "cpsearch":
        # Marcar este comprobante como "esperando cliente" para que el proximo mensaje de
        # texto del mismo chat se interprete como busqueda de cliente
        comp["esperando_cliente"] = True
        comp["chat_id"] = comp.get("chat_id")
        _ESPERANDO_CLIENTE_POR_CHAT[comp["chat_id"]] = cid
        await cb.edit_message_text(
            f"💰 *Monto detectado:* `${comp['monto']:,.0f}` ({comp['metodo_detectado']})\n\n"
            "Escribime el *nombre* o *NIT* del cliente, o el número de factura (ej: FV-1-5074).",
            parse_mode="Markdown",
        )
        return

    if accion == "cpapply":
        factura_id = parts[2]
        # Buscar la factura
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT name FROM siigo_invoices WHERE id = ?",
                (factura_id,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            await cb.edit_message_text("⚠️ Factura no encontrada en el espejo local.")
            return
        factura_name = row["name"]

        # Lanzar analisis y mostrar opciones (reusa flujo de pagos existente)
        from skiimo.siigo_payments import analizar_pago
        await cb.edit_message_text(
            f"⏳ Analizando pago de `${comp['monto']:,.0f}` sobre {factura_name}...",
            parse_mode="Markdown",
        )
        a = await asyncio.to_thread(analizar_pago, factura_name, comp["monto"])
        if a is None:
            await cb.edit_message_text(f"⚠️ No pude analizar la factura {factura_name}")
            return

        # Convertir a dict (mismo formato que la tool del agente)
        analisis = {
            "pendiente_confirmacion": True,
            "factura": a.factura_name,
            "factura_id": a.factura_id,
            "cliente": a.cliente_nombre,
            "total_factura": a.factura_total,
            "saldo_actual": a.factura_balance,
            "monto_pagado": a.monto_pagado,
            "diferencia": round(a.diferencia, 2),
            "diferencia_pct": round(a.diferencia_pct, 1),
            "fecha_factura": a.fecha_factura.isoformat(),
            "fecha_pago": a.fecha_pago.isoformat(),
            "dias_transcurridos": a.dias_transcurridos,
            "metodo_pago": comp["metodo"],
            "opciones": a.opciones,
            "_img_hash": comp.get("img_hash"),
        }
        # Reusamos el flow de _send_pago_proposal pero editando el mensaje existente
        propuesta_id = _save_pago_propuesta(analisis)
        texto = (
            f"📥 *Aplicar pago a {factura_name}*\n\n"
            f"Cliente: {a.cliente_nombre}\n"
            f"Saldo factura: `${a.factura_balance:,.0f}`\n"
            f"Pago recibido: `${a.monto_pagado:,.0f}` ({comp['metodo_detectado']})\n"
            f"Días: {a.dias_transcurridos}\n\n"
            f"_¿Cómo lo registro?_"
        )
        buttons: list[list[InlineKeyboardButton]] = []
        for i, opt in enumerate(a.opciones):
            tipo = opt.get("tipo")
            label = opt.get("label", "?")
            if tipo == "error":
                continue
            emoji = {"completo": "✅", "pp": "🎯", "pp_forzar": "⚠️", "abono": "💵"}.get(tipo, "•")
            buttons.append([InlineKeyboardButton(
                f"{emoji}  {label}",
                callback_data=f"payopt:{propuesta_id}:{i}",
            )])
        buttons.append([InlineKeyboardButton("❌  Cancelar", callback_data=f"paycanc:{propuesta_id}")])

        await _edit_md_safe(
            cb,
            texto,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        # liberar comprobante (ya se delegó al flujo de pagos)
        _COMPROBANTE_CACHE.pop(cid, None)


async def _show_pago_result(cb, result, factura_name: str, descripcion: str) -> None:
    if result.ok:
        msg = f"✅ *Pago registrado en Siigo*\n\n"
        msg += f"Factura: {factura_name}\n"
        msg += f"Modo: _{descripcion}_\n"
        if result.rc_name:
            msg += f"\n🧾 Recibo: `{result.rc_name}`"
        if result.nc_name:
            msg += f"\n📋 Nota crédito: `{result.nc_name}`"
        await cb.edit_message_text(msg, parse_mode="Markdown")
    else:
        await cb.edit_message_text(
            f"⚠️ *Error al registrar pago*\n\n`{result.error}`",
            parse_mode="Markdown",
        )


async def _send_proposal(update: Update, ctx: ContextTypes.DEFAULT_TYPE, pedido) -> None:
    assert update.message and update.effective_chat
    # Si Gemini extrajo un mensaje sin items, asumimos que no era pedido
    if not pedido.items:
        await update.message.reply_text(
            "No entendi un pedido aca. Si era un mensaje normal, ignoralo.\n"
            "Si querias hacer un pedido, intenta:\n"
            "  'Para [cliente]: cantidad producto, cantidad producto...'"
        )
        return

    rp = resolve_pedido(pedido, _get_matcher())
    pedido_id = _save_pedido(update.effective_chat.id, update.message.message_id, rp)

    # Detectar si hay precios manuales para preguntar si son con/sin IVA
    items_manuales = [
        (i, it) for i, it in enumerate(pedido.items)
        if it.precio_unitario is not None and it.precio_unitario > 0
    ]
    if items_manuales:
        # Mostrar lista de precios y preguntar
        lineas = ["💵 *Detecté precios manuales*\n"]
        for idx, it in items_manuales:
            desc = it.descripcion[:30]
            lineas.append(f"  • {desc}: `${it.precio_unitario:,.0f}` c/u")
        lineas.append("")
        lineas.append("¿Esos precios *incluyen IVA* o son *sin IVA*?")
        await update.message.reply_text(
            "\n".join(lineas),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "✅ Con IVA (lo que paga el cliente)",
                    callback_data=f"ivac:{pedido_id}:siiva",
                )],
                [InlineKeyboardButton(
                    "🚫 Sin IVA (más IVA después)",
                    callback_data=f"ivac:{pedido_id}:noiva",
                )],
                [InlineKeyboardButton("❌ Cancelar", callback_data=f"canc:{pedido_id}")],
            ]),
            parse_mode="Markdown",
        )
        return

    resumen = format_summary(rp)
    buttons = _build_pedido_buttons(pedido_id, rp)

    await update.message.reply_text(
        f"*Pedido #{pedido_id}*\n\n{resumen}",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )


def _build_pedido_buttons(pedido_id: int, rp) -> list[list[InlineKeyboardButton]]:
    """Construye el teclado inline para un pedido.

    Paso 1: el vendedor elige el tipo de documento.

    Layout:
      [ 📧 Factura electronica ]
      [ 🧾 Factura ]
      [ ✏️ Cambiar producto 1 ]  [ ✏️ Cambiar producto 2 ]
      [ ❌ Cancelar pedido ]
    """
    bloqueado = bool(rp.necesita_input_humano) or rp.cliente_elegido is None
    buttons: list[list[InlineKeyboardButton]] = []

    # Paso 1: tipo de documento
    if not bloqueado:
        buttons.append([InlineKeyboardButton(
            "📧  Factura electronica",
            callback_data=f"dtyp:{pedido_id}:elec",
        )])
        buttons.append([InlineKeyboardButton(
            "🧾  Factura",
            callback_data=f"dtyp:{pedido_id}:trad",
        )])

    # Botones para cambiar items (compactos, 2 por fila si caben)
    items_editables = [
        (idx, item) for idx, item in enumerate(rp.items)
        if item.elegido and item.candidatos and len(item.candidatos) > 1
    ]
    if items_editables:
        # Un boton "Cambiar producto N" por cada item editable
        # En filas de 2 si hay varios
        row: list[InlineKeyboardButton] = []
        for idx, _item in items_editables:
            row.append(InlineKeyboardButton(
                f"✏️  Cambiar producto {idx+1}",
                callback_data=f"editi:{pedido_id}:{idx}",
            ))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

    # Cliente alternativo (solo si no se eligio automatico)
    if rp.cliente_candidatos and not rp.cliente_elegido:
        for c in rp.cliente_candidatos[:3]:
            buttons.append([InlineKeyboardButton(
                f"👤  {c.name[:35]}",
                callback_data=f"pickc:{pedido_id}:{c.id}",
            )])
        # Si el cliente real no aparece en candidatos, ofrecer crear nuevo
        buttons.append([InlineKeyboardButton(
            "🆕  Crear cliente nuevo",
            callback_data=f"newcli:{pedido_id}",
        )])

    # Cancelar siempre al final
    buttons.append([InlineKeyboardButton(
        "❌  Cancelar pedido",
        callback_data=f"canc:{pedido_id}",
    )])

    return buttons


def _doc_type_label(dtype: str) -> str:
    return "Factura electronica" if dtype == "elec" else "Factura"


def _build_payment_step_buttons(pedido_id: int, dtype: str) -> list[list[InlineKeyboardButton]]:
    """Paso 2: una vez elegido tipo de doc, pedir credito/pagada."""
    return [
        [InlineKeyboardButton(
            "📅  Enviar como CRÉDITO",
            callback_data=f"sendcr:{pedido_id}:{dtype}",
        )],
        [InlineKeyboardButton(
            "💰  Enviar como PAGADA",
            callback_data=f"sendpag:{pedido_id}:{dtype}",
        )],
        [InlineKeyboardButton(
            "← Cambiar tipo de factura",
            callback_data=f"back:{pedido_id}",
        )],
    ]


def _build_payment_method_picker(pedido_id: int, dtype: str) -> list[list[InlineKeyboardButton]]:
    """Sub-menu cuando el usuario elige 'Enviar como PAGADA': elegir metodo de pago."""
    return [
        [InlineKeyboardButton("💵  Efectivo", callback_data=f"pay:{pedido_id}:{dtype}:efectivo")],
        [InlineKeyboardButton("📱  Nequi", callback_data=f"pay:{pedido_id}:{dtype}:nequi")],
        [InlineKeyboardButton("📱  Daviplata", callback_data=f"pay:{pedido_id}:{dtype}:daviplata")],
        [InlineKeyboardButton("🏦  Transferencia bancaria", callback_data=f"pay:{pedido_id}:{dtype}:banco_ahorros")],
        [InlineKeyboardButton("💳  Tarjeta débito", callback_data=f"pay:{pedido_id}:{dtype}:tarjeta_debito")],
        [InlineKeyboardButton("💳  Tarjeta crédito", callback_data=f"pay:{pedido_id}:{dtype}:tarjeta_credito")],
        [InlineKeyboardButton("← Volver", callback_data=f"dtyp:{pedido_id}:{dtype}")],
    ]


async def _post_send(cb, ctx, pedido_row: dict, pedido_id: int, result, modo: str) -> None:
    """Procesa el resultado del envio a Siigo: actualiza estado, muestra mensaje
    informando del progreso (creacion -> descarga PDF -> entrega) y manda PDF."""
    if not result.ok:
        _update_pedido_estado(pedido_id, "error", error=(result.error or "")[:500])
        await cb.edit_message_text(
            f"⚠️ *Error al crear factura*\n\n`{result.error}`",
            parse_mode="Markdown",
        )
        return

    _update_pedido_estado(
        pedido_id, "enviado",
        siigo_invoice_id=result.siigo_id,
        siigo_invoice_name=result.siigo_name,
    )

    base_msg = (
        f"✅ *Factura {result.siigo_name} creada*\n"
        f"_{modo}_\n\n"
        f"💰 Total: `${result.total:,.0f}`"
    )
    if result.public_url:
        base_msg += f"\n\n🔗 [Ver en Siigo]({result.public_url})"

    # 1) Mostrar mensaje con loader "descargando PDF" para que el usuario sepa
    #    que viene mas. Si no tiene siigo_id, terminamos aca.
    if not result.siigo_id:
        await cb.edit_message_text(base_msg, parse_mode="Markdown", disable_web_page_preview=True)
        return

    chat_id_dest = pedido_row["telegram_chat_id"]
    msg_con_loader = base_msg + "\n\n⏳ _Descargando PDF…_"
    await cb.edit_message_text(msg_con_loader, parse_mode="Markdown", disable_web_page_preview=True)

    # 2) Mostrar chat action 'uploading document' mientras descargamos
    try:
        await ctx.bot.send_chat_action(chat_id=chat_id_dest, action="upload_document")
    except Exception:
        pass

    # 3) Descargar PDF en thread (no bloquea el event loop)
    try:
        pdf_bytes = await asyncio.to_thread(get_invoice_pdf, result.siigo_id)
    except Exception:
        log.exception("Error descargando PDF")
        pdf_bytes = None

    # 4) Editar mensaje quitando el loader + mandar PDF si lo tenemos
    if pdf_bytes:
        await cb.edit_message_text(base_msg, parse_mode="Markdown", disable_web_page_preview=True)
        try:
            await ctx.bot.send_document(
                chat_id=chat_id_dest,
                document=pdf_bytes,
                filename=f"{result.siigo_name}.pdf",
                caption=f"📄 {result.siigo_name}",
            )
        except Exception:
            log.exception("Error mandando PDF")
            await ctx.bot.send_message(
                chat_id=chat_id_dest,
                text=f"⚠️ La factura se creó pero no pude mandar el PDF. Vela en Siigo.",
                parse_mode="Markdown",
            )
    else:
        # Sin PDF: editar para que el usuario sepa que la factura SI esta hecha
        await cb.edit_message_text(
            base_msg + "\n\n_📎 PDF no disponible en este momento. Podés verlo en Siigo web._",
            parse_mode="Markdown",
            disable_web_page_preview=True,
        )


def _build_item_picker(pedido_id: int, item_idx: int, item) -> list[list[InlineKeyboardButton]]:
    """Sub-menu para elegir entre candidatos de un item."""
    buttons: list[list[InlineKeyboardButton]] = []
    for c in item.candidatos[:5]:
        marca = "✓ " if item.elegido and c.id == item.elegido.id else ""
        precio_txt = f" — ${c.price_default:,.0f}" if c.price_default else ""
        buttons.append([InlineKeyboardButton(
            f"{marca}{c.code}  {c.name[:30]}{precio_txt}",
            callback_data=f"picki:{pedido_id}:{item_idx}:{c.id}",
        )])
    buttons.append([InlineKeyboardButton(
        "← Volver al pedido",
        callback_data=f"back:{pedido_id}",
    )])
    return buttons


# =============================================================================
# CALLBACKS DE BOTONES
# =============================================================================

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    cb = update.callback_query
    assert cb and cb.data
    await cb.answer()

    parts = cb.data.split(":")
    accion = parts[0]

    # Callbacks de PAGOS (cache en memoria, no DB de pedidos)
    if accion in ("payopt", "paycanc"):
        await _handle_pago_callback(cb, ctx, accion, parts)
        return

    # Callbacks de PAGOS A PROVEEDOR
    if accion in ("prvopt", "prvcanc"):
        await _handle_pago_proveedor_callback(cb, ctx, accion, parts)
        return

    # Callbacks de ANULACION de factura
    if accion == "anul":
        await _handle_anulacion_callback(cb, ctx, parts)
        return

    # Callbacks de COMPROBANTES (foto OCR)
    if accion in ("cpapply", "cpsearch", "cpcancel"):
        await _handle_comprobante_callback(cb, ctx, accion, parts)
        return

    # Callbacks de FACTURAS de CORREO
    if accion in ("fcok", "fcno"):
        await _handle_factura_correo_callback(cb, ctx, accion, parts)
        return
    if accion == "fcback":
        factura_id = int(parts[1])
        await _mostrar_factura_correo(cb, ctx, factura_id)
        return
    if accion in ("prvcrear", "prvedit"):
        await _handle_proveedor_callback(cb, ctx, accion, parts)
        return

    if accion in ("gmok", "gmcanc", "gmpay", "gmback"):
        await _handle_gasto_manual_callback(cb, ctx, accion, parts)
        return

    if accion in ("iedi", "ieqt", "iepr", "iedl"):
        await _handle_edit_item_callback(cb, ctx, accion, parts)
        return

    # Confirmar si los precios manuales son con/sin IVA
    if accion == "ivac":
        await _handle_iva_confirm(cb, ctx, parts)
        return

    # Crear cliente nuevo desde un pedido
    if accion == "newcli":
        await _handle_newcli_iniciar(cb, ctx, parts)
        return
    if accion in ("newclitipo", "newclicat", "newcliok", "newclicanc"):
        await _handle_newcli_flow(cb, ctx, accion, parts)
        return

    pedido_id = int(parts[1])
    pedido_row = _load_pedido(pedido_id)
    if not pedido_row:
        await cb.edit_message_text("Pedido no encontrado")
        return

    if accion == "canc":
        _update_pedido_estado(pedido_id, "cancelado")
        await cb.edit_message_text(f"❌ *Pedido #{pedido_id} cancelado*", parse_mode="Markdown")
        return

    # Paso intermedio: elegir tipo de documento (electronica / tradicional)
    if accion == "dtyp":
        dtype = parts[2] if len(parts) > 2 else "trad"
        if dtype not in ("elec", "trad"):
            dtype = "trad"
        rp = _rehydrate_resolved(pedido_row)
        label = _doc_type_label(dtype)
        await cb.edit_message_text(
            f"*Pedido #{pedido_id}* — {label}\n\n"
            f"{format_summary(rp)}\n\n"
            f"¿Cómo se envía?",
            reply_markup=InlineKeyboardMarkup(_build_payment_step_buttons(pedido_id, dtype)),
            parse_mode="Markdown",
        )
        return

    # Enviar como CREDITO
    if accion == "sendcr":
        dtype = parts[2] if len(parts) > 2 else "trad"
        from skiimo.config import DEFAULT_INVOICE_DOC_ID, INVOICE_DOC_ID_ELECTRONIC
        doc_id_pick = INVOICE_DOC_ID_ELECTRONIC if dtype == "elec" else DEFAULT_INVOICE_DOC_ID
        label = _doc_type_label(dtype)
        rp = _rehydrate_resolved(pedido_row)
        await cb.edit_message_text(
            f"⏳ *Pedido #{pedido_id}*\n\nEnviando {label} como CRÉDITO a Siigo...",
            parse_mode="Markdown",
        )
        result = await asyncio.to_thread(
            crear_factura_venta, rp, f"chat:{pedido_row['telegram_chat_id']}",
            payment_mode="credito", payment_method="efectivo", doc_id=doc_id_pick,
        )
        await _post_send(cb, ctx, pedido_row, pedido_id, result, modo=f"{label} · CRÉDITO")
        return

    # Pedir metodo de pago para envio PAGADA
    if accion == "sendpag":
        dtype = parts[2] if len(parts) > 2 else "trad"
        if dtype not in ("elec", "trad"):
            dtype = "trad"
        label = _doc_type_label(dtype)
        await cb.edit_message_text(
            f"*Pedido #{pedido_id}* — {label}\n\n¿Cómo lo pagó el cliente?",
            reply_markup=InlineKeyboardMarkup(_build_payment_method_picker(pedido_id, dtype)),
            parse_mode="Markdown",
        )
        return

    # Ejecutar envio PAGADA con metodo concreto
    if accion == "pay":
        # Compat: formato nuevo "pay:<pid>:<dtype>:<metodo>" o legacy "pay:<pid>:<metodo>"
        if len(parts) >= 4:
            dtype = parts[2]
            metodo = parts[3]
        else:
            dtype = "trad"
            metodo = parts[2]
        from skiimo.config import DEFAULT_INVOICE_DOC_ID, INVOICE_DOC_ID_ELECTRONIC
        doc_id_pick = INVOICE_DOC_ID_ELECTRONIC if dtype == "elec" else DEFAULT_INVOICE_DOC_ID
        label = _doc_type_label(dtype)
        rp = _rehydrate_resolved(pedido_row)
        nombre_metodo = {
            "efectivo": "Efectivo", "nequi": "Nequi", "daviplata": "Daviplata",
            "banco_ahorros": "Transferencia", "tarjeta_debito": "Tarjeta débito",
            "tarjeta_credito": "Tarjeta crédito",
        }.get(metodo, metodo)
        await cb.edit_message_text(
            f"⏳ *Pedido #{pedido_id}*\n\nEnviando {label} PAGADA ({nombre_metodo})...",
            parse_mode="Markdown",
        )
        result = await asyncio.to_thread(
            crear_factura_venta, rp, f"chat:{pedido_row['telegram_chat_id']}",
            payment_mode="contado", payment_method=metodo, doc_id=doc_id_pick,
        )
        await _post_send(cb, ctx, pedido_row, pedido_id, result, modo=f"{label} · PAGADA en {nombre_metodo}")
        return

    # Sub-menu para elegir entre candidatos de un item
    if accion == "editi":
        idx = int(parts[2])
        rp = _rehydrate_resolved(pedido_row)
        if idx >= len(rp.items):
            return
        item = rp.items[idx]
        nombre_actual = item.elegido.name if item.elegido else item.raw.descripcion
        text = (
            f"*Cambiar producto {idx+1}*\n\n"
            f"_Actual:_ {nombre_actual}\n\n"
            f"Elegí la alternativa:"
        )
        await cb.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(_build_item_picker(pedido_id, idx, item)),
            parse_mode="Markdown",
        )
        return

    # Volver al pedido (desde un sub-menu)
    if accion == "back":
        rp = _rehydrate_resolved(pedido_row)
        await cb.edit_message_text(
            f"*Pedido #{pedido_id}*\n\n{format_summary(rp)}",
            reply_markup=InlineKeyboardMarkup(_build_pedido_buttons(pedido_id, rp)),
            parse_mode="Markdown",
        )
        return

    # Picks: actualizar payload y re-mostrar
    if accion in ("pickc", "picki"):
        payload = json.loads(pedido_row["payload_extraido"])
        if accion == "pickc":
            cust_id = parts[2]
            for c in payload.get("cliente_candidatos", []):
                if c.get("id") == cust_id:
                    payload["cliente"] = c
                    payload["cliente_candidatos"] = []
                    break
        elif accion == "picki":
            idx = int(parts[2])
            prod_id = parts[3]
            item = payload["items"][idx]
            for c in item.get("candidatos", []):
                if c.get("id") == prod_id:
                    item["elegido"] = c
                    # Si no hay precio aun, usar el del producto elegido (motor de precios lo recalcula al rehidratar)
                    if item.get("precio_unitario") is None and c.get("price_default"):
                        if c.get("tax_included") and c.get("iva_percentage"):
                            item["precio_unitario"] = c["price_default"] / (1 + c["iva_percentage"] / 100)
                        else:
                            item["precio_unitario"] = c["price_default"]
                    break

        # Recalcular pendientes
        pend: list[str] = []
        if not payload.get("cliente"):
            pend.append("Sin cliente")
        for i, it in enumerate(payload["items"]):
            if not it.get("elegido"):
                pend.append(f"Item {i+1} sin elegir")
            elif it.get("precio_unitario") is None:
                pend.append(f"Item {i+1} sin precio")
        payload["necesita_input_humano"] = pend

        conn = get_conn()
        try:
            conn.execute(
                "UPDATE bot_pedidos SET payload_extraido = ?, updated_at = ?, customer_id = ? "
                "WHERE id = ?",
                (
                    json.dumps(payload, ensure_ascii=False, default=str),
                    datetime.now().isoformat(timespec="seconds"),
                    (payload.get("cliente") or {}).get("id"),
                    pedido_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        rp = _rehydrate_resolved({**pedido_row, "payload_extraido": json.dumps(payload, ensure_ascii=False)})
        await cb.edit_message_text(
            f"*Pedido #{pedido_id}*\n\n{format_summary(rp)}",
            reply_markup=InlineKeyboardMarkup(_build_pedido_buttons(pedido_id, rp)),
            parse_mode="Markdown",
        )


def _rehydrate_resolved(pedido_row: dict) -> ResolvedPedido:
    """Reconstruye ResolvedPedido desde payload guardado en DB."""
    from skiimo.llm.schemas import Pedido, PedidoItem
    from skiimo.matcher import CustomerHit, ProductHit
    from skiimo.pipeline import ResolvedItem

    payload = json.loads(pedido_row["payload_extraido"])
    raw_pedido = Pedido.model_validate(payload["raw"])
    rp = ResolvedPedido(raw=raw_pedido)
    if payload.get("cliente"):
        c = payload["cliente"]
        rp.cliente_elegido = CustomerHit(
            id=c["id"], identification=c["identification"], name=c["name"],
            commercial_name=c.get("commercial_name", ""), email=c.get("email", ""),
            score=c.get("score", 100.0),
        )
    rp.cliente_candidatos = [
        CustomerHit(id=c["id"], identification=c["identification"], name=c["name"],
                    commercial_name=c.get("commercial_name", ""), email=c.get("email", ""),
                    score=c.get("score", 0.0))
        for c in payload.get("cliente_candidatos", [])
    ]
    for it in payload["items"]:
        raw_item = PedidoItem.model_validate(it["raw"])
        eleg = None
        if it.get("elegido"):
            e = it["elegido"]
            eleg = ProductHit(
                id=e["id"], code=e["code"], name=e["name"],
                account_group_name=e.get("account_group_name", ""),
                price_default=e.get("price_default"),
                iva_tax_id=e.get("iva_tax_id"), iva_percentage=e.get("iva_percentage"),
                tax_included=e.get("tax_included", False),
                score=e.get("score", 100.0),
            )
        cands = [
            ProductHit(id=c["id"], code=c["code"], name=c["name"],
                       account_group_name=c.get("account_group_name", ""),
                       price_default=c.get("price_default"),
                       iva_tax_id=c.get("iva_tax_id"), iva_percentage=c.get("iva_percentage"),
                       tax_included=c.get("tax_included", False),
                       score=c.get("score", 0.0))
            for c in it.get("candidatos", [])
        ]
        rp.items.append(ResolvedItem(
            raw=raw_item,
            candidatos=cands,
            elegido=eleg,
            cantidad=it["cantidad"],
            precio_unitario=it.get("precio_unitario"),
        ))
    rp.necesita_input_humano = payload.get("necesita_input_humano", [])
    rp.test_mode = payload.get("test_mode", True)
    return rp


# =============================================================================
# MAIN
# =============================================================================

BOT_COMMANDS: list[BotCommand] = [
    BotCommand("factura", "Cargar factura proveedor (foto/PDF)"),
    BotCommand("gastomanual", "Gasto sin factura (DS - conversacional)"),
    BotCommand("resumen", "Resumen del día"),
    BotCommand("correos", "Revisar facturas del correo"),
    BotCommand("agregar", "Agregar usuario / vendedor"),
    BotCommand("yo", "Ver mi info"),
    BotCommand("nuevo", "Empezar conversación de cero"),
    BotCommand("cancelar", "Cancelar pedido en curso"),
    BotCommand("start", "Saludo / instrucciones"),
]

# Menus por rol (Telegram permite comandos por chat con BotCommandScopeChat)
CMDS_TRABAJADOR = [
    BotCommand("mi_asistencia", "Mis marcajes de hoy"),
    BotCommand("mi_nomina", "Cuánto llevo esta quincena"),
    BotCommand("mis_kpis", "Mi asistencia y tardanzas"),
]
CMDS_SUPERVISION = CMDS_TRABAJADOR + [
    BotCommand("asistencia_hoy", "Resumen de asistencia de hoy"),
    BotCommand("quien_esta", "Quién está en la fábrica ahora"),
    BotCommand("llegadas_tarde", "Llegadas tarde (7 días)"),
    BotCommand("nomina_equipo", "Horas y pago del equipo"),
    BotCommand("extras", "Programar horas extra"),
]


async def _aplicar_menu_por_rol(bot, chat_id: int) -> None:
    """Configura el menu de comandos de Telegram segun el rol del chat.

    - admin-env / vendedor (bot_vendedores): menu de ventas (BOT_COMMANDS).
    - empleado Administrativo/Coordinador: menu de supervision.
    - empleado Operario/Venta: menu de trabajador.
    """
    from telegram import BotCommandScopeChat
    try:
        from skiimo.bot.asistencia_commands import empleado_por_chat, puede_supervisar
        ok, _info = _is_authorized(chat_id)
        emp = empleado_por_chat(chat_id)
        if ok:
            cmds = BOT_COMMANDS
        elif emp and puede_supervisar(chat_id):
            cmds = CMDS_SUPERVISION
        elif emp:
            cmds = CMDS_TRABAJADOR
        else:
            cmds = [BotCommand("start", "Identificarme")]
        await bot.set_my_commands(cmds, scope=BotCommandScopeChat(chat_id=chat_id))
    except Exception as e:
        log.warning("No se pudo aplicar menu por rol a %s: %s", chat_id, e)


async def _post_init(app: "Application") -> None:
    """Se ejecuta una vez al arrancar el bot. Registra comandos y boton Menu."""
    try:
        await app.bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeDefault())
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        log.info("Comandos y boton Menu registrados en Telegram.")
    except Exception as e:
        log.warning("No se pudo configurar comandos / boton Menu: %s", e)


def main() -> None:
    # Bootstrap: crea schema + sync inicial si la DB esta vacia (primer arranque en Railway)
    from skiimo.bootstrap import ensure_db_ready
    ensure_db_ready()
    # Recargar matcher por si bootstrap sincronizo datos
    m = _get_matcher()
    m.reload()
    log.info("Stats matcher: %s", m.stats())

    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("yo", cmd_yo))
    app.add_handler(CommandHandler("nuevo", cmd_nuevo))
    app.add_handler(CommandHandler("cancelar", cmd_cancelar))
    app.add_handler(CommandHandler("correos", cmd_correos))
    app.add_handler(CommandHandler("factura", cmd_factura))
    app.add_handler(CommandHandler("gasto", cmd_factura))
    app.add_handler(CommandHandler("gastomanual", cmd_gastomanual))
    app.add_handler(CommandHandler("gm", cmd_gastomanual))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("agregar", cmd_agregar))

    # Comandos de TRABAJADOR (asistencia, solo su propia info)
    from skiimo.bot.asistencia_commands import (
        cmd_mi_asistencia, cmd_mi_nomina, cmd_mis_kpis, cmd_mi_help,
        job_aviso_no_marco, job_aviso_sin_salida,
        cmd_quien_esta, cmd_asistencia_hoy, cmd_llegadas_tarde,
        cmd_quincena, cmd_asistencia_help, cmd_extras,
    )
    app.add_handler(CommandHandler("mi_asistencia", cmd_mi_asistencia))
    app.add_handler(CommandHandler("mi_nomina", cmd_mi_nomina))
    app.add_handler(CommandHandler("mis_kpis", cmd_mis_kpis))
    app.add_handler(CommandHandler("mi_help", cmd_mi_help))
    # Comandos de SUPERVISION (admin/coordinador, validan permiso adentro)
    app.add_handler(CommandHandler("quien_esta", cmd_quien_esta))
    app.add_handler(CommandHandler("asistencia_hoy", cmd_asistencia_hoy))
    app.add_handler(CommandHandler("llegadas_tarde", cmd_llegadas_tarde))
    app.add_handler(CommandHandler("nomina_equipo", cmd_quincena))
    app.add_handler(CommandHandler("quincena_equipo", cmd_quincena))  # alias
    app.add_handler(CommandHandler("extras", cmd_extras))
    app.add_handler(CommandHandler("asistencia_help", cmd_asistencia_help))

    # Job de resumen diario a las 8:00 hora Colombia (UTC-5 = 13:00 UTC)
    from datetime import time as _time
    if ADMIN_TELEGRAM_CHAT_ID and app.job_queue:
        app.job_queue.run_daily(
            _job_resumen_diario,
            time=_time(hour=13, minute=0),  # 13:00 UTC = 8:00 Colombia
            name="resumen_diario",
        )
        log.info("Job resumen diario programado: 8:00 hora Colombia (13:00 UTC)")
    # Avisos a trabajadores vinculados (hora Colombia = UTC-5)
    if app.job_queue:
        app.job_queue.run_daily(
            job_aviso_no_marco,
            time=_time(hour=12, minute=15),  # 7:15 Colombia: no marco entrada
            name="aviso_no_marco",
        )
        app.job_queue.run_daily(
            job_aviso_sin_salida,
            time=_time(hour=23, minute=0),   # 18:00 Colombia: no marco salida
            name="aviso_sin_salida",
        )
        log.info("Jobs de aviso a trabajadores programados (7:15 y 18:00 Colombia)")
    # Sync periodico de facturas recientes (cada 5 min)
    if app.job_queue:
        app.job_queue.run_repeating(
            _job_sync_periodico,
            interval=300,  # 5 minutos
            first=30,       # primera ejecucion 30s despues del arranque
            name="sync_invoices_periodico",
        )
        log.info("Job sync periodico programado: cada 5 minutos")
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.IMAGE | filters.Document.PDF, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))

    # Error handler global: si algun handler falla, avisar al usuario en vez de quedar mudo
    async def _error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        err = ctx.error
        log.exception("Excepcion no manejada en handler", exc_info=err)
        try:
            chat = getattr(update, "effective_chat", None) if isinstance(update, Update) else None
            if chat:
                await ctx.bot.send_message(
                    chat_id=chat.id,
                    text=(
                        f"⚠️ Algo salió mal procesando tu mensaje.\n\n"
                        f"`{type(err).__name__}: {str(err)[:200]}`\n\n"
                        f"Probá de nuevo o avisame qué hiciste para que lo arregle."
                    ),
                    parse_mode="Markdown",
                )
        except Exception:
            log.exception("Error mandando mensaje al usuario sobre el error")
    app.add_error_handler(_error_handler)

    log.info("Bot arrancado en modo polling. Ctrl+C para detener.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
