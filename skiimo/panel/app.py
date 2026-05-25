"""Panel web FastAPI para Skiimo. Corre en el mismo VM que el bot."""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Form, Request, HTTPException, Cookie, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from skiimo.bootstrap import ensure_db_ready
from skiimo.db.schema import get_conn
from skiimo.panel.auth import (
    autenticar, crear_sesion, validar_sesion, cerrar_sesion,
)


log = logging.getLogger("skiimo.panel")

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="Skiimo Panel", docs_url=None, redoc_url=None)

# Static
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.on_event("startup")
async def _startup() -> None:
    ensure_db_ready()
    log.info("Panel Skiimo arrancado")


def _user_or_redirect(session_token: str | None):
    """Devuelve el user dict o un RedirectResponse a /login."""
    user = validar_sesion(session_token)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
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
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    user = autenticar(username, password)
    if not user:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Usuario o contraseña incorrectos."},
            status_code=401,
        )
    ip = request.client.host if request.client else None
    token = crear_sesion(user["id"], ip=ip)
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24 * 7,  # 1 semana
    )
    return resp


@app.get("/logout")
async def logout(session_token: str | None = Cookie(default=None)):
    if session_token:
        cerrar_sesion(session_token)
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("session_token")
    return resp


# =============================================================================
# DASHBOARD
# =============================================================================

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, session_token: str | None = Cookie(default=None)):
    user = validar_sesion(session_token)
    if not user:
        return RedirectResponse(url="/login", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"user": user["username"]},
    )


# =============================================================================
# API: KPIs
# =============================================================================

@app.get("/api/kpis")
async def api_kpis(session_token: str | None = Cookie(default=None)):
    user = validar_sesion(session_token)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")

    # Pre-sync para datos en vivo
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
        # Ventas hoy
        r_hoy = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(total), 0) AS t "
            "FROM siigo_invoices WHERE date = ?",
            (hoy,),
        ).fetchone()
        # Ventas mes
        r_mes = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(total), 0) AS t "
            "FROM siigo_invoices WHERE date >= ?",
            (mes_inicio,),
        ).fetchone()
        # Por cobrar
        r_cob = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(balance), 0) AS t "
            "FROM siigo_invoices WHERE balance > 0"
        ).fetchone()
        # Por pagar
        r_pag = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(balance), 0) AS t "
            "FROM siigo_purchases WHERE balance > 0"
        ).fetchone()
    finally:
        conn.close()

    return {
        "ventas_hoy": float(r_hoy["t"] or 0),
        "ventas_hoy_count": int(r_hoy["n"] or 0),
        "ventas_mes": float(r_mes["t"] or 0),
        "ventas_mes_count": int(r_mes["n"] or 0),
        "por_cobrar": float(r_cob["t"] or 0),
        "por_cobrar_count": int(r_cob["n"] or 0),
        "por_pagar": float(r_pag["t"] or 0),
        "por_pagar_count": int(r_pag["n"] or 0),
    }


# =============================================================================
# API: Chat (reusa el agente del bot)
# =============================================================================

class ChatBody(BaseModel):
    message: str


@app.post("/api/chat")
async def api_chat(body: ChatBody, session_token: str | None = Cookie(default=None)):
    user = validar_sesion(session_token)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    msg = (body.message or "").strip()
    if not msg:
        return {"reply": ""}
    # Reusa el agente conversacional del bot. Usa user_id del panel como chat_id virtual.
    # Para no mezclar con el historial de Telegram, prefijamos con un offset alto.
    panel_chat_id = 9_000_000 + int(user["id"])
    try:
        from skiimo.llm.agent import process_message
        import asyncio
        reply = await asyncio.to_thread(
            process_message, panel_chat_id, msg, user_role="admin",
        )
    except Exception as e:
        log.exception("Error en agente para panel")
        return JSONResponse(
            {"reply": f"⚠ Error: {str(e)[:200]}"},
            status_code=200,
        )
    return {"reply": reply or "(sin respuesta)"}


@app.get("/healthz")
async def healthz():
    return {"ok": True}


def main():
    """Entry point para correr el panel directamente."""
    import uvicorn
    import os
    host = os.environ.get("PANEL_HOST", "0.0.0.0")
    port = int(os.environ.get("PANEL_PORT", "8080"))
    log_level = os.environ.get("PANEL_LOG_LEVEL", "info")
    uvicorn.run(
        "skiimo.panel.app:app",
        host=host, port=port, log_level=log_level,
    )


if __name__ == "__main__":
    main()
