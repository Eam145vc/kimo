"""Rutas del panel para asistencia. Se importan desde panel/app.py.

Vistas:
  GET  /asistencia          -> resumen de HOY (quien marco, llegadas tarde, etc.)
  GET  /empleados           -> CRUD de empleados
  GET  /quincena            -> resumen quincenal de horas
  GET  /asistencia/config   -> configuracion editable

APIs JSON (autenticadas):
  GET  /api/asistencia/hoy
  GET  /api/asistencia/marcajes?desde=...&hasta=...&empleado_id=...
  GET  /api/empleados
  POST /api/empleados
  PUT  /api/empleados/{id}
  DELETE /api/empleados/{id}
  GET  /api/empleados/hik
  POST /api/asistencia/sync
  GET  /api/asistencia/quincena?fecha_inicio=YYYY-MM-DD
  GET  /api/asistencia/config
  PUT  /api/asistencia/config
  GET  /api/asistencia/equipo/status

Endpoint publico (recibe push del Hikvision via ISUP Listening / httpHosts):
  POST /api/hik/event
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

    @app.get("/equipo-hikvision", response_class=HTMLResponse)
    async def page_equipo_hikvision(request: Request, session_token: str | None = Cookie(default=None)):
        user = validar_sesion(session_token)
        if not user:
            return RedirectResponse(url="/login", status_code=303)
        return templates.TemplateResponse(
            request=request, name="equipo_hikvision.html",
            context={"user": user["username"], "page": "equipo_hikvision"},
        )

    @app.get("/plantillas", response_class=HTMLResponse)
    async def page_plantillas(request: Request, session_token: str | None = Cookie(default=None)):
        user = validar_sesion(session_token)
        if not user:
            return RedirectResponse(url="/login", status_code=303)
        return templates.TemplateResponse(
            request=request, name="plantillas.html",
            context={"user": user["username"], "page": "plantillas"},
        )

    @app.get("/excepciones", response_class=HTMLResponse)
    async def page_excepciones(request: Request, session_token: str | None = Cookie(default=None)):
        user = validar_sesion(session_token)
        if not user:
            return RedirectResponse(url="/login", status_code=303)
        return templates.TemplateResponse(
            request=request, name="excepciones.html",
            context={"user": user["username"], "page": "excepciones"},
        )


# =============================================================================
# APIs
# =============================================================================


@router.get("/api/asistencia/resumen-diario")
async def api_resumen_diario(
    session_token: str | None = Cookie(default=None),
    empleado_id: int | None = None,
    desde: str = "",
    hasta: str = "",
):
    """Fase 1: Resumen simple por dia y empleado.

    Devuelve para cada (empleado, fecha) en el rango:
      - primera entrada (HH:MM)
      - ultima salida (HH:MM)
      - cantidad de marcajes
      - horas trabajadas (ultima - primera, sin descontar almuerzo)

    Filtros opcionales:
      - empleado_id: solo ese empleado
      - desde/hasta: rango de fechas (YYYY-MM-DD)

    No hace calculo de horas extra ni recargos. Para eso ver fase 2.
    """
    _require_user(session_token)

    # Sync oportunista al abrir la pagina
    try:
        sync_once()
    except Exception:
        log.exception("Sync oportunista fallo")

    where = ["m.empleado_id IS NOT NULL"]
    params: list[Any] = []
    if empleado_id:
        where.append("m.empleado_id = ?")
        params.append(empleado_id)
    if desde:
        where.append("m.fecha >= ?")
        params.append(desde)
    if hasta:
        where.append("m.fecha <= ?")
        params.append(hasta)

    # Obtener todos los marcajes del rango con todos sus timestamps,
    # para poder inferir entrada/almuerzo/salida cuando hay 3 o 4 marcajes en el dia.
    sql = f"""
        SELECT
            m.empleado_id,
            e.nombre AS empleado_nombre,
            m.fecha,
            m.ts
        FROM marcajes m
        JOIN empleados e ON e.id = m.empleado_id
        WHERE {' AND '.join(where)}
        ORDER BY m.empleado_id, m.fecha, m.ts
    """

    conn = get_conn()
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.close()

    # Agrupar por (empleado_id, fecha)
    grupos: dict[tuple[int, str], dict] = {}
    for r in rows:
        key = (r["empleado_id"], r["fecha"])
        if key not in grupos:
            grupos[key] = {
                "empleado_id": r["empleado_id"],
                "empleado_nombre": r["empleado_nombre"],
                "fecha": r["fecha"],
                "ts_list": [],
            }
        grupos[key]["ts_list"].append(r["ts"])

    def _fmt(ts_iso: str) -> str | None:
        try:
            return datetime.fromisoformat(ts_iso).strftime("%H:%M:%S")
        except Exception:
            return None

    items = []
    for (eid, fecha), g in grupos.items():
        ts_list = g["ts_list"]
        n = len(ts_list)
        # Etiquetar segun cantidad
        primera_entrada = _fmt(ts_list[0]) if n >= 1 else None
        ultima_salida = _fmt(ts_list[-1]) if n >= 2 else None
        almuerzo_out = None
        almuerzo_in = None
        if n == 3:
            # Entrada, salida-almuerzo, regreso-almuerzo (falta salida final)
            almuerzo_out = _fmt(ts_list[1])
            almuerzo_in = _fmt(ts_list[2])
            ultima_salida = None
        elif n == 4:
            almuerzo_out = _fmt(ts_list[1])
            almuerzo_in = _fmt(ts_list[2])
            ultima_salida = _fmt(ts_list[3])
        elif n >= 5:
            # Caso raro: marcajes adicionales. Tomamos primer/ultimo como entrada/salida.
            almuerzo_out = _fmt(ts_list[1])
            almuerzo_in = _fmt(ts_list[2])
            ultima_salida = _fmt(ts_list[-1])

        # Calculo de horas
        horas = None
        horas_almuerzo = None
        try:
            if n >= 2:
                t_in = datetime.fromisoformat(ts_list[0])
                t_out = datetime.fromisoformat(ts_list[-1])
                bruto = (t_out - t_in).total_seconds() / 3600.0
                horas = round(bruto, 2)
                # Si hubo almuerzo registrado, descontarlo
                if n >= 4:
                    t_alm_out = datetime.fromisoformat(ts_list[1])
                    t_alm_in = datetime.fromisoformat(ts_list[2])
                    pausa = (t_alm_in - t_alm_out).total_seconds() / 3600.0
                    horas_almuerzo = round(pausa, 2)
                    horas = round(bruto - pausa, 2)
        except Exception:
            pass

        items.append({
            "empleado_id": g["empleado_id"],
            "empleado_nombre": g["empleado_nombre"],
            "fecha": fecha,
            "primera_entrada": primera_entrada,
            "almuerzo_out": almuerzo_out,
            "almuerzo_in": almuerzo_in,
            "ultima_salida": ultima_salida,
            "horas": horas,
            "horas_almuerzo": horas_almuerzo,
            "cantidad_marcajes": n,
        })

    # Orden: fecha desc, nombre asc
    items.sort(key=lambda x: (x["fecha"], x["empleado_nombre"]), reverse=False)
    items.sort(key=lambda x: x["fecha"], reverse=True)

    return {"items": items}


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
    plantilla_id: int | None = None


class PlantillaIn(BaseModel):
    nombre: str
    descripcion: str | None = None
    hora_entrada: str            # "07:00"
    hora_salida: str             # "16:00"
    almuerzo_inicio: str | None = None
    almuerzo_fin: str | None = None
    almuerzo_minutos_auto: int = 60
    dias_semana: str             # "1,2,3,4,5"
    tolerancia_entrada_min: int = 10
    activa: bool = True


class MarcajeIn(BaseModel):
    """Para crear marcaje manual o editar uno existente."""
    empleado_id: int
    ts: str                      # ISO-8601 con tz
    tipo: str | None = None      # entrada | salida | almuerzo_in | almuerzo_out
    nota_admin: str | None = None
    ignorar_nomina: bool = False


class ExcepcionIn(BaseModel):
    empleado_id: int
    fecha_desde: str             # YYYY-MM-DD
    fecha_hasta: str
    tipo: str                    # permiso | vacaciones | incapacidad | ausencia_justificada | etc.
    horas_ajuste: float = 0
    paga: bool = True
    motivo: str | None = None


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
    """Lista las personas registradas en el equipo Hikvision (para mapear).

    Solo funciona si la VM tiene HIK_HOST configurado y alcanza al equipo
    directamente (LAN). En despliegue normal (equipo detras de NAT) la VM
    NO puede llegar, hay que usar POST /api/empleados/sync-hik desde el navegador.
    """
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


class HikPersonImport(BaseModel):
    employeeNo: str
    name: str | None = None
    department: str | None = None
    gender: str | None = None
    cardNo: str | None = None
    has_face: bool = False
    has_fingerprint: bool = False


class SyncHikPayload(BaseModel):
    persons: list[HikPersonImport]


class SyncHikFromDevice(BaseModel):
    """El admin manda credenciales para que el PANEL (no el navegador) jale del equipo.

    Solo funciona cuando el admin esta en la misma red que el equipo y la VM
    NO. Pero entonces como funciona? El usuario apreta en su navegador, el
    request llega al PANEL en la VM, y el panel intenta llegar al equipo.
    Solo funciona si la VM esta en la misma LAN.

    Alternativa real: hacer fetch DIRECTO desde el navegador al equipo,
    y mandar el resultado al panel. Por eso este endpoint NO se usa: ver
    /api/empleados/sync-hik que recibe el payload ya extraido.
    """
    ip: str
    port: int = 80
    user: str = "admin"
    password: str


@router.post("/api/empleados/sync-hik")
async def api_empleados_sync_hik(
    payload: SyncHikPayload,
    session_token: str | None = Cookie(default=None),
):
    """Recibe la lista de personas desde el navegador del usuario.

    El navegador (que SI esta en la red del equipo Hikvision) llamo antes a
    `http://<ip-equipo>/ISAPI/AccessControl/UserInfo/Search` y nos pasa el
    JSON ya parseado aca para que lo guardemos.

    Comportamiento:
      - Si existe empleado con ese hik_employee_no -> actualiza nombre/cargo si vinieron.
      - Si NO existe -> crea con defaults.
      - Mapea marcajes huerfanos que tengan ese hik_employee_no.
    """
    _require_user(session_token)
    from skiimo.asistencia.config import DEFAULTS

    now = datetime.utcnow().isoformat()
    sal_default = DEFAULTS["salario_minimo_2026"]
    valor_hora_default = round(sal_default / DEFAULTS["horas_legales_mes"])

    creados = 0
    actualizados = 0
    marcajes_mapeados = 0

    conn = get_conn()
    try:
        for p in payload.persons:
            if not p.employeeNo:
                continue
            nombre = (p.name or f"Empleado #{p.employeeNo}").strip()
            if nombre.islower():
                nombre = " ".join(w.capitalize() for w in nombre.split())

            row = conn.execute(
                "SELECT id FROM empleados WHERE hik_employee_no = ?", (p.employeeNo,)
            ).fetchone()

            if row:
                # Actualizar nombre solo si esta vacio o decia 'Pendiente revision'
                conn.execute(
                    """UPDATE empleados SET
                         nombre = CASE
                            WHEN nombre IS NULL OR nombre = '' OR nombre LIKE 'Empleado #%' THEN ?
                            ELSE nombre
                         END,
                         cargo = COALESCE(NULLIF(cargo, 'Pendiente revision'), ?),
                         updated_at = ?
                       WHERE id = ?""",
                    (nombre, p.department or "Pendiente revision", now, row["id"]),
                )
                actualizados += 1
                emp_id = row["id"]
            else:
                cur = conn.execute(
                    """INSERT INTO empleados (hik_employee_no, nombre, cargo,
                                               salario_mensual, valor_hora_ord,
                                               activo, observaciones, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                    (
                        p.employeeNo,
                        nombre,
                        p.department or "Pendiente",
                        sal_default,
                        valor_hora_default,
                        "sincronizado desde equipo Hikvision",
                        now,
                        now,
                    ),
                )
                creados += 1
                emp_id = cur.lastrowid

            # Mapear marcajes huerfanos
            cur = conn.execute(
                """UPDATE marcajes SET empleado_id = ?
                   WHERE empleado_id IS NULL AND hik_employee_no = ?""",
                (emp_id, p.employeeNo),
            )
            marcajes_mapeados += cur.rowcount

        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "creados": creados,
        "actualizados": actualizados,
        "marcajes_mapeados": marcajes_mapeados,
        "total_procesados": len(payload.persons),
    }


@router.post("/api/empleados")
async def api_empleado_crear(body: EmpleadoIn, session_token: str | None = Cookie(default=None)):
    _require_user(session_token)
    now = datetime.utcnow().isoformat()
    # Valor hora: solo si el salario esta seteado
    sal = body.salario_mensual
    valor_hora = round(sal / DEFAULTS["horas_legales_mes"]) if sal else None
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO empleados (hik_employee_no, cedula, nombre, cargo, telegram_chat_id,
                                       salario_mensual, valor_hora_ord, fecha_ingreso, plantilla_id,
                                       activo, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (body.hik_employee_no, body.cedula, body.nombre, body.cargo, body.telegram_chat_id,
             sal, valor_hora, body.fecha_ingreso, body.plantilla_id, now, now),
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
    sal = body.salario_mensual
    valor_hora = round(sal / DEFAULTS["horas_legales_mes"]) if sal else None
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE empleados SET hik_employee_no=?, cedula=?, nombre=?, cargo=?,
                                     telegram_chat_id=?, salario_mensual=?, valor_hora_ord=?,
                                     fecha_ingreso=?, plantilla_id=?, updated_at=?
               WHERE id = ?""",
            (body.hik_employee_no, body.cedula, body.nombre, body.cargo, body.telegram_chat_id,
             sal, valor_hora, body.fecha_ingreso, body.plantilla_id, now, emp_id),
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


# =============================================================================
# Plantillas de turno
# =============================================================================


@router.get("/api/plantillas")
async def api_plantillas_list(session_token: str | None = Cookie(default=None)):
    _require_user(session_token)
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT p.*,
                      (SELECT COUNT(*) FROM empleados e WHERE e.plantilla_id = p.id AND e.activo = 1) AS empleados_count
               FROM plantillas_turno p
               ORDER BY p.activa DESC, p.nombre"""
        ).fetchall()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows]}


@router.post("/api/plantillas")
async def api_plantilla_crear(body: PlantillaIn, session_token: str | None = Cookie(default=None)):
    _require_user(session_token)
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO plantillas_turno (nombre, descripcion, hora_entrada, hora_salida,
                                              almuerzo_inicio, almuerzo_fin, almuerzo_minutos_auto,
                                              dias_semana, tolerancia_entrada_min, activa,
                                              created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (body.nombre, body.descripcion, body.hora_entrada, body.hora_salida,
             body.almuerzo_inicio, body.almuerzo_fin, body.almuerzo_minutos_auto,
             body.dias_semana, body.tolerancia_entrada_min,
             1 if body.activa else 0, now, now),
        )
        conn.commit()
        pid = cur.lastrowid
    finally:
        conn.close()
    return {"id": pid}


@router.put("/api/plantillas/{plantilla_id}")
async def api_plantilla_editar(plantilla_id: int, body: PlantillaIn,
                                session_token: str | None = Cookie(default=None)):
    _require_user(session_token)
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    try:
        conn.execute(
            """UPDATE plantillas_turno SET
                  nombre=?, descripcion=?, hora_entrada=?, hora_salida=?,
                  almuerzo_inicio=?, almuerzo_fin=?, almuerzo_minutos_auto=?,
                  dias_semana=?, tolerancia_entrada_min=?, activa=?, updated_at=?
               WHERE id = ?""",
            (body.nombre, body.descripcion, body.hora_entrada, body.hora_salida,
             body.almuerzo_inicio, body.almuerzo_fin, body.almuerzo_minutos_auto,
             body.dias_semana, body.tolerancia_entrada_min,
             1 if body.activa else 0, now, plantilla_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@router.delete("/api/plantillas/{plantilla_id}")
async def api_plantilla_borrar(plantilla_id: int, session_token: str | None = Cookie(default=None)):
    _require_user(session_token)
    conn = get_conn()
    try:
        # Marcar empleados que la usan como sin plantilla
        conn.execute("UPDATE empleados SET plantilla_id = NULL WHERE plantilla_id = ?", (plantilla_id,))
        conn.execute("DELETE FROM plantillas_turno WHERE id = ?", (plantilla_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


# =============================================================================
# Correccion de marcajes
# =============================================================================


@router.post("/api/marcajes")
async def api_marcaje_crear_manual(body: MarcajeIn, session_token: str | None = Cookie(default=None)):
    """Crea un marcaje manual (cuando el equipo no lo registro o el admin
    necesita agregar uno historico). origen='manual', metodo='manual'."""
    user = _require_user(session_token)
    now = datetime.utcnow().isoformat()
    try:
        ts = datetime.fromisoformat(body.ts)
    except ValueError:
        raise HTTPException(400, "ts invalido. Use ISO-8601, ej: 2026-05-26T07:00:00-05:00")
    fecha = ts.date().isoformat()

    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO marcajes (empleado_id, ts, fecha, tipo, metodo, origen,
                                      editado, editado_por, editado_at, nota_admin,
                                      ignorar_nomina, created_at)
               VALUES (?, ?, ?, ?, 'manual', 'manual', 1, ?, ?, ?, ?, ?)""",
            (body.empleado_id, body.ts, fecha, body.tipo,
             user.get("username", "admin"), now, body.nota_admin,
             1 if body.ignorar_nomina else 0, now),
        )
        conn.commit()
        mid = cur.lastrowid
    finally:
        conn.close()
    return {"id": mid}


@router.put("/api/marcajes/{marcaje_id}")
async def api_marcaje_editar(marcaje_id: int, body: MarcajeIn,
                              session_token: str | None = Cookie(default=None)):
    """Edita un marcaje existente (cambiar hora, tipo, o marcar ignorar_nomina).
    Marca origen='corregido' y editado=1.
    """
    user = _require_user(session_token)
    now = datetime.utcnow().isoformat()
    try:
        ts = datetime.fromisoformat(body.ts)
    except ValueError:
        raise HTTPException(400, "ts invalido")
    fecha = ts.date().isoformat()

    conn = get_conn()
    try:
        # Verificar que existe
        row = conn.execute("SELECT origen FROM marcajes WHERE id = ?", (marcaje_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Marcaje no encontrado")
        nuevo_origen = "manual" if row["origen"] == "manual" else "corregido"

        conn.execute(
            """UPDATE marcajes SET
                  ts=?, fecha=?, tipo=COALESCE(?, tipo),
                  origen=?, editado=1, editado_por=?, editado_at=?,
                  nota_admin=COALESCE(?, nota_admin),
                  ignorar_nomina=?
               WHERE id = ?""",
            (body.ts, fecha, body.tipo, nuevo_origen,
             user.get("username", "admin"), now,
             body.nota_admin, 1 if body.ignorar_nomina else 0, marcaje_id),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@router.delete("/api/marcajes/{marcaje_id}")
async def api_marcaje_borrar(marcaje_id: int, session_token: str | None = Cookie(default=None)):
    """Borra un marcaje. Solo permitido si es manual; si es del Hikvision,
    en lugar de borrar marcamos ignorar_nomina=1 para mantener trazabilidad."""
    _require_user(session_token)
    conn = get_conn()
    try:
        row = conn.execute("SELECT origen FROM marcajes WHERE id = ?", (marcaje_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Marcaje no encontrado")
        if row["origen"] == "manual":
            conn.execute("DELETE FROM marcajes WHERE id = ?", (marcaje_id,))
        else:
            # Marcajes del equipo: soft delete (ignorar para nomina)
            conn.execute(
                "UPDATE marcajes SET ignorar_nomina = 1 WHERE id = ?", (marcaje_id,)
            )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "soft_delete": row["origen"] != "manual"}


# =============================================================================
# Excepciones (permisos, vacaciones, ajustes)
# =============================================================================


@router.get("/api/excepciones")
async def api_excepciones_list(
    session_token: str | None = Cookie(default=None),
    empleado_id: int | None = None,
    desde: str = "",
    hasta: str = "",
):
    _require_user(session_token)
    sql = """SELECT x.*, e.nombre AS empleado_nombre
             FROM excepciones_asistencia x
             JOIN empleados e ON e.id = x.empleado_id
             WHERE 1=1"""
    params: list[Any] = []
    if empleado_id:
        sql += " AND x.empleado_id = ?"
        params.append(empleado_id)
    if desde:
        sql += " AND x.fecha_hasta >= ?"
        params.append(desde)
    if hasta:
        sql += " AND x.fecha_desde <= ?"
        params.append(hasta)
    sql += " ORDER BY x.fecha_desde DESC, x.id DESC"
    conn = get_conn()
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows]}


@router.post("/api/excepciones")
async def api_excepcion_crear(body: ExcepcionIn,
                                session_token: str | None = Cookie(default=None)):
    user = _require_user(session_token)
    now = datetime.utcnow().isoformat()
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO excepciones_asistencia
                (empleado_id, fecha_desde, fecha_hasta, tipo, horas_ajuste, paga,
                 motivo, aprobado_por, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (body.empleado_id, body.fecha_desde, body.fecha_hasta, body.tipo,
             body.horas_ajuste, 1 if body.paga else 0,
             body.motivo, user.get("username", "admin"), now),
        )
        conn.commit()
        xid = cur.lastrowid
    finally:
        conn.close()
    return {"id": xid}


@router.delete("/api/excepciones/{excep_id}")
async def api_excepcion_borrar(excep_id: int,
                                 session_token: str | None = Cookie(default=None)):
    _require_user(session_token)
    conn = get_conn()
    try:
        conn.execute("DELETE FROM excepciones_asistencia WHERE id = ?", (excep_id,))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@router.delete("/api/empleados/{emp_id}")
async def api_empleado_borrar(emp_id: int, session_token: str | None = Cookie(default=None)):
    """Soft delete: marca empleado como inactivo. Mantiene historial de marcajes."""
    _require_user(session_token)
    conn = get_conn()
    try:
        conn.execute("UPDATE empleados SET activo = 0, updated_at = ? WHERE id = ?",
                     (datetime.utcnow().isoformat(), emp_id))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@router.delete("/api/empleados/{emp_id}/permanente")
async def api_empleado_borrar_definitivo(emp_id: int, session_token: str | None = Cookie(default=None)):
    """Borrado DEFINITIVO: elimina el empleado de la DB + sus marcajes.

    NO borra del equipo Hikvision (eso requiere que el navegador del admin
    en LAN local del equipo haga la llamada ISAPI).

    Para borrado completo (panel + equipo), el frontend debe:
      1. Llamar a este endpoint para limpiar la DB
      2. Llamar al equipo via /ISAPI/AccessControl/UserInfo/Delete con
         el hik_employee_no
    """
    _require_user(session_token)
    conn = get_conn()
    try:
        # Verificar que existe
        row = conn.execute(
            "SELECT id, hik_employee_no, nombre FROM empleados WHERE id = ?",
            (emp_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Empleado no encontrado")

        nombre = row["nombre"]
        hik_no = row["hik_employee_no"]

        # Borrar marcajes asociados primero (por FK)
        conn.execute("DELETE FROM marcajes WHERE empleado_id = ?", (emp_id,))
        # Borrar turnos asociados
        conn.execute("DELETE FROM turnos WHERE empleado_id = ?", (emp_id,))
        # Borrar horas calculadas
        conn.execute("DELETE FROM horas_calculadas WHERE empleado_id = ?", (emp_id,))
        # Borrar el empleado
        conn.execute("DELETE FROM empleados WHERE id = ?", (emp_id,))
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "nombre_borrado": nombre,
        "hik_employee_no": hik_no,
        "nota": "Empleado eliminado del panel. Para eliminarlo tambien del equipo Hikvision, hacelo desde el menu del equipo o desde el navegador en la LAN local.",
    }


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
    """Estado del equipo. Tiene dos formas de saber que esta vivo:

      A) Si la VM puede llegar a la IP del equipo (LAN compartida) -> consulta directa
      B) Si NO puede (caso comun: equipo detras de NAT en la fabrica) -> mira si hay
         marcajes recientes en la DB. Si hubo eventos en los ultimos 5 min -> online.
    """
    _require_user(session_token)
    from datetime import datetime, timedelta

    # B) verificar marcajes recientes (sirve siempre, incluso sin conexion directa)
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT MAX(ts) AS ultimo, COUNT(*) AS total FROM marcajes WHERE ts >= datetime('now', '-1 day')"
        ).fetchone()
        ultimo_marcaje = row["ultimo"] if row else None
        total_dia = row["total"] if row else 0
    finally:
        conn.close()

    push_reciente = False
    if ultimo_marcaje:
        try:
            ts = datetime.fromisoformat(ultimo_marcaje)
            edad_min = (datetime.now(ts.tzinfo) - ts).total_seconds() / 60
            push_reciente = edad_min < 60  # ultimo evento en los ultimos 60 min
        except Exception:
            pass

    # A) intentar conexion directa (solo si HIK_HOST esta seteado)
    from skiimo.config import HIK_ENABLED
    if HIK_ENABLED:
        try:
            with HikClient() as hik:
                di = hik.device_info()
                t = hik.device_time()
                n = hik.count_persons()
            return {
                "online": True,
                "modo": "direct",
                "model": di.model,
                "serial": di.serial,
                "firmware": di.firmware,
                "mac": di.mac,
                "device_time": t.get("localTime"),
                "timezone": t.get("timeZone"),
                "personas_en_equipo": n,
                "ultimo_marcaje": ultimo_marcaje,
            }
        except Exception as e:
            # Cae al fallback push
            return {
                "online": push_reciente,
                "modo": "push" if push_reciente else "offline",
                "ultimo_marcaje": ultimo_marcaje,
                "marcajes_24h": total_dia,
                "error_direct": str(e)[:200],
            }

    # Sin HIK_HOST -> solo modo push
    return {
        "online": push_reciente,
        "modo": "push" if push_reciente else "esperando",
        "ultimo_marcaje": ultimo_marcaje,
        "marcajes_24h": total_dia,
        "nota": "Equipo detras de NAT. Recibo eventos via POST cuando alguien marca.",
    }


# =============================================================================
# Endpoint PUBLICO: receptor de eventos push del Hikvision (ISUP Listening / httpHosts)
# =============================================================================
#
# El equipo se configura para hacer POST a esta URL cada vez que pasa un evento.
# Es PUBLICO (sin auth de sesion) porque el equipo no maneja cookies.
# Si se requiere auth, se pasa via Basic/Digest configurado en el equipo, pero
# por simplicidad y porque la conexion sale del equipo en LAN -> internet con
# IP destino fija (la nuestra), confiamos en el origen.
#
# El equipo puede mandar JSON, XML, o multipart/form-data segun firmware.
# Manejamos los 3 casos.


# Memoria para debug: guardar el ultimo evento crudo recibido (solo desarrollo)
_LAST_HIK_PAYLOAD: dict = {"count": 0, "samples": []}


@router.get("/api/hik/event/last")
async def api_hik_event_last():
    """Devuelve el ultimo evento crudo recibido. DEBUG."""
    return _LAST_HIK_PAYLOAD


@router.post("/api/asistencia/limpiar-secundarios")
async def api_limpiar_secundarios(session_token: str | None = Cookie(default=None)):
    """Borra de la DB los marcajes 'desconocidos' que vienen del equipo
    como eventos secundarios (puerta abierta, etc) sin employeeNo.

    Solo borra los que tienen empleado_id IS NULL y tipo='desconocido'.
    Los marcajes con empleado real quedan intactos.
    """
    _require_user(session_token)
    conn = get_conn()
    try:
        cur = conn.execute(
            """DELETE FROM marcajes
               WHERE empleado_id IS NULL
                 AND (tipo = 'desconocido' OR metodo = 'invalid')"""
        )
        conn.commit()
        borrados = cur.rowcount
    finally:
        conn.close()
    return {"ok": True, "borrados": borrados}


@router.post("/api/hik/event")
async def api_hik_event_receiver(request: Request):
    """Recibe eventos push del Hikvision configurado como ISUP Listening.

    El equipo manda multipart/form-data con un campo JSON describiendo
    el evento + 1 o mas partes binarias con las fotos del momento.
    """
    import json
    import os
    import uuid
    from pathlib import Path
    from skiimo.hikvision import _event_from_raw
    from skiimo.config import DB_PATH

    raw_payload: dict | None = None
    foto_relativa: str | None = None  # path relativo desde /static
    content_type = (request.headers.get("content-type") or "").lower()
    _LAST_HIK_PAYLOAD["count"] += 1

    # Directorio donde guardamos fotos. /data/photos al lado de la DB.
    fotos_dir = Path(DB_PATH).parent / "photos"
    fotos_dir.mkdir(parents=True, exist_ok=True)

    try:
        if "json" in content_type:
            raw_payload = await request.json()
        elif "xml" in content_type:
            text = (await request.body()).decode("utf-8", errors="ignore")
            from xml.etree import ElementTree as ET
            try:
                from skiimo.hikvision import _xml_walk, _strip_ns
                root = ET.fromstring(text)
                walked = _xml_walk(root)
                raw_payload = walked if isinstance(walked, dict) else {"_text": str(walked)}
            except ET.ParseError as e:
                log.warning("XML parse error en hik event: %s", e)
                raw_payload = {"_raw_xml": text[:1000]}
        elif "multipart" in content_type:
            form = await request.form()
            # Buscar el campo JSON principal + fotos en el mismo form
            for k, v in form.items():
                # Si es archivo (foto), guardarla
                if hasattr(v, "read") and hasattr(v, "filename"):
                    try:
                        contenido = await v.read()
                        if contenido and len(contenido) > 500:  # ignorar thumbnails vacios
                            ext = ".jpg"
                            if v.filename and "." in v.filename:
                                ext = "." + v.filename.rsplit(".", 1)[-1].lower()
                            fname = f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}{ext}"
                            (fotos_dir / fname).write_bytes(contenido)
                            foto_relativa = f"/photos/{fname}"
                    except Exception:
                        log.exception("Error guardando foto del push")
                    continue
                s = str(v)
                if s.startswith("{"):
                    try:
                        raw_payload = json.loads(s)
                    except Exception:
                        pass
            if raw_payload is None:
                raw_payload = {k: str(v) for k, v in form.items() if not hasattr(v, "read")}
        else:
            body = (await request.body()).decode("utf-8", errors="ignore")
            try:
                raw_payload = json.loads(body)
            except Exception:
                raw_payload = {"_raw_body": body[:1000]}
    except Exception as e:
        log.exception("Error parseando push del Hikvision")
        return {"ok": False, "error": str(e)}

    if not raw_payload:
        return {"ok": False, "error": "Sin payload"}

    # DEBUG: guardar muestras (solo primeras 10)
    if len(_LAST_HIK_PAYLOAD["samples"]) < 10:
        _LAST_HIK_PAYLOAD["samples"].append({
            "content_type": content_type,
            "payload": raw_payload,
        })

    # DEBUG: log el primer evento de cada tipo para entender el formato
    et = raw_payload.get("eventType") or raw_payload.get("EventNotificationAlert", {}).get("eventType") if isinstance(raw_payload.get("EventNotificationAlert"), dict) else None
    log.debug("Hik push eventType=%s keys=%s", et, list(raw_payload.keys())[:10])

    # Estructura esperada del evento de Hikvision (eventos AccessControl):
    # {
    #   "ipAddress": "...", "portNo": ..., "macAddress": "...",
    #   "channelID": 1, "dateTime": "2026-05-26T14:30:00-05:00",
    #   "activePostCount": 1, "eventType": "AccessControllerEvent",
    #   "AccessControllerEvent": { "majorEventType": 5, "subEventType": 75,
    #     "employeeNoString": "1", "name": "...", "currentVerifyMode": "...",
    #     "attendanceStatus": "...", "pictureURL": "...", ... }
    # }

    # Extraer el bloque relevante
    acs = raw_payload.get("AccessControllerEvent") or raw_payload
    if not isinstance(acs, dict):
        log.warning("AccessControllerEvent no es dict: %s", type(acs))
        return {"ok": True, "ignored": "estructura desconocida"}

    # Filtrar eventos secundarios (puerta abierta, alarmas, etc).
    # Solo guardamos eventos de autenticacion real de personas:
    #   minor 75 = face authentication
    #   minor 76 = fingerprint
    #   minor 38 = card
    #   minor 77 = pin
    # El equipo emite ademas eventos minor=21/22 (door open/close) o
    # similares por cada marcaje, sin employeeNo, que ensucian la tabla.
    minor = int(acs.get("subEventType") or acs.get("minor") or 0)
    SUB_EVENTS_AUTENTICACION = {75, 76, 77, 38, 1, 5, 7, 15, 16, 19, 20}
    if minor not in SUB_EVENTS_AUTENTICACION:
        return {"ok": True, "ignored": f"evento secundario minor={minor}"}

    # Adaptar nombres de campos al formato esperado por _event_from_raw
    ts = raw_payload.get("dateTime") or acs.get("dateTime") or acs.get("time")
    # Preferir foto del multipart (la que guardamos localmente). Si no, la URL del equipo.
    pic = foto_relativa or acs.get("pictureURL")
    adapted = {
        "time": ts,
        "employeeNoString": acs.get("employeeNoString") or acs.get("employeeNo"),
        "employeeNo": acs.get("employeeNo"),
        "name": acs.get("name"),
        "cardNo": acs.get("cardNo"),
        "major": acs.get("majorEventType") or acs.get("major") or 5,
        "minor": acs.get("subEventType") or acs.get("minor") or 0,
        "currentVerifyMode": acs.get("currentVerifyMode"),
        "attendanceStatus": acs.get("attendanceStatus"),
        "pictureURL": pic,
        "serialNo": acs.get("serialNo") or raw_payload.get("macAddress"),
    }

    ev = _event_from_raw(adapted)

    # Insertar via la misma logica del sync (dedup, resolver empleado_id, etc.)
    from skiimo.asistencia.sync import _insert_marcaje
    try:
        inserted = _insert_marcaje(ev)
    except Exception as e:
        log.exception("Error insertando evento push")
        return {"ok": False, "error": str(e)}

    log.info(
        "Push Hik recibido: emp=%s name=%s mayor.minor=%d.%d insert=%s",
        ev.employee_no, ev.name, ev.major, ev.minor, inserted,
    )
    return {"ok": True, "inserted": inserted, "event_id": ev.event_id}


@router.get("/api/hik/event/test")
async def api_hik_event_test():
    """Smoke endpoint publico para que el equipo verifique conectividad."""
    return {"ok": True, "service": "skiimo-asistencia", "ready": True}
