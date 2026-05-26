"""Rutas del panel para asistencia. Se importan desde panel/app.py.

Vistas:
  GET  /asistencia          -> resumen de HOY (quien marco, llegadas tarde, etc.)
  GET  /empleados           -> CRUD de empleados
  GET  /quincena            -> resumen quincenal de horas

APIs JSON:
  GET  /api/asistencia/hoy
  GET  /api/asistencia/marcajes?desde=...&hasta=...&empleado_id=...
  GET  /api/empleados
  POST /api/empleados
  PUT  /api/empleados/{id}
  DELETE /api/empleados/{id}
  GET  /api/empleados/hik           -> personas registradas en el equipo Hikvision (para mapear)
  POST /api/asistencia/sync         -> ejecuta sync manual
  GET  /api/asistencia/quincena?fecha_inicio=YYYY-MM-DD
  GET  /api/asistencia/config
  PUT  /api/asistencia/config
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from skiimo.asistencia.config import DEFAULTS, get_all_conf, set_conf
from skiimo.asistencia.festivos import es_festivo
from skiimo.asistencia.horas import calcular_dia
from skiimo.asistencia.sync import sync_once
from skiimo.db.schema import get_conn
from skiimo.hikvision import TZ_BOGOTA, HikClient
from skiimo.panel.auth import validar_sesion

log = logging.getLogger("skiimo.panel.asistencia")

router = APIRouter()


def _require_user(session_token: str | None) -> dict:
    user = validar_sesion(session_token)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    return user


def _now_bogota() -> datetime:
    return datetime.now(TZ_BOGOTA)


# =============================================================================
# Pages (HTML)
# =============================================================================


def register_pages(app, templates) -> None:
    """Se llama desde app.py para registrar las rutas HTML."""

    @app.get("/asistencia", response_class=HTMLResponse)
    async def page_asistencia(request: Request, session_token: str | None = Cookie(default=None)):
        user = validar_sesion(session_token)
        if not user:
            return RedirectResponse(url="/login", status_code=303)
        return templates.TemplateResponse(
            request=request, name="asistencia.html",
            context={"user": user["username"], "page": "asistencia"},
        )

    @app.get("/empleados", response_class=HTMLResponse)
    async def page_empleados(request: Request, session_token: str | None = Cookie(default=None)):
        user = validar_sesion(session_token)
        if not user:
            return RedirectResponse(url="/login", status_code=303)
        return templates.TemplateResponse(
            request=request, name="empleados.html",
            context={"user": user["username"], "page": "empleados"},
        )

    @app.get("/quincena", response_class=HTMLResponse)
    async def page_quincena(request: Request, session_token: str | None = Cookie(default=None)):
        user = validar_sesion(session_token)
        if not user:
            return RedirectResponse(url="/login", status_code=303)
        return templates.TemplateResponse(
            request=request, name="quincena.html",
            context={"user": user["username"], "page": "quincena"},
        )

    @app.get("/asistencia/config", response_class=HTMLResponse)
    async def page_asistencia_config(request: Request, session_token: str | None = Cookie(default=None)):
        user = validar_sesion(session_token)
        if not user:
            return RedirectResponse(url="/login", status_code=303)
        return templates.TemplateResponse(
            request=request, name="asistencia_config.html",
            context={"user": user["username"], "page": "asistencia_config"},
        )


# =============================================================================
# APIs
# =============================================================================


@router.get("/api/asistencia/hoy")
async def api_asistencia_hoy(session_token: str | None = Cookie(default=None)):
    _require_user(session_token)
    # Sync oportunista (no bloqueante si falla)
    try:
        sync_once()
    except Exception:
        log.exception("Sync oportunista fallo")

    hoy = _now_bogota().date().isoformat()
    conn = get_conn()
    try:
        empleados = conn.execute(
            "SELECT id, nombre, cargo, hik_employee_no FROM empleados WHERE activo = 1 ORDER BY nombre"
        ).fetchall()
        marcajes_hoy = conn.execute(
            """SELECT m.*, e.nombre AS empleado_nombre
               FROM marcajes m
               LEFT JOIN empleados e ON e.id = m.empleado_id
               WHERE m.fecha = ?
               ORDER BY m.ts DESC""",
            (hoy,),
        ).fetchall()
    finally:
        conn.close()

    # Quien esta adentro (ultimo marcaje fue entrada o almuerzo_in)
    dentro_ids = set()
    primer_marcaje: dict[int, str] = {}
    ultimo_marcaje: dict[int, dict] = {}
    for m in marcajes_hoy:  # ya viene DESC
        eid = m["empleado_id"]
        if eid is None:
            continue
        if eid not in ultimo_marcaje:
            ultimo_marcaje[eid] = dict(m)
            if m["tipo"] in ("entrada", "almuerzo_in", "extra"):
                dentro_ids.add(eid)
        primer_marcaje[eid] = m["ts"]

    # Empleados que no marcaron hoy
    no_marcaron = [dict(e) for e in empleados if e["id"] not in primer_marcaje]
    dentro = [dict(e) for e in empleados if e["id"] in dentro_ids]
    afuera = [
        dict(e) for e in empleados
        if e["id"] in primer_marcaje and e["id"] not in dentro_ids
    ]

    return {
        "fecha": hoy,
        "total_empleados": len(empleados),
        "dentro": dentro,
        "afuera": afuera,
        "no_marcaron": no_marcaron,
        "marcajes": [dict(m) for m in marcajes_hoy[:50]],
    }


@router.get("/api/asistencia/marcajes")
async def api_marcajes(
    session_token: str | None = Cookie(default=None),
    desde: str = "",
    hasta: str = "",
    empleado_id: int | None = None,
    page: int = 1,
    page_size: int = 100,
):
    _require_user(session_token)
    where = []
    params: list[Any] = []
    if desde:
        where.append("m.fecha >= ?")
        params.append(desde)
    if hasta:
        where.append("m.fecha <= ?")
        params.append(hasta)
    if empleado_id:
        where.append("m.empleado_id = ?")
        params.append(empleado_id)
    sql = """SELECT m.*, e.nombre AS empleado_nombre
             FROM marcajes m
             LEFT JOIN empleados e ON e.id = m.empleado_id"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY m.ts DESC LIMIT ? OFFSET ?"
    params.append(page_size)
    params.append((page - 1) * page_size)
    conn = get_conn()
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows], "page": page, "page_size": page_size}


@router.post("/api/asistencia/sync")
async def api_sync(session_token: str | None = Cookie(default=None)):
    _require_user(session_token)
    summary = sync_once()
    return summary


# ----- Empleados -----


class EmpleadoIn(BaseModel):
    hik_employee_no: str | None = None
    cedula: str | None = None
    nombre: str
    cargo: str | None = None
    telegram_chat_id: str | None = None
    salario_mensual: float | None = None
    fecha_ingreso: str | None = None


@router.get("/api/empleados")
async def api_empleados_list(session_token: str | None = Cookie(default=None)):
    _require_user(session_token)
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT e.*,
                      (SELECT COUNT(*) FROM marcajes m WHERE m.empleado_id = e.id) AS total_marcajes,
                      (SELECT MAX(ts) FROM marcajes m WHERE m.empleado_id = e.id) AS ultimo_marcaje
               FROM empleados e
               ORDER BY e.activo DESC, e.nombre"""
        ).fetchall()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows]}


@router.get("/api/empleados/hik")
async def api_empleados_hik(session_token: str | None = Cookie(default=None)):
    """Lista las personas registradas en el equipo Hikvision (para mapear)."""
    _require_user(session_token)
    try:
        with HikClient() as hik:
            persons = list(hik.iter_persons())
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudo conectar al equipo: {e}")
    return {
        "items": [
            {
                "employeeNo": p.employee_no,
                "name": p.name,
                "userType": p.user_type,
                "gender": p.gender,
            }
            for p in persons
        ]
    }


@router.post("/api/empleados")
async def api_empleado_crear(body: EmpleadoIn, session_token: str | None = Cookie(default=None)):
    _require_user(session_token)
    now = datetime.utcnow().isoformat()
    # Valor hora calculado
    sal = body.salario_mensual or DEFAULTS["salario_minimo_2026"]
    valor_hora = round(sal / DEFAULTS["horas_legales_mes"])
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO empleados (hik_employee_no, cedula, nombre, cargo, telegram_chat_id,
                                       salario_mensual, valor_hora_ord, fecha_ingreso,
                                       activo, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (body.hik_employee_no, body.cedula, body.nombre, body.cargo, body.telegram_chat_id,
             sal, valor_hora, body.fecha_ingreso, now, now),
        )
        conn.commit()
        emp_id = cur.lastrowid
    finally:
        conn.close()
    return {"id": emp_id}


@router.put("/api/empleados/{emp_id}")
async def api_empleado_editar(emp_id: int, body: EmpleadoIn, session_token: str | None = Cookie(default=None)):
    _require_user(session_token)
    now = datetime.utcnow().isoformat()
    sal = body.salario_mensual or DEFAULTS["salario_minimo_2026"]
    valor_hora = round(sal / DEFAULTS["horas_legales_mes"])
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE empleados SET hik_employee_no=?, cedula=?, nombre=?, cargo=?,
                                     telegram_chat_id=?, salario_mensual=?, valor_hora_ord=?,
                                     fecha_ingreso=?, updated_at=?
               WHERE id = ?""",
            (body.hik_employee_no, body.cedula, body.nombre, body.cargo, body.telegram_chat_id,
             sal, valor_hora, body.fecha_ingreso, now, emp_id),
        )
        # Re-asignar marcajes huerfanos con el mismo hik_employee_no
        if body.hik_employee_no:
            conn.execute(
                "UPDATE marcajes SET empleado_id = ? WHERE empleado_id IS NULL AND hik_employee_no = ?",
                (emp_id, body.hik_employee_no),
            )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@router.delete("/api/empleados/{emp_id}")
async def api_empleado_borrar(emp_id: int, session_token: str | None = Cookie(default=None)):
    _require_user(session_token)
    conn = get_conn()
    try:
        # Soft delete: marcamos inactivo, conservamos historial
        conn.execute("UPDATE empleados SET activo = 0, updated_at = ? WHERE id = ?",
                     (datetime.utcnow().isoformat(), emp_id))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# ----- Quincena -----


@router.get("/api/asistencia/quincena")
async def api_quincena(
    session_token: str | None = Cookie(default=None),
    fecha_inicio: str = "",
):
    _require_user(session_token)
    if not fecha_inicio:
        # Por default: ultima quincena terminada o la actual
        hoy = _now_bogota().date()
        if hoy.day <= 15:
            inicio = hoy.replace(day=1)
        else:
            inicio = hoy.replace(day=16)
        fecha_inicio = inicio.isoformat()
    try:
        inicio = date.fromisoformat(fecha_inicio)
    except ValueError:
        raise HTTPException(status_code=400, detail="fecha_inicio invalida")

    if inicio.day == 1:
        fin = inicio.replace(day=15)
    elif inicio.day == 16:
        # Ultimo dia del mes
        if inicio.month == 12:
            fin = inicio.replace(year=inicio.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            fin = inicio.replace(month=inicio.month + 1, day=1) - timedelta(days=1)
    else:
        fin = inicio + timedelta(days=14)

    # Recolectar empleados activos
    conn = get_conn()
    try:
        empleados = conn.execute(
            "SELECT id, nombre, valor_hora_ord FROM empleados WHERE activo = 1 ORDER BY nombre"
        ).fetchall()

        resumen = []
        for emp in empleados:
            # Marcajes del periodo
            marc_rows = conn.execute(
                "SELECT ts FROM marcajes WHERE empleado_id = ? AND fecha BETWEEN ? AND ? ORDER BY ts",
                (emp["id"], inicio.isoformat(), fin.isoformat()),
            ).fetchall()
            # Agrupar por fecha
            por_dia: dict[str, list[datetime]] = {}
            for r in marc_rows:
                ts = datetime.fromisoformat(r["ts"])
                por_dia.setdefault(ts.date().isoformat(), []).append(ts)

            totales = {
                "ord_d": 0.0, "ord_n": 0.0, "ext_d": 0.0, "ext_n": 0.0,
                "dom_d": 0.0, "dom_n": 0.0, "dom_ext_d": 0.0, "dom_ext_n": 0.0,
                "minutos_tarde": 0, "dias_marcados": 0,
            }
            for fecha_str, lista in por_dia.items():
                fecha_dia = date.fromisoformat(fecha_str)
                t = calcular_dia(fecha_dia, sorted(lista))
                totales["ord_d"] += t.ordinarias_diurnas
                totales["ord_n"] += t.ordinarias_nocturnas
                totales["ext_d"] += t.extra_diurnas
                totales["ext_n"] += t.extra_nocturnas
                totales["dom_d"] += t.dom_fest_ord_diurnas
                totales["dom_n"] += t.dom_fest_ord_nocturnas
                totales["dom_ext_d"] += t.dom_fest_extra_diurnas
                totales["dom_ext_n"] += t.dom_fest_extra_nocturnas
                totales["minutos_tarde"] += t.minutos_tarde
                totales["dias_marcados"] += 1

            valor_h = emp["valor_hora_ord"] or 0
            t_total = sum([totales[k] for k in ("ord_d", "ord_n", "ext_d", "ext_n",
                                                 "dom_d", "dom_n", "dom_ext_d", "dom_ext_n")])
            # Valorizar
            pago = (
                totales["ord_d"] * valor_h * 1.00
                + totales["ord_n"] * valor_h * 1.35
                + totales["ext_d"] * valor_h * 1.25
                + totales["ext_n"] * valor_h * 1.75
                + totales["dom_d"] * valor_h * 1.75
                + totales["dom_n"] * valor_h * 2.10
                + totales["dom_ext_d"] * valor_h * 2.00
                + totales["dom_ext_n"] * valor_h * 2.50
            )
            resumen.append({
                "empleado_id": emp["id"],
                "nombre": emp["nombre"],
                "valor_hora": valor_h,
                "total_horas": round(t_total, 2),
                "ord_diurna": round(totales["ord_d"], 2),
                "ord_nocturna": round(totales["ord_n"], 2),
                "extra_diurna": round(totales["ext_d"], 2),
                "extra_nocturna": round(totales["ext_n"], 2),
                "dom_fest_ord": round(totales["dom_d"] + totales["dom_n"], 2),
                "dom_fest_extra": round(totales["dom_ext_d"] + totales["dom_ext_n"], 2),
                "minutos_tarde": totales["minutos_tarde"],
                "dias_marcados": totales["dias_marcados"],
                "pago_estimado": round(pago, 0),
            })
    finally:
        conn.close()

    return {
        "periodo": {"inicio": inicio.isoformat(), "fin": fin.isoformat()},
        "resumen": resumen,
        "total_pago": sum(r["pago_estimado"] for r in resumen),
    }


# ----- Config -----


@router.get("/api/asistencia/config")
async def api_config_get(session_token: str | None = Cookie(default=None)):
    _require_user(session_token)
    return {"config": get_all_conf(), "defaults": DEFAULTS}


@router.put("/api/asistencia/config")
async def api_config_set(payload: dict, session_token: str | None = Cookie(default=None)):
    _require_user(session_token)
    for k, v in payload.items():
        if k in DEFAULTS:
            set_conf(k, v)
    return {"ok": True, "config": get_all_conf()}


# ----- Health del equipo -----


@router.get("/api/asistencia/equipo/status")
async def api_equipo_status(session_token: str | None = Cookie(default=None)):
    _require_user(session_token)
    try:
        with HikClient() as hik:
            di = hik.device_info()
            t = hik.device_time()
            n = hik.count_persons()
        return {
            "online": True,
            "model": di.model,
            "serial": di.serial,
            "firmware": di.firmware,
            "mac": di.mac,
            "device_time": t.get("localTime"),
            "timezone": t.get("timeZone"),
            "personas_en_equipo": n,
        }
    except Exception as e:
        return {"online": False, "error": str(e)}
