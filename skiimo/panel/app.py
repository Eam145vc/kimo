"""Panel web FastAPI para Esskimo Cocktails. Corre en el mismo VM que el bot."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, Request, HTTPException, Cookie
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from skiimo.bootstrap import ensure_db_ready
from skiimo.db.schema import get_conn
from skiimo.panel.auth import (
    autenticar, crear_sesion, validar_sesion, cerrar_sesion,
)
from skiimo.panel.asistencia_routes import router as asistencia_router, register_pages as register_asistencia_pages


log = logging.getLogger("skiimo.panel")

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Esskimo Panel", docs_url=None, redoc_url=None)

_static_dir = BASE_DIR / "static"
_static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# Fotos de marcajes del Hikvision: guardadas en /data/photos (mismo volume que la DB)
from skiimo.config import DB_PATH as _DB_PATH
_fotos_dir = _DB_PATH.parent / "photos"
_fotos_dir.mkdir(parents=True, exist_ok=True)
app.mount("/photos", StaticFiles(directory=str(_fotos_dir)), name="photos")


@app.on_event("startup")
async def _startup() -> None:
    ensure_db_ready()
    log.info("Panel Esskimo arrancado")

    # Background: sync periodico de asistencia (Hikvision)
    from skiimo.config import HIK_ENABLED
    if HIK_ENABLED:
        asyncio.create_task(_loop_sync_asistencia())
        log.info("Loop de sync asistencia arrancado (cada 3 min)")


async def _loop_sync_asistencia() -> None:
    """Tarea background que jala marcajes del Hikvision cada N minutos."""
    from skiimo.asistencia.config import get_conf
    from skiimo.asistencia.sync import sync_once
    # Esperar 15s al arranque para no chocar con otros startups
    await asyncio.sleep(15)
    while True:
        try:
            summary = await asyncio.to_thread(sync_once)
            if summary.get("inserted"):
                log.info("Sync asistencia: %d nuevos marcajes", summary["inserted"])
            if summary.get("error"):
                log.warning("Sync asistencia error: %s", summary["error"])
        except Exception:
            log.exception("Error en loop_sync_asistencia")
        # Intervalo configurable
        try:
            interval_min = int(get_conf("sync_interval_minutes") or 3)
        except Exception:
            interval_min = 3
        await asyncio.sleep(max(60, interval_min * 60))


def _require_user(session_token: str | None) -> dict:
    user = validar_sesion(session_token)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    return user


# =============================================================================
# AUTH
# =============================================================================

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, session_token: str | None = Cookie(default=None)):
    if validar_sesion(session_token):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    user = autenticar(username, password)
    if not user:
        return templates.TemplateResponse(
            request=request, name="login.html",
            context={"error": "Usuario o contraseña incorrectos."},
            status_code=401,
        )
    ip = request.client.host if request.client else None
    token = crear_sesion(user["id"], ip=ip)
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie("session_token", token, httponly=True, samesite="lax", max_age=60*60*24*7)
    return resp


@app.get("/logout")
async def logout(session_token: str | None = Cookie(default=None)):
    if session_token:
        cerrar_sesion(session_token)
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("session_token")
    return resp


# =============================================================================
# PAGES (HTML)
# =============================================================================

def _render_page(request: Request, template: str, page_key: str, session_token: str | None):
    user = validar_sesion(session_token)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(
        request=request, name=template,
        context={"user": user["username"], "page": page_key},
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session_token: str | None = Cookie(default=None)):
    return _render_page(request, "dashboard.html", "home", session_token)


@app.get("/ventas", response_class=HTMLResponse)
async def page_ventas(request: Request, session_token: str | None = Cookie(default=None)):
    return _render_page(request, "ventas.html", "ventas", session_token)


@app.get("/compras", response_class=HTMLResponse)
async def page_compras(request: Request, session_token: str | None = Cookie(default=None)):
    return _render_page(request, "compras.html", "compras", session_token)


@app.get("/clientes", response_class=HTMLResponse)
async def page_clientes(request: Request, session_token: str | None = Cookie(default=None)):
    return _render_page(request, "clientes.html", "clientes", session_token)


@app.get("/proveedores", response_class=HTMLResponse)
async def page_proveedores(request: Request, session_token: str | None = Cookie(default=None)):
    return _render_page(request, "proveedores.html", "proveedores", session_token)


@app.get("/productos", response_class=HTMLResponse)
async def page_productos(request: Request, session_token: str | None = Cookie(default=None)):
    return _render_page(request, "productos.html", "productos", session_token)


@app.get("/equipo", response_class=HTMLResponse)
async def page_equipo(request: Request, session_token: str | None = Cookie(default=None)):
    return _render_page(request, "equipo.html", "equipo", session_token)


# Asistencia: registrar paginas HTML y API
register_asistencia_pages(app, templates)
app.include_router(asistencia_router)


# =============================================================================
# API: KPIs (home)
# =============================================================================

@app.get("/api/kpis")
async def api_kpis(session_token: str | None = Cookie(default=None)):
    _require_user(session_token)
    try:
        from skiimo.llm.tools import _sync_invoices_recientes, _sync_purchases_recientes
        _sync_invoices_recientes(dias=7)
        _sync_purchases_recientes(dias=14)
    except Exception:
        log.exception("Pre-sync KPIs fallo")

    today = date.today()
    mes_inicio = today.replace(day=1).isoformat()
    hoy = today.isoformat()

    conn = get_conn()
    try:
        r_hoy = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(total), 0) AS t FROM siigo_invoices WHERE date = ?", (hoy,)).fetchone()
        r_mes = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(total), 0) AS t FROM siigo_invoices WHERE date >= ?", (mes_inicio,)).fetchone()
        r_cob = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(balance), 0) AS t FROM siigo_invoices WHERE balance > 0").fetchone()
        r_pag = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(balance), 0) AS t FROM siigo_purchases WHERE balance > 0").fetchone()
    finally:
        conn.close()
    return {
        "ventas_hoy": float(r_hoy["t"] or 0), "ventas_hoy_count": int(r_hoy["n"] or 0),
        "ventas_mes": float(r_mes["t"] or 0), "ventas_mes_count": int(r_mes["n"] or 0),
        "por_cobrar": float(r_cob["t"] or 0), "por_cobrar_count": int(r_cob["n"] or 0),
        "por_pagar": float(r_pag["t"] or 0), "por_pagar_count": int(r_pag["n"] or 0),
    }


# =============================================================================
# API: Chat
# =============================================================================

class ChatBody(BaseModel):
    message: str


@app.post("/api/chat")
async def api_chat(body: ChatBody, session_token: str | None = Cookie(default=None)):
    user = _require_user(session_token)
    msg = (body.message or "").strip()
    if not msg:
        return {"reply": ""}
    panel_chat_id = 9_000_000 + int(user["id"])
    try:
        from skiimo.llm.agent import process_message
        agent_reply = await asyncio.to_thread(
            process_message, panel_chat_id, msg, user_role="admin",
        )
    except Exception as e:
        log.exception("Error en agente para panel")
        return JSONResponse({"reply": f"⚠ Error: {str(e)[:200]}"}, status_code=200)
    if agent_reply is None:
        texto = "(sin respuesta)"
    elif hasattr(agent_reply, "texto") and agent_reply.texto:
        texto = agent_reply.texto
    elif hasattr(agent_reply, "kind") and agent_reply.kind == "pedido":
        texto = ("📋 Detecté un pedido en tu mensaje. Los pedidos se "
                 "confirman desde Telegram para evitar duplicados.")
    else:
        texto = str(agent_reply) or "(sin respuesta)"
    return {"reply": texto}


# =============================================================================
# API: VENTAS
# =============================================================================

@app.get("/api/ventas")
async def api_ventas(
    session_token: str | None = Cookie(default=None),
    page: int = 1, page_size: int = 50,
    desde: str = "", hasta: str = "", estado: str = "", q: str = "",
):
    _require_user(session_token)
    try:
        from skiimo.llm.tools import _sync_invoices_recientes
        _sync_invoices_recientes(dias=7)
    except Exception:
        pass

    where = []
    params: list = []
    if desde:
        where.append("i.date >= ?"); params.append(desde)
    if hasta:
        where.append("i.date <= ?"); params.append(hasta)
    if estado == "pendiente":
        where.append("i.balance > 0")
    elif estado == "pagada":
        where.append("(i.balance IS NULL OR i.balance <= 0)")
    if q:
        like = f"%{q}%"
        where.append("(LOWER(i.name) LIKE LOWER(?) OR i.customer_ident LIKE ? OR LOWER(c.name) LIKE LOWER(?))")
        params.extend([like, like, like])
    where_sql = " WHERE " + " AND ".join(where) if where else ""

    offset = max(0, (page - 1) * page_size)
    conn = get_conn()
    try:
        rows = conn.execute(
            f"""SELECT i.id, i.name, i.date, i.total, i.balance, i.customer_ident,
                       c.name AS customer_name
                FROM siigo_invoices i
                LEFT JOIN siigo_customers c ON c.id = i.customer_id
                {where_sql}
                ORDER BY i.date DESC, i.id DESC
                LIMIT ? OFFSET ?""",
            (*params, page_size, offset),
        ).fetchall()
        tot = conn.execute(
            f"""SELECT COUNT(*) AS n, COALESCE(SUM(i.total),0) AS t
                FROM siigo_invoices i
                LEFT JOIN siigo_customers c ON c.id = i.customer_id
                {where_sql}""",
            params,
        ).fetchone()
    finally:
        conn.close()
    return {
        "items": [dict(r) for r in rows],
        "total": int(tot["n"] or 0),
        "total_monto": float(tot["t"] or 0),
    }


@app.get("/api/ventas/{invoice_id}")
async def api_venta_detalle(invoice_id: str, session_token: str | None = Cookie(default=None)):
    _require_user(session_token)
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT i.id, i.name, i.date, i.total, i.balance, i.customer_ident, i.observations,
                      i.items_json, i.payments_json, c.name AS customer_name
               FROM siigo_invoices i
               LEFT JOIN siigo_customers c ON c.id = i.customer_id
               WHERE i.id = ?""",
            (invoice_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Factura no encontrada")
    d = dict(row)
    d["items"] = json.loads(d.pop("items_json") or "[]")
    d["payments"] = json.loads(d.pop("payments_json") or "[]")
    return d


@app.get("/api/ventas/{invoice_id}/pdf")
async def api_venta_pdf(invoice_id: str, session_token: str | None = Cookie(default=None)):
    _require_user(session_token)
    try:
        from skiimo.siigo_writer import get_invoice_pdf
        pdf_bytes = await asyncio.to_thread(get_invoice_pdf, invoice_id)
    except Exception as e:
        log.exception("Error get_invoice_pdf")
        raise HTTPException(500, str(e))
    if not pdf_bytes:
        raise HTTPException(404, "PDF no disponible")
    return Response(content=pdf_bytes, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="factura-{invoice_id[:8]}.pdf"'})


class CobrarBody(BaseModel):
    monto: float
    metodo: str = "efectivo"


@app.post("/api/ventas/{invoice_id}/cobrar")
async def api_venta_cobrar(invoice_id: str, body: CobrarBody, session_token: str | None = Cookie(default=None)):
    user = _require_user(session_token)
    # Necesitamos factura_name y cliente_ident del espejo local
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT name, customer_ident, balance FROM siigo_invoices WHERE id = ?",
            (invoice_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"ok": False, "error": "Factura no encontrada"}
    saldo = float(row["balance"] or 0)
    es_completo = abs(body.monto - saldo) < 1.0
    try:
        from skiimo.siigo_payments import registrar_pago_completo, registrar_abono
        fn = registrar_pago_completo if es_completo else registrar_abono
        result = await asyncio.to_thread(
            fn, invoice_id, row["name"], row["customer_ident"],
            body.monto, body.metodo, None, f"panel:{user['username']}",
        )
    except Exception as e:
        log.exception("Cobro fallo")
        return {"ok": False, "error": str(e)[:300]}
    return {"ok": result.ok, "rc_name": getattr(result, "rc_name", None),
            "error": getattr(result, "error", None)}


@app.post("/api/ventas/{invoice_id}/anular")
async def api_venta_anular(invoice_id: str, session_token: str | None = Cookie(default=None)):
    user = _require_user(session_token)
    try:
        from skiimo.siigo_writer import anular_factura
        result = await asyncio.to_thread(anular_factura, invoice_id, f"panel:{user['username']}")
    except Exception as e:
        log.exception("Anular fallo")
        return {"ok": False, "error": str(e)[:300]}
    return {"ok": result.ok, "error": result.error}


# =============================================================================
# API: COMPRAS
# =============================================================================

@app.get("/api/compras")
async def api_compras(
    session_token: str | None = Cookie(default=None),
    page: int = 1, page_size: int = 50,
    desde: str = "", hasta: str = "", estado: str = "", q: str = "",
):
    _require_user(session_token)
    try:
        from skiimo.llm.tools import _sync_purchases_recientes
        _sync_purchases_recientes(dias=14)
    except Exception:
        pass

    where = []
    params: list = []
    if desde:
        where.append("p.date >= ?"); params.append(desde)
    if hasta:
        where.append("p.date <= ?"); params.append(hasta)
    if estado == "pendiente":
        where.append("p.balance > 0")
    elif estado == "pagada":
        where.append("(p.balance IS NULL OR p.balance <= 0)")
    if q:
        like = f"%{q}%"
        where.append("(LOWER(p.name) LIKE LOWER(?) OR p.supplier_ident LIKE ? OR LOWER(c.name) LIKE LOWER(?) OR p.provider_inv_number LIKE ?)")
        params.extend([like, like, like, like])
    where_sql = " WHERE " + " AND ".join(where) if where else ""

    offset = max(0, (page - 1) * page_size)
    conn = get_conn()
    try:
        rows = conn.execute(
            f"""SELECT p.id, p.name, p.date, p.total, p.balance, p.supplier_ident,
                       c.name AS supplier_name
                FROM siigo_purchases p
                LEFT JOIN siigo_customers c ON c.id = p.supplier_id
                {where_sql}
                ORDER BY p.date DESC, p.id DESC
                LIMIT ? OFFSET ?""",
            (*params, page_size, offset),
        ).fetchall()
        tot = conn.execute(
            f"""SELECT COUNT(*) AS n, COALESCE(SUM(p.total),0) AS t
                FROM siigo_purchases p
                LEFT JOIN siigo_customers c ON c.id = p.supplier_id
                {where_sql}""",
            params,
        ).fetchone()
    finally:
        conn.close()
    return {
        "items": [dict(r) for r in rows],
        "total": int(tot["n"] or 0),
        "total_monto": float(tot["t"] or 0),
    }


@app.get("/api/compras/{purchase_id}")
async def api_compra_detalle(purchase_id: str, session_token: str | None = Cookie(default=None)):
    _require_user(session_token)
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT p.id, p.name, p.date, p.total, p.balance, p.supplier_ident, p.observations,
                      p.provider_inv_prefix, p.provider_inv_number,
                      p.items_json, p.payments_json, c.name AS supplier_name
               FROM siigo_purchases p
               LEFT JOIN siigo_customers c ON c.id = p.supplier_id
               WHERE p.id = ?""",
            (purchase_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Compra no encontrada")
    d = dict(row)
    d["items"] = json.loads(d.pop("items_json") or "[]")
    d["payments"] = json.loads(d.pop("payments_json") or "[]")
    return d


class PagarBody(BaseModel):
    monto: float
    metodo: str = "banco_ahorros"


@app.post("/api/compras/{purchase_id}/pagar")
async def api_compra_pagar(purchase_id: str, body: PagarBody, session_token: str | None = Cookie(default=None)):
    user = _require_user(session_token)
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT name, supplier_ident FROM siigo_purchases WHERE id = ?",
            (purchase_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"ok": False, "error": "Compra no encontrada"}
    try:
        from skiimo.siigo_payments import registrar_pago_proveedor
        result = await asyncio.to_thread(
            registrar_pago_proveedor,
            purchase_id, row["name"], row["supplier_ident"],
            body.monto, body.metodo,
        )
    except Exception as e:
        log.exception("Pago fallo")
        return {"ok": False, "error": str(e)[:300]}
    return {"ok": result.ok, "rp_name": getattr(result, "rp_name", None),
            "error": getattr(result, "error", None)}


# =============================================================================
# API: CLIENTES
# =============================================================================

@app.get("/api/clientes")
async def api_clientes(
    session_token: str | None = Cookie(default=None),
    page: int = 1, page_size: int = 50, q: str = "", tipo: str = "",
):
    _require_user(session_token)
    where = ["(c.type = 'Customer' OR c.type IS NULL)", "c.active = 1"]
    params: list = []
    if q:
        like = f"%{q}%"
        where.append("(LOWER(c.name) LIKE LOWER(?) OR c.identification LIKE ?)")
        params.extend([like, like])
    where_sql = " WHERE " + " AND ".join(where)

    offset = max(0, (page - 1) * page_size)
    conn = get_conn()
    try:
        # Subquery saldo + ultima compra
        rows = conn.execute(
            f"""SELECT c.id, c.identification, c.name,
                       COALESCE((SELECT SUM(balance) FROM siigo_invoices WHERE customer_id = c.id AND balance > 0), 0) AS saldo,
                       (SELECT MAX(date) FROM siigo_invoices WHERE customer_id = c.id) AS ultima_compra
                FROM siigo_customers c
                {where_sql}
                {"HAVING saldo > 0" if tipo == "con_saldo" else ""}
                ORDER BY c.name
                LIMIT ? OFFSET ?""",
            (*params, page_size, offset),
        ).fetchall()
        tot = conn.execute(
            f"SELECT COUNT(*) AS n FROM siigo_customers c {where_sql}",
            params,
        ).fetchone()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows], "total": int(tot["n"] or 0)}


@app.get("/api/clientes/{customer_id}")
async def api_cliente_detalle(customer_id: str, session_token: str | None = Cookie(default=None)):
    _require_user(session_token)
    try:
        from skiimo.llm.tools import _sync_invoices_recientes
        _sync_invoices_recientes(dias=14)
    except Exception:
        pass
    conn = get_conn()
    try:
        c = conn.execute(
            "SELECT id, identification, name, email, phone, address FROM siigo_customers WHERE id = ?",
            (customer_id,),
        ).fetchone()
        if not c:
            raise HTTPException(404, "Cliente no encontrado")
        saldo = conn.execute(
            "SELECT COALESCE(SUM(balance), 0) AS s FROM siigo_invoices WHERE customer_id = ? AND balance > 0",
            (customer_id,),
        ).fetchone()
        total_12m = conn.execute(
            "SELECT COALESCE(SUM(total), 0) AS t FROM siigo_invoices WHERE customer_id = ? AND date >= date('now', '-365 days')",
            (customer_id,),
        ).fetchone()
        pendientes = conn.execute(
            """SELECT name, date, balance FROM siigo_invoices
               WHERE customer_id = ? AND balance > 0
               ORDER BY date ASC LIMIT 20""",
            (customer_id,),
        ).fetchall()
    finally:
        conn.close()
    return {
        "id": c["id"], "identification": c["identification"], "name": c["name"],
        "email": c["email"], "phone": c["phone"], "address": c["address"],
        "saldo": float(saldo["s"] or 0),
        "total_12m": float(total_12m["t"] or 0),
        "facturas_pendientes": [dict(r) for r in pendientes],
    }


# =============================================================================
# API: PROVEEDORES
# =============================================================================

@app.get("/api/proveedores")
async def api_proveedores(
    session_token: str | None = Cookie(default=None),
    page: int = 1, page_size: int = 50, q: str = "", tipo: str = "",
):
    _require_user(session_token)
    where = ["c.type = 'Supplier'", "c.active = 1"]
    params: list = []
    if q:
        like = f"%{q}%"
        where.append("(LOWER(c.name) LIKE LOWER(?) OR c.identification LIKE ?)")
        params.extend([like, like])
    where_sql = " WHERE " + " AND ".join(where)

    offset = max(0, (page - 1) * page_size)
    conn = get_conn()
    try:
        rows = conn.execute(
            f"""SELECT c.id, c.identification, c.name,
                       COALESCE((SELECT SUM(balance) FROM siigo_purchases WHERE supplier_id = c.id AND balance > 0), 0) AS saldo,
                       (SELECT MAX(date) FROM siigo_purchases WHERE supplier_id = c.id) AS ultima_compra
                FROM siigo_customers c
                {where_sql}
                {"HAVING saldo > 0" if tipo == "con_saldo" else ""}
                ORDER BY c.name
                LIMIT ? OFFSET ?""",
            (*params, page_size, offset),
        ).fetchall()
        tot = conn.execute(
            f"SELECT COUNT(*) AS n FROM siigo_customers c {where_sql}",
            params,
        ).fetchone()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows], "total": int(tot["n"] or 0)}


@app.get("/api/proveedores/{supplier_id}")
async def api_proveedor_detalle(supplier_id: str, session_token: str | None = Cookie(default=None)):
    _require_user(session_token)
    try:
        from skiimo.llm.tools import _sync_purchases_recientes
        _sync_purchases_recientes(dias=30)
    except Exception:
        pass
    conn = get_conn()
    try:
        s = conn.execute(
            "SELECT id, identification, name, email, phone, address FROM siigo_customers WHERE id = ?",
            (supplier_id,),
        ).fetchone()
        if not s:
            raise HTTPException(404, "Proveedor no encontrado")
        saldo = conn.execute(
            "SELECT COALESCE(SUM(balance), 0) AS s FROM siigo_purchases WHERE supplier_id = ? AND balance > 0",
            (supplier_id,),
        ).fetchone()
        total_12m = conn.execute(
            "SELECT COALESCE(SUM(total), 0) AS t FROM siigo_purchases WHERE supplier_id = ? AND date >= date('now', '-365 days')",
            (supplier_id,),
        ).fetchone()
        pendientes = conn.execute(
            """SELECT name, date, balance FROM siigo_purchases
               WHERE supplier_id = ? AND balance > 0
               ORDER BY date ASC LIMIT 20""",
            (supplier_id,),
        ).fetchall()
    finally:
        conn.close()
    return {
        "id": s["id"], "identification": s["identification"], "name": s["name"],
        "email": s["email"], "phone": s["phone"], "address": s["address"],
        "saldo": float(saldo["s"] or 0),
        "total_12m": float(total_12m["t"] or 0),
        "facturas_pendientes": [dict(r) for r in pendientes],
    }


# =============================================================================
# API: PRODUCTOS
# =============================================================================

@app.get("/api/productos")
async def api_productos(
    session_token: str | None = Cookie(default=None),
    page: int = 1, page_size: int = 50, q: str = "",
):
    _require_user(session_token)
    where = ["(active IS NULL OR active = 1)"]
    params: list = []
    if q:
        like = f"%{q}%"
        where.append("(LOWER(code) LIKE LOWER(?) OR LOWER(name) LIKE LOWER(?))")
        params.extend([like, like])
    where_sql = " WHERE " + " AND ".join(where) if where else ""

    offset = max(0, (page - 1) * page_size)
    conn = get_conn()
    try:
        rows = conn.execute(
            f"""SELECT id, code, name, price_default, iva_percentage
                FROM siigo_products {where_sql}
                ORDER BY code LIMIT ? OFFSET ?""",
            (*params, page_size, offset),
        ).fetchall()
        tot = conn.execute(
            f"SELECT COUNT(*) AS n FROM siigo_products {where_sql}",
            params,
        ).fetchone()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows], "total": int(tot["n"] or 0)}


@app.get("/api/productos/{product_id}")
async def api_producto_detalle(product_id: str, session_token: str | None = Cookie(default=None)):
    _require_user(session_token)
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT id, code, name, price_default, iva_percentage, account_group_name
               FROM siigo_products WHERE id = ?""",
            (product_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404, "Producto no encontrado")
    d = dict(row)
    # Precios por categoria (motor de precios)
    try:
        from skiimo.pricing.engine import sugerir_precio
        cats = []
        for cat_name, cat_id in [("DETAL", None), ("MAYORISTA", None), ("DISTRIBUIDOR", None)]:
            # sugerir_precio toma customer_id. Sin un cliente representativo no podemos calcular por categoria
            # exacta. Lo dejamos para una version siguiente.
            pass
        d["precios_categoria"] = cats
    except Exception:
        d["precios_categoria"] = []
    return d


# =============================================================================
# API: EQUIPO (usuarios del bot Telegram)
# =============================================================================

@app.get("/api/equipo")
async def api_equipo_list(session_token: str | None = Cookie(default=None)):
    _require_user(session_token)
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT telegram_chat_id, nombre, siigo_seller_id, rol, activo, created_at
               FROM bot_vendedores ORDER BY rol DESC, nombre"""
        ).fetchall()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows]}


class EquipoBody(BaseModel):
    nombre: str
    telegram_chat_id: int
    rol: str = "vendedor"
    siigo_seller_id: int = 341


@app.post("/api/equipo")
async def api_equipo_create(body: EquipoBody, session_token: str | None = Cookie(default=None)):
    user = _require_user(session_token)
    nombre = body.nombre.strip()
    if not nombre or len(nombre) < 2:
        return {"ok": False, "error": "Nombre requerido (al menos 2 caracteres)"}
    if not body.telegram_chat_id:
        return {"ok": False, "error": "Chat ID requerido"}
    if body.rol not in ("vendedor", "admin"):
        return {"ok": False, "error": "Rol invalido. Debe ser 'vendedor' o 'admin'"}

    from datetime import datetime
    conn = get_conn()
    try:
        # Verificar si ya existe
        existing = conn.execute(
            "SELECT telegram_chat_id, nombre FROM bot_vendedores WHERE telegram_chat_id = ?",
            (body.telegram_chat_id,),
        ).fetchone()
        if existing:
            return {"ok": False, "error": f"Ya existe un usuario con chat_id {body.telegram_chat_id}: {existing['nombre']}"}
        conn.execute(
            """INSERT INTO bot_vendedores (telegram_chat_id, nombre, siigo_seller_id, rol, activo, created_at)
               VALUES (?, ?, ?, ?, 1, ?)""",
            (
                body.telegram_chat_id, nombre, body.siigo_seller_id, body.rol,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    except Exception as e:
        log.exception("Error creando usuario bot")
        return {"ok": False, "error": str(e)[:300]}
    finally:
        conn.close()
    log.info("Panel %s creo usuario bot: %s (chat_id=%s, rol=%s)",
             user["username"], nombre, body.telegram_chat_id, body.rol)
    return {"ok": True}


@app.post("/api/equipo/{chat_id}/toggle")
async def api_equipo_toggle(chat_id: int, session_token: str | None = Cookie(default=None)):
    user = _require_user(session_token)
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT nombre, activo FROM bot_vendedores WHERE telegram_chat_id = ?",
            (chat_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "error": "Usuario no encontrado"}
        nuevo_estado = 0 if row["activo"] else 1
        conn.execute(
            "UPDATE bot_vendedores SET activo = ? WHERE telegram_chat_id = ?",
            (nuevo_estado, chat_id),
        )
        conn.commit()
    except Exception as e:
        log.exception("Error toggle usuario bot")
        return {"ok": False, "error": str(e)[:300]}
    finally:
        conn.close()
    log.info("Panel %s cambio estado de usuario bot chat_id=%s: activo=%s",
             user["username"], chat_id, nuevo_estado)
    return {"ok": True, "activo": nuevo_estado}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


def main():
    import uvicorn, os
    host = os.environ.get("PANEL_HOST", "0.0.0.0")
    port = int(os.environ.get("PANEL_PORT", "8080"))
    log_level = os.environ.get("PANEL_LOG_LEVEL", "info")
    uvicorn.run("skiimo.panel.app:app", host=host, port=port, log_level=log_level)


if __name__ == "__main__":
    main()
