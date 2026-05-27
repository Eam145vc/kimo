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

    @app.get("/jornada", response_class=HTMLResponse)
    async def page_jornada(request: Request, session_token: str | None = Cookie(default=None)):
        user = validar_sesion(session_token)
        if not user:
            return RedirectResponse(url="/login", status_code=303)
        return templates.TemplateResponse(
            request=request, name="jornada.html",
            context={"user": user["username"], "page": "jornada"},
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

    # Traemos timestamps + tipo + datos salariales del empleado
    sql = f"""
        SELECT
            m.empleado_id,
            e.nombre AS empleado_nombre,
            e.valor_hora_ord,
            e.salario_mensual,
            m.fecha,
            m.ts,
            m.tipo
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
                "valor_hora_ord": r["valor_hora_ord"],
                "salario_mensual": r["salario_mensual"],
                "fecha": r["fecha"],
                "marcajes": [],
            }
        grupos[key]["marcajes"].append({"ts": r["ts"], "tipo": r["tipo"]})

    def _fmt(ts_iso: str) -> str | None:
        try:
            return datetime.fromisoformat(ts_iso).strftime("%H:%M:%S")
        except Exception:
            return None

    items = []
    for (eid, fecha), g in grupos.items():
        marcajes = g["marcajes"]
        n = len(marcajes)
        ts_list = [m["ts"] for m in marcajes]

        # ¿Cuantos marcajes tienen tipo clasificado por el equipo?
        # (no 'desconocido' y no NULL)
        tipos_claros = [
            m for m in marcajes
            if m["tipo"] in ("entrada", "salida", "almuerzo_out", "almuerzo_in",
                              "extra_in", "extra_out")
        ]
        usar_tipos = len(tipos_claros) > 0

        # Tomamos el primero/ultimo de cada tipo REAL (no inferimos).
        # Si no hay un marcaje tipo X, la celda queda vacia.
        entradas_ts = [m["ts"] for m in marcajes if m["tipo"] == "entrada"]
        salidas_ts = [m["ts"] for m in marcajes if m["tipo"] == "salida"]
        outs_ts = [m["ts"] for m in marcajes if m["tipo"] == "almuerzo_out"]
        ins_ts = [m["ts"] for m in marcajes if m["tipo"] == "almuerzo_in"]
        extra_in_ts = [m["ts"] for m in marcajes if m["tipo"] == "extra_in"]
        extra_out_ts = [m["ts"] for m in marcajes if m["tipo"] == "extra_out"]

        primera_entrada_ts = min(entradas_ts) if entradas_ts else None
        ultima_salida_ts = max(salidas_ts) if salidas_ts else None
        almuerzo_out_ts = min(outs_ts) if outs_ts else None
        almuerzo_in_ts = max(ins_ts) if ins_ts else None
        primer_extra_in_ts = min(extra_in_ts) if extra_in_ts else None
        ultimo_extra_out_ts = max(extra_out_ts) if extra_out_ts else None

        items.append({
            "empleado_id": g["empleado_id"],
            "empleado_nombre": g["empleado_nombre"],
            "valor_hora_ord": g.get("valor_hora_ord"),
            "salario_mensual": g.get("salario_mensual"),
            "fecha": fecha,
            "primera_entrada": _fmt(primera_entrada_ts),
            "primera_entrada_ts": primera_entrada_ts,
            "almuerzo_out": _fmt(almuerzo_out_ts),
            "almuerzo_out_ts": almuerzo_out_ts,
            "almuerzo_in": _fmt(almuerzo_in_ts),
            "almuerzo_in_ts": almuerzo_in_ts,
            "ultima_salida": _fmt(ultima_salida_ts),
            "ultima_salida_ts": ultima_salida_ts,
            "extra_in": _fmt(primer_extra_in_ts),
            "extra_in_ts": primer_extra_in_ts,
            "extra_out": _fmt(ultimo_extra_out_ts),
            "extra_out_ts": ultimo_extra_out_ts,
            "cantidad_marcajes": n,
        })

    # ===========================================================================
    # Calcular horas trabajadas + status de cada marcaje con tolerancia.
    # ===========================================================================
    from skiimo.asistencia.config import get_conf

    def _parse_hhmm(s: str, fallback_h: int, fallback_m: int = 0) -> tuple[int, int]:
        try:
            h, m = s.split(":")
            return int(h), int(m)
        except Exception:
            return fallback_h, fallback_m

    entrada_h, entrada_m = _parse_hhmm(get_conf("jornada_entrada_hora") or "07:00", 7)
    alm_ini_h, alm_ini_m = _parse_hhmm(get_conf("jornada_almuerzo_inicio") or "12:00", 12)
    alm_fin_h, alm_fin_m = _parse_hhmm(get_conf("jornada_almuerzo_fin") or "13:00", 13)
    salida_h, salida_m = _parse_hhmm(get_conf("jornada_salida_hora") or "17:00", 17)

    try:
        TOLERANCIA_MIN = int(get_conf("tolerancia_min") or 5)
    except Exception:
        TOLERANCIA_MIN = 5
    try:
        margen_extras_min = int(get_conf("margen_gracia_extras_min") or 30)
    except Exception:
        margen_extras_min = 30
    try:
        valor_hora_extra = float(get_conf("valor_hora_extra") or 14000)
    except Exception:
        valor_hora_extra = 14000.0

    def _evaluar_marcaje(ts_iso: str | None, hora_oficial_h: int, hora_oficial_m: int) -> str:
        """Devuelve 'ok' si esta dentro de +/- 5 min del hito, 'temprano' si antes,
        'tarde' si despues, 'falta' si no hay marcaje."""
        if not ts_iso:
            return "falta"
        try:
            ts = datetime.fromisoformat(ts_iso)
        except Exception:
            return "falta"
        oficial = ts.replace(hour=hora_oficial_h, minute=hora_oficial_m,
                              second=0, microsecond=0)
        delta_min = (ts - oficial).total_seconds() / 60.0
        if abs(delta_min) <= TOLERANCIA_MIN:
            return "ok"
        return "temprano" if delta_min < 0 else "tarde"

    def _ajustar_a_oficial(ts_iso: str | None, hora_oficial_h: int, hora_oficial_m: int,
                            tipo: str) -> datetime | None:
        """Aplica reglas de tolerancia para calculo de horas trabajadas.

        Trunca los segundos al minuto inferior (13:06:54 -> 13:06:00) para
        que el calculo de minutos perdidos sea exacto.

        - Entrada: si marca antes de la oficial -> oficial. Si dentro de tolerancia -> oficial.
                   Si despues de tolerancia -> hora real - tolerancia (los 5 min se respetan).
        - Salida: si marca dentro o despues de tolerancia -> oficial.
                  Antes de tolerancia -> hora real + tolerancia.
        - Almuerzo_out: igual que salida (al reves para el calculo).
        - Almuerzo_in: igual que entrada.
        """
        if not ts_iso:
            return None
        try:
            ts = datetime.fromisoformat(ts_iso)
        except Exception:
            return None
        # Truncar segundos para redondear hacia abajo
        ts = ts.replace(second=0, microsecond=0)
        oficial = ts.replace(hour=hora_oficial_h, minute=hora_oficial_m,
                              second=0, microsecond=0)
        delta_min = (ts - oficial).total_seconds() / 60.0

        # REGLA UNIVERSAL: el empleado NUNCA gana tiempo, solo puede perder.
        # La tolerancia (+/-5 min) solo evita "perder" por pequeñas variaciones.
        # Las horas extras se manejan APARTE con marcajes explicitos extra_in/extra_out.
        #
        # Para cada hito devolvemos la hora a usar en el calculo:

        if tipo == "entrada":
            # Antes de oficial: oficial (no gana)
            # Hasta +tol: oficial (tolerancia)
            # Despues: hora real (pierde)
            if delta_min <= TOLERANCIA_MIN:
                return oficial
            return ts

        if tipo == "almuerzo_out":
            # Antes de oficial-tol: hora real (pierde, salio antes a almorzar)
            # Entre -tol y +infinito: oficial (no gana por salir tarde a almorzar)
            if delta_min < -TOLERANCIA_MIN:
                return ts
            return oficial

        if tipo == "almuerzo_in":
            # Hasta +tol: oficial (no gana por volver antes)
            # Despues: hora real (pierde, llego tarde de almorzar)
            if delta_min <= TOLERANCIA_MIN:
                return oficial
            return ts

        if tipo == "salida":
            # Antes de oficial-tol: hora real (pierde, se va antes)
            # Entre -tol y +infinito: oficial (no gana por quedarse mas tiempo)
            if delta_min < -TOLERANCIA_MIN:
                return ts
            return oficial

        return ts

    # Hora actual (Bogota) para calcular horas "en curso" cuando no hay salida.
    ahora_bogota = datetime.now(TZ_BOGOTA)
    hoy_str = ahora_bogota.date().isoformat()

    for item in items:
        # Evaluar status de cada hito
        item["status_entrada"] = _evaluar_marcaje(item["primera_entrada_ts"], entrada_h, entrada_m)
        item["status_almuerzo_out"] = _evaluar_marcaje(item["almuerzo_out_ts"], alm_ini_h, alm_ini_m)
        item["status_almuerzo_in"] = _evaluar_marcaje(item["almuerzo_in_ts"], alm_fin_h, alm_fin_m)
        item["status_salida"] = _evaluar_marcaje(item["ultima_salida_ts"], salida_h, salida_m)

        # Calcular horas trabajadas: ajustar marcajes a hora oficial con tolerancia
        t_in_ajust = _ajustar_a_oficial(item["primera_entrada_ts"], entrada_h, entrada_m, "entrada")
        t_out_ajust = _ajustar_a_oficial(item["ultima_salida_ts"], salida_h, salida_m, "salida")

        # En curso: si el dia es HOY y NO marco salida pero tiene entrada
        # -> usamos la hora actual como salida estimada
        en_curso = False
        if (
            t_in_ajust is not None
            and t_out_ajust is None
            and item["fecha"] == hoy_str
        ):
            # Solo si tiene entrada (sino no esta trabajando)
            t_out_ajust = ahora_bogota
            en_curso = True

        item["en_curso"] = en_curso

        horas = None
        horas_extras = 0.0
        if t_in_ajust and t_out_ajust and t_out_ajust > t_in_ajust:
            # Total bruto trabajado (en horas)
            bruto = (t_out_ajust - t_in_ajust).total_seconds() / 3600.0

            # ----- Descuento de almuerzo -----
            # Lógica:
            #  - Si NO marcaron almuerzo: descontar 1h oficial (12-13).
            #  - Si SI marcaron (tienen almuerzo_out + almuerzo_in):
            #      Tiempo de almuerzo = (regreso_real - salida_real)
            #      Si ambos están dentro de tolerancia -> descontar 1h oficial
            #      Si uno o ambos están fuera -> descontar el tiempo real
            alm_inicio_oficial = t_in_ajust.replace(hour=alm_ini_h, minute=alm_ini_m, second=0, microsecond=0)
            alm_fin_oficial = t_in_ajust.replace(hour=alm_fin_h, minute=alm_fin_m, second=0, microsecond=0)
            pausa_oficial_h = (alm_fin_oficial - alm_inicio_oficial).total_seconds() / 3600.0

            t_alm_out_real = None
            t_alm_in_real = None
            if item["almuerzo_out_ts"]:
                try:
                    t_alm_out_real = datetime.fromisoformat(item["almuerzo_out_ts"])
                except Exception:
                    pass
            if item["almuerzo_in_ts"]:
                try:
                    t_alm_in_real = datetime.fromisoformat(item["almuerzo_in_ts"])
                except Exception:
                    pass

            # Calculo de pausa de almuerzo:
            #  - Si tiene salida_out y regreso_in: usar ambos ajustados con tolerancia
            #  - Si solo tiene UNO de los dos: asumir oficial para el faltante,
            #    aplicar tolerancia al que SI tiene
            #  - Si no tiene ninguno pero el rango cruza 12-13: descontar 1h oficial
            descuento_almuerzo = 0.0

            t_out_ajust_alm = _ajustar_a_oficial(
                item["almuerzo_out_ts"], alm_ini_h, alm_ini_m, "almuerzo_out"
            )
            t_in_ajust_alm = _ajustar_a_oficial(
                item["almuerzo_in_ts"], alm_fin_h, alm_fin_m, "almuerzo_in"
            )

            tiene_alguno = t_out_ajust_alm or t_in_ajust_alm
            cruza_almuerzo = t_in_ajust < alm_fin_oficial and t_out_ajust > alm_inicio_oficial

            if tiene_alguno:
                # Si falta uno de los dos, asumimos oficial
                if t_out_ajust_alm is None:
                    t_out_ajust_alm = alm_inicio_oficial
                if t_in_ajust_alm is None:
                    t_in_ajust_alm = alm_fin_oficial
                if t_in_ajust_alm > t_out_ajust_alm:
                    descuento_almuerzo = (t_in_ajust_alm - t_out_ajust_alm).total_seconds() / 3600.0
            elif cruza_almuerzo:
                # No marcaron nada pero trabajan cruzando almuerzo: descontar oficial
                inicio = max(t_in_ajust, alm_inicio_oficial)
                fin = min(t_out_ajust, alm_fin_oficial)
                if fin > inicio:
                    descuento_almuerzo = (fin - inicio).total_seconds() / 3600.0

            bruto -= descuento_almuerzo
            horas = round(bruto, 2)
            item["pausa_almuerzo_h"] = round(descuento_almuerzo, 2)

        # Horas extras: SOLO cuentan si el empleado marco explicitamente
        # extra_in y extra_out (botones del equipo o agregado manual).
        # Sin marca = no hay extras, aunque se quede hasta tarde.
        try:
            if not en_curso and item["extra_in_ts"] and item["extra_out_ts"]:
                t_ex_in = datetime.fromisoformat(item["extra_in_ts"])
                t_ex_out = datetime.fromisoformat(item["extra_out_ts"])
                if t_ex_out > t_ex_in:
                    horas_extras = round(
                        (t_ex_out - t_ex_in).total_seconds() / 3600.0, 2
                    )
        except Exception:
            pass

        item["horas"] = horas
        item["horas_extras"] = horas_extras

        # Calculo de pago del dia
        # - horas_pago_ord = horas * valor_hora_ord
        # - horas_pago_extras = horas_extras * valor_hora_extra (configurable, global)
        valor_h = item.get("valor_hora_ord") or 0
        pago_ord = (horas or 0) * valor_h
        pago_extras = (horas_extras or 0) * valor_hora_extra
        item["pago_ord"] = round(pago_ord)
        item["pago_extras"] = round(pago_extras)
        item["pago_total"] = round(pago_ord + pago_extras)
        item["valor_hora_extra"] = valor_hora_extra

        # Status de extras
        item["status_extra_in"] = "ok" if item["extra_in_ts"] else ("falta" if horas_extras > 0 else "no_aplica")
        item["status_extra_out"] = "ok" if item["extra_out_ts"] else ("falta" if horas_extras > 0 else "no_aplica")

        # No exponer los ts crudos al frontend
        for k in ("primera_entrada_ts", "almuerzo_out_ts", "almuerzo_in_ts",
                  "ultima_salida_ts", "extra_in_ts", "extra_out_ts"):
            item.pop(k, None)

    # Orden: fecha desc, nombre asc
    items.sort(key=lambda x: (x["fecha"], x["empleado_nombre"]), reverse=False)
    items.sort(key=lambda x: x["fecha"], reverse=True)

    # ===========================================================================
    # KPIs del rango filtrado (sirven para mostrar arriba en la tabla)
    # ===========================================================================
    # Sumario solo del DIA DE HOY si esta en el rango (mas accionable).
    items_hoy = [i for i in items if i["fecha"] == hoy_str]
    tarde_entrada_hoy = sum(1 for i in items_hoy if i["status_entrada"] == "tarde")
    tarde_almuerzo_hoy = sum(1 for i in items_hoy if i["status_almuerzo_in"] == "tarde")

    # Tarde extras: marco extra_in DESPUES de la hora oficial en que empiezan
    # las extras (configurable, default = salida + margen_extras_min). Por ej:
    # salida 17:00 + 30 = extras empiezan 17:30. Si marca extra_in 17:36 -> tarde.
    limite_extras_tarde = datetime.combine(
        ahora_bogota.date(),
        datetime.min.replace(hour=salida_h, minute=salida_m).time(),
    ).replace(tzinfo=TZ_BOGOTA) + timedelta(minutes=margen_extras_min + TOLERANCIA_MIN)

    def _es_tarde_extra(i: dict) -> bool:
        if not i.get("extra_in"):
            return False
        try:
            ts = datetime.fromisoformat(f"{i['fecha']}T{i['extra_in']}-05:00")
            return ts > limite_extras_tarde
        except Exception:
            return False

    tarde_extras_hoy = sum(1 for i in items_hoy if _es_tarde_extra(i))

    sin_salida_hoy = sum(
        1 for i in items_hoy
        if i["status_entrada"] in ("ok", "tarde", "temprano") and i["status_salida"] == "falta"
    )
    en_curso_hoy = sum(1 for i in items_hoy if i.get("en_curso"))

    # Empleados activos que no marcaron HOY (ausentes)
    conn2 = get_conn()
    try:
        emp_activos = conn2.execute(
            "SELECT id, nombre FROM empleados WHERE activo = 1"
        ).fetchall()
    finally:
        conn2.close()
    ids_que_marcaron_hoy = {i["empleado_id"] for i in items_hoy}
    ausentes_hoy = [
        {"id": e["id"], "nombre": e["nombre"]}
        for e in emp_activos
        if e["id"] not in ids_que_marcaron_hoy
    ]

    return {
        "items": items,
        "jornada": {
            "entrada": f"{entrada_h:02d}:{entrada_m:02d}",
            "almuerzo_inicio": f"{alm_ini_h:02d}:{alm_ini_m:02d}",
            "almuerzo_fin": f"{alm_fin_h:02d}:{alm_fin_m:02d}",
            "salida": f"{salida_h:02d}:{salida_m:02d}",
            "tolerancia_min": TOLERANCIA_MIN,
            "margen_extras_min": margen_extras_min,
        },
        "kpis": {
            "fecha_hoy": hoy_str,
            "empleados_activos": len(emp_activos),
            "marcaron_hoy": len(items_hoy),
            "en_curso_hoy": en_curso_hoy,
            "tarde_entrada_hoy": tarde_entrada_hoy,
            "tarde_almuerzo_hoy": tarde_almuerzo_hoy,
            "tarde_extras_hoy": tarde_extras_hoy,
            "sin_salida_hoy": sin_salida_hoy,
            "ausentes_hoy": ausentes_hoy,
        },
    }


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
    sal = body.salario_mensual
    valor_hora = round(sal / DEFAULTS["horas_legales_mes"]) if sal else None
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


# =============================================================================
# Correccion de marcajes
# =============================================================================


@router.post("/api/marcajes")
async def api_marcaje_crear_manual(body: MarcajeIn, session_token: str | None = Cookie(default=None)):
    """Crea un marcaje manual (cuando el equipo no lo registro o el admin
    necesita agregar uno historico). origen='manual', metodo='manual'.

    Si `tipo` es null, se autoclasifica segun la hora del dia.
    """
    user = _require_user(session_token)
    now = datetime.utcnow().isoformat()
    try:
        ts = datetime.fromisoformat(body.ts)
    except ValueError:
        raise HTTPException(400, "ts invalido. Use ISO-8601, ej: 2026-05-26T07:00:00-05:00")
    fecha = ts.date().isoformat()

    # Si no vino tipo, clasificar por hora
    tipo = body.tipo
    if not tipo:
        from skiimo.asistencia.sync import _clasificar_por_hora
        tipo = _clasificar_por_hora(ts)

    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO marcajes (empleado_id, ts, fecha, tipo, metodo, origen,
                                      editado, editado_por, editado_at, nota_admin,
                                      ignorar_nomina, created_at)
               VALUES (?, ?, ?, ?, 'manual', 'manual', 1, ?, ?, ?, ?, ?)""",
            (body.empleado_id, body.ts, fecha, tipo,
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

    # Si no vino tipo, autoclasificar por hora
    tipo = body.tipo
    if not tipo:
        from skiimo.asistencia.sync import _clasificar_por_hora
        tipo = _clasificar_por_hora(ts)

    conn = get_conn()
    try:
        # Verificar que existe
        row = conn.execute("SELECT origen FROM marcajes WHERE id = ?", (marcaje_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Marcaje no encontrado")
        nuevo_origen = "manual" if row["origen"] == "manual" else "corregido"

        conn.execute(
            """UPDATE marcajes SET
                  ts=?, fecha=?, tipo=?,
                  origen=?, editado=1, editado_por=?, editado_at=?,
                  nota_admin=COALESCE(?, nota_admin),
                  ignorar_nomina=?
               WHERE id = ?""",
            (body.ts, fecha, tipo, nuevo_origen,
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


class SalarioMasivoIn(BaseModel):
    """Body para POST /api/empleados/salario-masivo."""
    salario_mensual: float
    cargo: str | None = None  # si viene, tambien actualiza el cargo
    excluir_ids: list[int] = []
    excluir_nombres: list[str] = []  # nombres a excluir (parciales)
    solo_sin_salario: bool = False  # si True, solo a los que no tienen salario


@router.post("/api/empleados/salario-masivo")
async def api_empleados_salario_masivo(
    body: SalarioMasivoIn,
    session_token: str | None = Cookie(default=None),
):
    """Aplica salario (y opcionalmente cargo) a todos los empleados activos.
    Calcula valor_hora_ord automaticamente.
    """
    _require_user(session_token)
    now = datetime.utcnow().isoformat()
    horas_mes = DEFAULTS.get("horas_legales_mes", 230)
    valor_hora = round(body.salario_mensual / horas_mes)

    where = ["activo = 1"]
    params: list[Any] = []
    if body.excluir_ids:
        placeholders = ",".join("?" * len(body.excluir_ids))
        where.append(f"id NOT IN ({placeholders})")
        params.extend(body.excluir_ids)
    if body.excluir_nombres:
        for nombre in body.excluir_nombres:
            where.append("LOWER(nombre) NOT LIKE LOWER(?)")
            params.append(f"%{nombre}%")
    if body.solo_sin_salario:
        where.append("(salario_mensual IS NULL OR salario_mensual = 0)")

    # Construir SET dinamico (cargo opcional)
    sets = ["salario_mensual = ?", "valor_hora_ord = ?", "updated_at = ?"]
    set_params = [body.salario_mensual, valor_hora, now]
    if body.cargo and body.cargo.strip():
        sets.insert(2, "cargo = ?")
        set_params.insert(2, body.cargo.strip())

    sql = f"UPDATE empleados SET {', '.join(sets)} WHERE {' AND '.join(where)}"
    conn = get_conn()
    try:
        cur = conn.execute(sql, (*set_params, *params))
        conn.commit()
        actualizados = cur.rowcount
    finally:
        conn.close()
    return {
        "ok": True,
        "actualizados": actualizados,
        "salario": body.salario_mensual,
        "valor_hora_ord": valor_hora,
        "cargo": body.cargo,
    }


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


@router.post("/api/asistencia/reclasificar")
async def api_reclasificar_marcajes(session_token: str | None = Cookie(default=None)):
    """Re-clasifica todos los marcajes existentes usando la logica de hora del dia.

    Util cuando se cambia la jornada o se agregan empleados nuevos.
    Sobreescribe el campo `tipo` de marcajes con tipo='desconocido' o
    cuando el tipo no coincide con la franja horaria.
    No toca marcajes editados manualmente (editado=1).
    """
    _require_user(session_token)
    from skiimo.asistencia.sync import _clasificar_por_hora

    conn = get_conn()
    actualizados = 0
    try:
        rows = conn.execute(
            """SELECT id, empleado_id, fecha, ts, tipo
               FROM marcajes
               WHERE empleado_id IS NOT NULL
                 AND COALESCE(editado, 0) = 0
               ORDER BY empleado_id, fecha, ts"""
        ).fetchall()

        # Track tipos ya asignados por (empleado, fecha) para detectar duplicados
        usados: dict[tuple[int, str], set] = {}

        for r in rows:
            try:
                ts = datetime.fromisoformat(r["ts"])
            except Exception:
                continue
            tipo_correcto = _clasificar_por_hora(ts)
            if tipo_correcto == "desconocido":
                continue
            key = (r["empleado_id"], r["fecha"])
            ya_usados = usados.setdefault(key, set())
            if tipo_correcto in ya_usados:
                tipo_correcto = "extra"
            else:
                ya_usados.add(tipo_correcto)

            if r["tipo"] != tipo_correcto:
                conn.execute(
                    "UPDATE marcajes SET tipo = ? WHERE id = ?",
                    (tipo_correcto, r["id"]),
                )
                actualizados += 1
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "actualizados": actualizados}


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
