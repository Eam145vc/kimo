"""CRM WhatsApp: webhook Cloud API + inbox + pipeline + gestion de lineas/usuarios.

Vistas:
  GET  /crm             -> inbox (lista de chats + conversacion)
  GET  /crm/pipeline    -> kanban por etapas
  GET  /crm/usuarios    -> admin: usuarios del panel y lineas

Webhook publico (Meta Cloud API):
  GET  /webhooks/whatsapp   -> verificacion (hub.challenge)
  POST /webhooks/whatsapp   -> mensajes entrantes + statuses

Roles:
  admin/dev -> ven todas las lineas y administran usuarios.
  agente    -> solo sus lineas (wa_lineas.agente_user_id). No ve el resto del panel.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel

from skiimo.config import (
    DB_PATH, WA_ACCESS_TOKEN, WA_APP_SECRET, WA_GRAPH_BASE, WA_VERIFY_TOKEN,
)
from skiimo.db.schema import get_conn
from skiimo.panel.auth import crear_usuario, cambiar_password, validar_sesion

log = logging.getLogger("skiimo.panel.crm")

router = APIRouter()

TZ = ZoneInfo("America/Bogota")
WA_MEDIA_DIR = DB_PATH.parent / "wa_media"
WA_MEDIA_DIR.mkdir(parents=True, exist_ok=True)

ETAPAS_SEED = [
    ("Nuevo", 1, "#4FC4E8"),
    ("En conversación", 2, "#f59e0b"),
    ("Cotizado", 3, "#a78bfa"),
    ("Pedido", 4, "#22c55e"),
    ("Entregado", 5, "#5b6b8f"),
]


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def ensure_crm_seed() -> None:
    """Etapas y lineas default. Idempotente; se llama en el startup del panel."""
    conn = get_conn()
    try:
        n = conn.execute("SELECT COUNT(*) FROM crm_etapas").fetchone()[0]
        if n == 0:
            for nombre, orden, color in ETAPAS_SEED:
                conn.execute(
                    "INSERT INTO crm_etapas (nombre, orden, color) VALUES (?, ?, ?)",
                    (nombre, orden, color),
                )
        n = conn.execute("SELECT COUNT(*) FROM wa_lineas").fetchone()[0]
        if n == 0:
            for nombre in ("Ventas", "Atención al cliente"):
                conn.execute(
                    "INSERT INTO wa_lineas (nombre, activo, created_at) VALUES (?, 1, ?)",
                    (nombre, _now()),
                )
        conn.commit()
    finally:
        conn.close()


# =============================================================================
# Guards
# =============================================================================

def _require_crm(session_token: str | None) -> dict:
    user = validar_sesion(session_token)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    return user


def _require_admin(session_token: str | None) -> dict:
    user = _require_crm(session_token)
    if user.get("role") not in ("admin", "dev"):
        raise HTTPException(status_code=403, detail="Solo admin")
    return user


def _lineas_permitidas(user: dict) -> list[int] | None:
    """None = todas (admin/dev). Lista de ids para agente."""
    if user.get("role") in ("admin", "dev"):
        return None
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id FROM wa_lineas WHERE agente_user_id = ? AND activo = 1",
            (user["id"],),
        ).fetchall()
    finally:
        conn.close()
    return [r["id"] for r in rows]


def _chat_permitido(conn, chat_id: int, user: dict) -> dict:
    row = conn.execute(
        """SELECT ch.*, l.agente_user_id, l.phone_number_id, l.nombre AS linea_nombre,
                  co.telefono, co.nombre_wa, co.nombre_custom, co.siigo_customer_id,
                  co.id AS contacto_id
           FROM wa_chats ch
           JOIN wa_lineas l ON l.id = ch.linea_id
           JOIN wa_contactos co ON co.id = ch.contacto_id
           WHERE ch.id = ?""",
        (chat_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Chat no encontrado")
    if user.get("role") not in ("admin", "dev") and row["agente_user_id"] != user["id"]:
        raise HTTPException(403, "Chat de otra línea")
    return dict(row)


# =============================================================================
# Helpers de datos
# =============================================================================

def _match_siigo(conn, telefono: str) -> str | None:
    """Busca cliente Siigo cuyo telefono termine en los mismos 10 digitos."""
    digits = "".join(c for c in telefono if c.isdigit())[-10:]
    if len(digits) < 7:
        return None
    row = conn.execute(
        """SELECT id FROM siigo_customers
           WHERE active = 1 AND phone IS NOT NULL AND phone != ''
             AND REPLACE(REPLACE(REPLACE(REPLACE(phone,' ',''),'-',''),'(',''),')','') LIKE ?
           LIMIT 1""",
        (f"%{digits}",),
    ).fetchone()
    return row["id"] if row else None


def _upsert_contacto(conn, telefono: str, nombre_wa: str | None) -> int:
    row = conn.execute(
        "SELECT id, nombre_wa FROM wa_contactos WHERE telefono = ?", (telefono,)
    ).fetchone()
    now = _now()
    if row:
        if nombre_wa and nombre_wa != row["nombre_wa"]:
            conn.execute(
                "UPDATE wa_contactos SET nombre_wa = ?, updated_at = ? WHERE id = ?",
                (nombre_wa, now, row["id"]),
            )
        return row["id"]
    siigo_id = _match_siigo(conn, telefono)
    cur = conn.execute(
        """INSERT INTO wa_contactos (telefono, nombre_wa, siigo_customer_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (telefono, nombre_wa, siigo_id, now, now),
    )
    return cur.lastrowid


def _get_or_create_chat(conn, linea_id: int, contacto_id: int) -> int:
    row = conn.execute(
        "SELECT id FROM wa_chats WHERE linea_id = ? AND contacto_id = ?",
        (linea_id, contacto_id),
    ).fetchone()
    if row:
        return row["id"]
    etapa = conn.execute("SELECT id FROM crm_etapas ORDER BY orden LIMIT 1").fetchone()
    cur = conn.execute(
        """INSERT INTO wa_chats (linea_id, contacto_id, etapa_id, created_at)
           VALUES (?, ?, ?, ?)""",
        (linea_id, contacto_id, etapa["id"] if etapa else None, _now()),
    )
    return cur.lastrowid


def _touch_chat(conn, chat_id: int, ts: str, preview: str, inc_unread: bool) -> None:
    conn.execute(
        """UPDATE wa_chats
           SET last_msg_at = MAX(COALESCE(last_msg_at,''), ?),
               last_msg_preview = ?,
               unread = unread + ?
           WHERE id = ?""",
        (ts, (preview or "")[:120], 1 if inc_unread else 0, chat_id),
    )


# =============================================================================
# Webhook Cloud API
# =============================================================================

@router.get("/webhooks/whatsapp")
async def wa_webhook_verify(request: Request):
    q = request.query_params
    if q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == WA_VERIFY_TOKEN and WA_VERIFY_TOKEN:
        return PlainTextResponse(q.get("hub.challenge", ""))
    raise HTTPException(403, "verify_token invalido")


@router.post("/webhooks/whatsapp")
async def wa_webhook(request: Request):
    body = await request.body()
    if WA_APP_SECRET:
        sig = request.headers.get("x-hub-signature-256", "")
        expected = "sha256=" + hmac.new(WA_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise HTTPException(403, "Firma invalida")
    try:
        payload = json.loads(body or b"{}")
    except Exception:
        return {"ok": True}
    try:
        _procesar_webhook(payload)
    except Exception:
        log.exception("Error procesando webhook WA")
    # Siempre 200: Meta reintenta y desactiva el webhook si acumula errores
    return {"ok": True}


def _procesar_webhook(payload: dict) -> None:
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value") or {}
            if value.get("messaging_product") != "whatsapp":
                continue
            meta = value.get("metadata") or {}
            pnid = meta.get("phone_number_id")
            if not pnid:
                continue
            conn = get_conn()
            try:
                linea = conn.execute(
                    "SELECT id FROM wa_lineas WHERE phone_number_id = ?", (pnid,)
                ).fetchone()
                if not linea:
                    # Autocrear para no perder mensajes de una linea aun no configurada
                    cur = conn.execute(
                        "INSERT INTO wa_lineas (nombre, phone_number_id, display_number, activo, created_at) "
                        "VALUES (?, ?, ?, 1, ?)",
                        (f"Línea {meta.get('display_phone_number', pnid)}", pnid,
                         meta.get("display_phone_number"), _now()),
                    )
                    linea_id = cur.lastrowid
                else:
                    linea_id = linea["id"]

                nombres = {
                    c.get("wa_id"): (c.get("profile") or {}).get("name")
                    for c in value.get("contacts", [])
                }
                for msg in value.get("messages", []):
                    _guardar_entrante(conn, linea_id, msg, nombres)
                for st in value.get("statuses", []):
                    conn.execute(
                        "UPDATE wa_mensajes SET status = ? WHERE wa_msg_id = ?",
                        (st.get("status"), st.get("id")),
                    )
                conn.commit()
            finally:
                conn.close()


def _guardar_entrante(conn, linea_id: int, msg: dict, nombres: dict) -> None:
    wa_id = msg.get("from", "")
    tipo = msg.get("type", "text")
    ts = msg.get("timestamp")
    try:
        ts_iso = datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(TZ).isoformat(timespec="seconds")
    except Exception:
        ts_iso = _now()

    body, media_id, media_mime = None, None, None
    if tipo == "text":
        body = (msg.get("text") or {}).get("body")
    elif tipo in ("image", "video", "audio", "document", "sticker"):
        m = msg.get(tipo) or {}
        body = m.get("caption") or m.get("filename")
        media_id, media_mime = m.get("id"), m.get("mime_type")
    elif tipo == "location":
        loc = msg.get("location") or {}
        body = f"📍 {loc.get('latitude')},{loc.get('longitude')} {loc.get('name') or ''}".strip()
    elif tipo == "button":
        body = (msg.get("button") or {}).get("text")
    elif tipo == "interactive":
        inter = msg.get("interactive") or {}
        body = (inter.get("button_reply") or inter.get("list_reply") or {}).get("title")
    else:
        body = f"({tipo})"

    contacto_id = _upsert_contacto(conn, wa_id, nombres.get(wa_id))
    chat_id = _get_or_create_chat(conn, linea_id, contacto_id)
    try:
        cur = conn.execute(
            """INSERT INTO wa_mensajes
               (chat_id, wa_msg_id, direccion, tipo, body, media_mime, ts, origen, raw, created_at)
               VALUES (?, ?, 'in', ?, ?, ?, ?, 'api', ?, ?)""",
            (chat_id, msg.get("id"), tipo, body, media_mime, ts_iso,
             json.dumps(msg, ensure_ascii=False), _now()),
        )
    except Exception as e:
        if "UNIQUE" in str(e):
            return  # reintento de Meta, ya lo tenemos
        raise
    preview = body if tipo == "text" else f"📎 {tipo}" + (f": {body}" if body else "")
    _touch_chat(conn, chat_id, ts_iso, preview or "", inc_unread=True)

    if media_id and WA_ACCESS_TOKEN:
        row_id = cur.lastrowid
        threading.Thread(
            target=_descargar_media, args=(row_id, media_id), daemon=True
        ).start()


def _descargar_media(mensaje_id: int, media_id: str) -> None:
    """Best-effort: baja el binario de Graph y guarda la ruta local."""
    try:
        headers = {"Authorization": f"Bearer {WA_ACCESS_TOKEN}"}
        with httpx.Client(timeout=60) as client:
            info = client.get(f"{WA_GRAPH_BASE}/{media_id}", headers=headers).json()
            url = info.get("url")
            if not url:
                return
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            mime = info.get("mime_type") or resp.headers.get("content-type", "")
            ext = {
                "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                "video/mp4": ".mp4", "audio/ogg": ".ogg", "audio/mpeg": ".mp3",
                "application/pdf": ".pdf",
            }.get(mime.split(";")[0], ".bin")
            fname = f"{media_id}{ext}"
            (WA_MEDIA_DIR / fname).write_bytes(resp.content)
        conn = get_conn()
        try:
            conn.execute(
                "UPDATE wa_mensajes SET media_path = ? WHERE id = ?", (fname, mensaje_id)
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        log.exception("Descarga de media WA fallo (media_id=%s)", media_id)


# =============================================================================
# API: inbox
# =============================================================================

@router.get("/api/crm/lineas")
async def api_lineas(session_token: str | None = Cookie(default=None)):
    user = _require_crm(session_token)
    permitidas = _lineas_permitidas(user)
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT l.id, l.nombre, l.phone_number_id, l.display_number, l.activo,
                      l.agente_user_id, u.username AS agente
               FROM wa_lineas l LEFT JOIN panel_users u ON u.id = l.agente_user_id
               ORDER BY l.id"""
        ).fetchall()
    finally:
        conn.close()
    items = [dict(r) for r in rows]
    if permitidas is not None:
        items = [r for r in items if r["id"] in permitidas]
    return {"items": items, "role": user.get("role")}


@router.get("/api/crm/etapas")
async def api_etapas(session_token: str | None = Cookie(default=None)):
    _require_crm(session_token)
    conn = get_conn()
    try:
        rows = conn.execute("SELECT id, nombre, orden, color FROM crm_etapas ORDER BY orden").fetchall()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows]}


# Subquery reutilizada: etiquetas del chat como "id|nombre|color;id|nombre|color"
_SQL_ETIQUETAS = (
    "(SELECT GROUP_CONCAT(e2.id || '|' || e2.nombre || '|' || e2.color, ';') "
    " FROM wa_chat_etiquetas ce JOIN crm_etiquetas e2 ON e2.id = ce.etiqueta_id "
    " WHERE ce.chat_id = ch.id) AS etiquetas"
)


@router.get("/api/crm/chats")
async def api_chats(
    session_token: str | None = Cookie(default=None),
    linea_id: int = 0, etapa_id: int = 0, q: str = "", archivado: int = 0,
    etiqueta_id: int = 0,
):
    user = _require_crm(session_token)
    permitidas = _lineas_permitidas(user)
    where = ["ch.archivado = ?"]
    params: list = [archivado]
    if permitidas is not None:
        if not permitidas:
            return {"items": []}
        where.append(f"ch.linea_id IN ({','.join('?' * len(permitidas))})")
        params.extend(permitidas)
    if linea_id:
        where.append("ch.linea_id = ?"); params.append(linea_id)
    if etapa_id:
        where.append("ch.etapa_id = ?"); params.append(etapa_id)
    if etiqueta_id:
        where.append(
            "EXISTS (SELECT 1 FROM wa_chat_etiquetas ce WHERE ce.chat_id = ch.id AND ce.etiqueta_id = ?)"
        )
        params.append(etiqueta_id)
    if q:
        like = f"%{q}%"
        where.append("(co.telefono LIKE ? OR LOWER(COALESCE(co.nombre_custom, co.nombre_wa, '')) LIKE LOWER(?) OR LOWER(COALESCE(sc.name,'')) LIKE LOWER(?))")
        params.extend([like, like, like])
    conn = get_conn()
    try:
        rows = conn.execute(
            f"""SELECT ch.id, ch.linea_id, ch.etapa_id, ch.last_msg_at, ch.last_msg_preview,
                       ch.unread, ch.archivado,
                       co.id AS contacto_id, co.telefono, co.nombre_wa, co.nombre_custom,
                       co.siigo_customer_id, sc.name AS siigo_name,
                       l.nombre AS linea_nombre, e.nombre AS etapa_nombre, e.color AS etapa_color,
                       {_SQL_ETIQUETAS}
                FROM wa_chats ch
                JOIN wa_contactos co ON co.id = ch.contacto_id
                JOIN wa_lineas l ON l.id = ch.linea_id
                LEFT JOIN crm_etapas e ON e.id = ch.etapa_id
                LEFT JOIN siigo_customers sc ON sc.id = co.siigo_customer_id
                WHERE {' AND '.join(where)}
                ORDER BY COALESCE(ch.last_msg_at, ch.created_at) DESC
                LIMIT 300""",
            params,
        ).fetchall()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows]}


@router.get("/api/crm/chats/{chat_id}")
async def api_chat_detalle(
    chat_id: int,
    session_token: str | None = Cookie(default=None),
    before_id: int = 0, limit: int = 60,
):
    user = _require_crm(session_token)
    conn = get_conn()
    try:
        chat = _chat_permitido(conn, chat_id, user)
        where = "chat_id = ?"
        params: list = [chat_id]
        if before_id:
            where += " AND id < ?"; params.append(before_id)
        rows = conn.execute(
            f"""SELECT id, wa_msg_id, direccion, tipo, body, media_path, media_mime,
                       ts, status, origen, enviado_por
                FROM wa_mensajes WHERE {where}
                ORDER BY ts DESC, id DESC LIMIT ?""",
            (*params, min(limit, 200)),
        ).fetchall()
        conn.execute("UPDATE wa_chats SET unread = 0 WHERE id = ?", (chat_id,))
        conn.commit()
        etiquetas = conn.execute(
            """SELECT e.id, e.nombre, e.color
               FROM wa_chat_etiquetas ce JOIN crm_etiquetas e ON e.id = ce.etiqueta_id
               WHERE ce.chat_id = ?""",
            (chat_id,),
        ).fetchall()
    finally:
        conn.close()
    mensajes = [dict(r) for r in rows][::-1]
    return {
        "chat": {k: chat[k] for k in (
            "id", "linea_id", "linea_nombre", "etapa_id", "contacto_id", "telefono",
            "nombre_wa", "nombre_custom", "siigo_customer_id", "archivado")},
        "etiquetas": [dict(r) for r in etiquetas],
        "mensajes": mensajes,
    }


class EnviarBody(BaseModel):
    texto: str


@router.post("/api/crm/chats/{chat_id}/enviar")
async def api_chat_enviar(
    chat_id: int, body: EnviarBody, session_token: str | None = Cookie(default=None),
):
    user = _require_crm(session_token)
    texto = (body.texto or "").strip()
    if not texto:
        return {"ok": False, "error": "Mensaje vacío"}
    conn = get_conn()
    try:
        chat = _chat_permitido(conn, chat_id, user)
    finally:
        conn.close()
    if not WA_ACCESS_TOKEN:
        return {"ok": False, "error": "Cloud API no configurada aún (falta WA_ACCESS_TOKEN en el VM)"}
    if not chat.get("phone_number_id"):
        return {"ok": False, "error": "Esta línea no tiene phone_number_id configurado (Líneas & Usuarios)"}

    def _post():
        with httpx.Client(timeout=30) as client:
            return client.post(
                f"{WA_GRAPH_BASE}/{chat['phone_number_id']}/messages",
                headers={"Authorization": f"Bearer {WA_ACCESS_TOKEN}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": chat["telefono"],
                    "type": "text",
                    "text": {"body": texto},
                },
            )
    import asyncio
    resp = await asyncio.to_thread(_post)
    data = {}
    try:
        data = resp.json()
    except Exception:
        pass
    if resp.status_code >= 300:
        err = (data.get("error") or {})
        detalle = err.get("message", f"HTTP {resp.status_code}")
        if err.get("code") == 131047 or "re-engagement" in detalle.lower():
            detalle = "Ventana de 24h cerrada: el cliente debe escribir primero, o se necesita plantilla aprobada."
        return {"ok": False, "error": detalle}
    wa_msg_id = ((data.get("messages") or [{}])[0]).get("id")
    ts = _now()
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO wa_mensajes
               (chat_id, wa_msg_id, direccion, tipo, body, ts, status, origen, enviado_por, created_at)
               VALUES (?, ?, 'out', 'text', ?, ?, 'sent', 'api', ?, ?)""",
            (chat_id, wa_msg_id, texto, ts, user["username"], ts),
        )
        _touch_chat(conn, chat_id, ts, texto, inc_unread=False)
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "wa_msg_id": wa_msg_id}


class EtapaBody(BaseModel):
    etapa_id: int


@router.post("/api/crm/chats/{chat_id}/etapa")
async def api_chat_etapa(
    chat_id: int, body: EtapaBody, session_token: str | None = Cookie(default=None),
):
    user = _require_crm(session_token)
    conn = get_conn()
    try:
        _chat_permitido(conn, chat_id, user)
        conn.execute("UPDATE wa_chats SET etapa_id = ? WHERE id = ?", (body.etapa_id, chat_id))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


class ArchivarBody(BaseModel):
    archivado: int = 1


@router.post("/api/crm/chats/{chat_id}/archivar")
async def api_chat_archivar(
    chat_id: int, body: ArchivarBody, session_token: str | None = Cookie(default=None),
):
    user = _require_crm(session_token)
    conn = get_conn()
    try:
        _chat_permitido(conn, chat_id, user)
        conn.execute("UPDATE wa_chats SET archivado = ? WHERE id = ?", (1 if body.archivado else 0, chat_id))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


class ContactoBody(BaseModel):
    nombre_custom: str | None = None
    siigo_customer_id: str | None = None


@router.post("/api/crm/contactos/{contacto_id}")
async def api_contacto_editar(
    contacto_id: int, body: ContactoBody, session_token: str | None = Cookie(default=None),
):
    _require_crm(session_token)
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE wa_contactos
               SET nombre_custom = COALESCE(?, nombre_custom),
                   siigo_customer_id = COALESCE(?, siigo_customer_id),
                   updated_at = ?
               WHERE id = ?""",
            (body.nombre_custom, body.siigo_customer_id, _now(), contacto_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@router.get("/api/crm/contactos/{contacto_id}/siigo")
async def api_contacto_siigo(contacto_id: int, session_token: str | None = Cookie(default=None)):
    _require_crm(session_token)
    conn = get_conn()
    try:
        co = conn.execute(
            "SELECT id, telefono, siigo_customer_id FROM wa_contactos WHERE id = ?",
            (contacto_id,),
        ).fetchone()
        if not co:
            raise HTTPException(404, "Contacto no encontrado")
        if not co["siigo_customer_id"]:
            # Reintentar match (pudo llegar el cliente a Siigo despues)
            sid = _match_siigo(conn, co["telefono"])
            if sid:
                conn.execute(
                    "UPDATE wa_contactos SET siigo_customer_id = ?, updated_at = ? WHERE id = ?",
                    (sid, _now(), contacto_id),
                )
                conn.commit()
            else:
                return {"match": False}
        else:
            sid = co["siigo_customer_id"]
        c = conn.execute(
            "SELECT id, identification, name, email, phone, address FROM siigo_customers WHERE id = ?",
            (sid,),
        ).fetchone()
        if not c:
            return {"match": False}
        saldo = conn.execute(
            "SELECT COALESCE(SUM(balance),0) AS s FROM siigo_invoices WHERE customer_id = ? AND balance > 0",
            (sid,),
        ).fetchone()
        total_12m = conn.execute(
            "SELECT COALESCE(SUM(total),0) AS t FROM siigo_invoices WHERE customer_id = ? AND date >= date('now','-365 days')",
            (sid,),
        ).fetchone()
        ultimas = conn.execute(
            """SELECT name, date, total, balance FROM siigo_invoices
               WHERE customer_id = ? ORDER BY date DESC LIMIT 5""",
            (sid,),
        ).fetchall()
        cat = conn.execute(
            "SELECT categoria FROM clientes_categoria WHERE customer_id = ?", (sid,)
        ).fetchone()
    finally:
        conn.close()
    return {
        "match": True,
        "cliente": dict(c),
        "categoria": cat["categoria"] if cat else None,
        "saldo": float(saldo["s"] or 0),
        "total_12m": float(total_12m["t"] or 0),
        "ultimas_facturas": [dict(r) for r in ultimas],
    }


class NotaBody(BaseModel):
    texto: str


@router.get("/api/crm/chats/{chat_id}/notas")
async def api_notas_list(chat_id: int, session_token: str | None = Cookie(default=None)):
    user = _require_crm(session_token)
    conn = get_conn()
    try:
        _chat_permitido(conn, chat_id, user)
        rows = conn.execute(
            "SELECT id, username, texto, created_at FROM crm_notas WHERE chat_id = ? ORDER BY id DESC LIMIT 50",
            (chat_id,),
        ).fetchall()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows]}


@router.post("/api/crm/chats/{chat_id}/notas")
async def api_notas_create(
    chat_id: int, body: NotaBody, session_token: str | None = Cookie(default=None),
):
    user = _require_crm(session_token)
    texto = (body.texto or "").strip()
    if not texto:
        return {"ok": False, "error": "Nota vacía"}
    conn = get_conn()
    try:
        _chat_permitido(conn, chat_id, user)
        conn.execute(
            "INSERT INTO crm_notas (chat_id, user_id, username, texto, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, user["id"], user["username"], texto, _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# =============================================================================
# API: etiquetas
# =============================================================================

@router.get("/api/crm/etiquetas")
async def api_etiquetas(session_token: str | None = Cookie(default=None)):
    _require_crm(session_token)
    conn = get_conn()
    try:
        rows = conn.execute("SELECT id, nombre, color FROM crm_etiquetas ORDER BY nombre").fetchall()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows]}


class EtiquetaBody(BaseModel):
    nombre: str
    color: str = "#4FC4E8"


@router.post("/api/crm/etiquetas")
async def api_etiqueta_crear(body: EtiquetaBody, session_token: str | None = Cookie(default=None)):
    _require_crm(session_token)
    nombre = (body.nombre or "").strip()
    if not nombre or len(nombre) > 30:
        return {"ok": False, "error": "Nombre requerido (máx. 30)"}
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO crm_etiquetas (nombre, color) VALUES (?, ?)", (nombre, body.color)
        )
        conn.commit()
        return {"ok": True, "id": cur.lastrowid}
    except Exception as e:
        if "UNIQUE" in str(e):
            return {"ok": False, "error": "Ya existe una etiqueta con ese nombre"}
        return {"ok": False, "error": str(e)[:200]}
    finally:
        conn.close()


@router.post("/api/crm/etiquetas/{etiqueta_id}/eliminar")
async def api_etiqueta_eliminar(etiqueta_id: int, session_token: str | None = Cookie(default=None)):
    _require_admin(session_token)
    conn = get_conn()
    try:
        conn.execute("DELETE FROM wa_chat_etiquetas WHERE etiqueta_id = ?", (etiqueta_id,))
        conn.execute("DELETE FROM crm_etiquetas WHERE id = ?", (etiqueta_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


class ChatEtiquetaBody(BaseModel):
    etiqueta_id: int
    poner: int = 1  # 1 = agregar, 0 = quitar


@router.post("/api/crm/chats/{chat_id}/etiquetas")
async def api_chat_etiqueta(
    chat_id: int, body: ChatEtiquetaBody, session_token: str | None = Cookie(default=None),
):
    user = _require_crm(session_token)
    conn = get_conn()
    try:
        _chat_permitido(conn, chat_id, user)
        if body.poner:
            conn.execute(
                "INSERT OR IGNORE INTO wa_chat_etiquetas (chat_id, etiqueta_id) VALUES (?, ?)",
                (chat_id, body.etiqueta_id),
            )
        else:
            conn.execute(
                "DELETE FROM wa_chat_etiquetas WHERE chat_id = ? AND etiqueta_id = ?",
                (chat_id, body.etiqueta_id),
            )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@router.get("/api/crm/pipeline")
async def api_pipeline(
    session_token: str | None = Cookie(default=None), linea_id: int = 0,
):
    user = _require_crm(session_token)
    permitidas = _lineas_permitidas(user)
    where = ["ch.archivado = 0"]
    params: list = []
    if permitidas is not None:
        if not permitidas:
            return {"etapas": []}
        where.append(f"ch.linea_id IN ({','.join('?' * len(permitidas))})")
        params.extend(permitidas)
    if linea_id:
        where.append("ch.linea_id = ?"); params.append(linea_id)
    conn = get_conn()
    try:
        etapas = conn.execute("SELECT id, nombre, orden, color FROM crm_etapas ORDER BY orden").fetchall()
        rows = conn.execute(
            f"""SELECT ch.id, ch.etapa_id, ch.last_msg_at, ch.last_msg_preview, ch.unread,
                       co.telefono, co.nombre_wa, co.nombre_custom, sc.name AS siigo_name,
                       l.nombre AS linea_nombre,
                       {_SQL_ETIQUETAS}
                FROM wa_chats ch
                JOIN wa_contactos co ON co.id = ch.contacto_id
                JOIN wa_lineas l ON l.id = ch.linea_id
                LEFT JOIN siigo_customers sc ON sc.id = co.siigo_customer_id
                WHERE {' AND '.join(where)}
                ORDER BY COALESCE(ch.last_msg_at, ch.created_at) DESC
                LIMIT 500""",
            params,
        ).fetchall()
    finally:
        conn.close()
    por_etapa: dict = {e["id"]: [] for e in etapas}
    for r in rows:
        por_etapa.setdefault(r["etapa_id"], []).append(dict(r))
    return {
        "etapas": [
            {**dict(e), "chats": por_etapa.get(e["id"], [])} for e in etapas
        ],
    }


# =============================================================================
# API: admin (lineas + usuarios del panel)
# =============================================================================

class LineaBody(BaseModel):
    nombre: str | None = None
    phone_number_id: str | None = None
    display_number: str | None = None
    agente_user_id: int | None = None
    activo: int | None = None


@router.post("/api/crm/lineas/{linea_id}")
async def api_linea_editar(
    linea_id: int, body: LineaBody, session_token: str | None = Cookie(default=None),
):
    _require_admin(session_token)
    sets, params = [], []
    for campo in ("nombre", "phone_number_id", "display_number", "agente_user_id", "activo"):
        val = getattr(body, campo)
        if val is not None:
            sets.append(f"{campo} = ?")
            params.append(val if val != "" else None)
    if not sets:
        return {"ok": False, "error": "Nada que actualizar"}
    conn = get_conn()
    try:
        conn.execute(f"UPDATE wa_lineas SET {', '.join(sets)} WHERE id = ?", (*params, linea_id))
        conn.commit()
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    finally:
        conn.close()
    return {"ok": True}


class LineaCrearBody(BaseModel):
    nombre: str


@router.post("/api/crm/lineas")
async def api_linea_crear(body: LineaCrearBody, session_token: str | None = Cookie(default=None)):
    _require_admin(session_token)
    nombre = (body.nombre or "").strip()
    if not nombre:
        return {"ok": False, "error": "Nombre requerido"}
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO wa_lineas (nombre, activo, created_at) VALUES (?, 1, ?)",
            (nombre, _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@router.get("/api/crm/usuarios")
async def api_usuarios(session_token: str | None = Cookie(default=None)):
    _require_admin(session_token)
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT u.id, u.username, u.role, u.active, u.created_at, u.last_login_at,
                      (SELECT GROUP_CONCAT(l.nombre, ', ') FROM wa_lineas l WHERE l.agente_user_id = u.id) AS lineas
               FROM panel_users u ORDER BY u.id"""
        ).fetchall()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows]}


class UsuarioBody(BaseModel):
    username: str
    password: str
    role: str = "agente"
    linea_id: int | None = None


@router.post("/api/crm/usuarios")
async def api_usuario_crear(body: UsuarioBody, session_token: str | None = Cookie(default=None)):
    _require_admin(session_token)
    if body.role not in ("agente", "admin"):
        return {"ok": False, "error": "Rol inválido (agente o admin)"}
    try:
        uid = crear_usuario(body.username, body.password, role=body.role)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    if body.linea_id:
        conn = get_conn()
        try:
            conn.execute("UPDATE wa_lineas SET agente_user_id = ? WHERE id = ?", (uid, body.linea_id))
            conn.commit()
        finally:
            conn.close()
    return {"ok": True, "id": uid}


@router.post("/api/crm/usuarios/{user_id}/toggle")
async def api_usuario_toggle(user_id: int, session_token: str | None = Cookie(default=None)):
    admin = _require_admin(session_token)
    if admin["id"] == user_id:
        return {"ok": False, "error": "No puedes desactivarte a ti mismo"}
    conn = get_conn()
    try:
        row = conn.execute("SELECT active FROM panel_users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "Usuario no encontrado"}
        nuevo = 0 if row["active"] else 1
        conn.execute("UPDATE panel_users SET active = ? WHERE id = ?", (nuevo, user_id))
        if nuevo == 0:
            conn.execute("DELETE FROM panel_sessions WHERE user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "active": nuevo}


class PasswordBody(BaseModel):
    password: str


@router.post("/api/crm/usuarios/{user_id}/password")
async def api_usuario_password(
    user_id: int, body: PasswordBody, session_token: str | None = Cookie(default=None),
):
    _require_admin(session_token)
    conn = get_conn()
    try:
        row = conn.execute("SELECT username FROM panel_users WHERE id = ?", (user_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return {"ok": False, "error": "Usuario no encontrado"}
    try:
        cambiar_password(row["username"], body.password)
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
    return {"ok": True}


# =============================================================================
# Pages
# =============================================================================

def register_pages(app, templates) -> None:

    def _page(request: Request, template: str, page_key: str, session_token: str | None):
        user = validar_sesion(session_token)
        if not user:
            return RedirectResponse(url="/login", status_code=303)
        return templates.TemplateResponse(
            request=request, name=template,
            context={"user": user["username"], "page": page_key, "role": user.get("role", "admin")},
        )

    @app.get("/crm", response_class=HTMLResponse)
    async def page_crm(request: Request, session_token: str | None = Cookie(default=None)):
        return _page(request, "crm_inbox.html", "crm", session_token)

    @app.get("/crm/pipeline", response_class=HTMLResponse)
    async def page_crm_pipeline(request: Request, session_token: str | None = Cookie(default=None)):
        return _page(request, "crm_pipeline.html", "crm_pipeline", session_token)

    @app.get("/crm/usuarios", response_class=HTMLResponse)
    async def page_crm_usuarios(request: Request, session_token: str | None = Cookie(default=None)):
        user = validar_sesion(session_token)
        if not user:
            return RedirectResponse(url="/login", status_code=303)
        if user.get("role") not in ("admin", "dev"):
            return RedirectResponse(url="/crm", status_code=303)
        return _page(request, "crm_usuarios.html", "crm_usuarios", session_token)
