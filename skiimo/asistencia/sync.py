"""Sync de marcajes desde el Hikvision al SQLite local.

Estrategia:
  - Tabla asistencia_sync guarda el ts del ultimo evento procesado.
  - En cada run jalamos desde (last_event_ts - 5 min) hasta now() para tolerar
    relojes desfasados y eventos que llegaron tarde.
  - Deduplicamos por hik_event_id (timestamp+serial+major+minor+emp).
  - Si el evento trae employeeNo, intentamos resolverlo a empleado_id local.

Tipos de marcaje (heuristica simple, mejorable con `attendanceStatus` del equipo):
  - Si es el 1er marcaje del dia para ese empleado -> "entrada"
  - Si es el ultimo del dia (cierre 22h) -> "salida"
  - Marcajes intermedios -> "almuerzo_out" o "almuerzo_in" segun orden

Se ejecuta como cron via `python -m skiimo.asistencia.sync` o programaticamente.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from skiimo.asistencia.config import get_conf
from skiimo.db.schema import get_conn
from skiimo.hikvision import TZ_BOGOTA, HikAcsEvent, HikClient


def _now() -> datetime:
    return datetime.now(TZ_BOGOTA)


def _get_state() -> dict:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM asistencia_sync WHERE id = 1").fetchone()
        if row:
            return dict(row)
    finally:
        conn.close()
    return {"id": 1, "last_event_ts": None, "eventos_procesados": 0}


def _save_state(*, last_event_ts: datetime | None, status: str, error: str | None, count: int) -> None:
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO asistencia_sync (id, last_event_ts, last_sync_at, last_sync_status, last_sync_error, eventos_procesados)
               VALUES (1, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   last_event_ts = COALESCE(excluded.last_event_ts, asistencia_sync.last_event_ts),
                   last_sync_at = excluded.last_sync_at,
                   last_sync_status = excluded.last_sync_status,
                   last_sync_error = excluded.last_sync_error,
                   eventos_procesados = asistencia_sync.eventos_procesados + excluded.eventos_procesados""",
            (
                last_event_ts.isoformat() if last_event_ts else None,
                _now().isoformat(),
                status,
                error,
                count,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _resolve_empleado_id(hik_employee_no: str | None) -> int | None:
    if not hik_employee_no:
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM empleados WHERE hik_employee_no = ?", (hik_employee_no,)
        ).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def _auto_create_empleado(hik_employee_no: str, nombre_hik: str | None) -> int | None:
    """Crea un empleado automaticamente cuando llega un marcaje de un ID nuevo.

    Usa el nombre que vino del equipo (puede estar en minusculas). El usuario
    DEBE editarlo despues desde /empleados para asignar salario y plantilla
    de horario - no usamos defaults para evitar pagos erroneos.
    """
    if not hik_employee_no:
        return None

    nombre = (nombre_hik or f"Empleado #{hik_employee_no}").strip()
    if nombre.islower():
        nombre = " ".join(w.capitalize() for w in nombre.split())

    now = datetime.now(TZ_BOGOTA).isoformat()
    conn = get_conn()
    try:
        try:
            # salario_mensual NULL: obligar a admin a configurarlo manualmente
            cur = conn.execute(
                """INSERT INTO empleados (hik_employee_no, nombre, cargo,
                                           activo, observaciones,
                                           created_at, updated_at)
                   VALUES (?, ?, ?, 1, ?, ?, ?)""",
                (
                    hik_employee_no,
                    nombre,
                    "Pendiente configuracion",
                    "Auto-creado desde push del equipo. Configurar salario y plantilla.",
                    now,
                    now,
                ),
            )
            conn.commit()
            return cur.lastrowid
        except Exception as e:
            if "UNIQUE" in str(e):
                row = conn.execute(
                    "SELECT id FROM empleados WHERE hik_employee_no = ?",
                    (hik_employee_no,),
                ).fetchone()
                return row["id"] if row else None
            raise
    finally:
        conn.close()


_ATTENDANCE_STATUS_MAP = {
    # Valores que manda el Hikvision cuando esta en modo "Manual + Auto":
    "checkIn": "entrada",
    "checkOut": "salida",
    "breakOut": "almuerzo_out",
    "breakIn": "almuerzo_in",
    "overTimeIn": "extra_in",
    "overTimeOut": "extra_out",
    "checkin": "entrada",
    "checkout": "salida",
    "breakout": "almuerzo_out",
    "breakin": "almuerzo_in",
}


# Jornada estandar de Esskimo (configurable a futuro via plantillas):
#   07:00 entrada / 12:00 salida almuerzo / 13:00 regreso / 17:00 salida
# Ventanas horarias para clasificar marcajes automaticamente. Generosas
# para tolerar llegadas tarde / temprano sin confundirse.
_VENTANAS_JORNADA = [
    # (hora_inicio_min, hora_fin_min, tipo)
    # Cada tupla: rango de minutos desde 00:00.
    (5 * 60 + 30, 10 * 60 + 30, "entrada"),         # 05:30 - 10:30
    (10 * 60 + 30, 12 * 60 + 45, "almuerzo_out"),   # 10:30 - 12:45
    (12 * 60 + 45, 14 * 60 + 30, "almuerzo_in"),    # 12:45 - 14:30
    (14 * 60 + 30, 23 * 60 + 30, "salida"),         # 14:30 - 23:30
]


def _clasificar_por_hora(ts: datetime) -> str:
    """Devuelve el tipo segun la franja horaria del marcaje.

    Usa la jornada estandar de Esskimo: 7-12 / 13-17. Cada marcaje cae
    en una de 4 ventanas: entrada / almuerzo_out / almuerzo_in / salida.
    """
    mins = ts.hour * 60 + ts.minute
    for ini, fin, tipo in _VENTANAS_JORNADA:
        if ini <= mins < fin:
            return tipo
    return "desconocido"


def _infer_tipo(
    empleado_id: int | None,
    fecha: str,
    ts: datetime,
    attendance_status: str | None = None,
) -> str:
    """Clasifica el marcaje. Prioridad:
      1. attendanceStatus del equipo si lo manda (modo Manual)
      2. Franja horaria (jornada estandar 7-12 / 13-17)
      3. Si dos marcajes caen en la misma franja, el segundo es 'extra'
    """
    if empleado_id is None:
        return "desconocido"

    # Caso 1: el equipo lo clasifico explicitamente
    if attendance_status:
        mapped = _ATTENDANCE_STATUS_MAP.get(attendance_status)
        if mapped:
            return mapped

    # Caso 2: clasificar por hora del dia
    tipo_por_hora = _clasificar_por_hora(ts)
    if tipo_por_hora == "desconocido":
        return tipo_por_hora

    # Verificar si ya existe otro marcaje del mismo tipo el mismo dia.
    # Si es asi, este es un duplicado/extra.
    conn = get_conn()
    try:
        existe = conn.execute(
            "SELECT id FROM marcajes WHERE empleado_id = ? AND fecha = ? AND tipo = ? LIMIT 1",
            (empleado_id, fecha, tipo_por_hora),
        ).fetchone()
    finally:
        conn.close()

    if existe:
        # Ya hay un marcaje del mismo tipo ese dia -> este es extra
        return "extra"
    return tipo_por_hora


def _legacy_infer_por_posicion(empleado_id: int, fecha: str) -> str:
    """Fallback antiguo (mantenido por compatibilidad)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT ts FROM marcajes WHERE empleado_id = ? AND fecha = ? ORDER BY ts",
            (empleado_id, fecha),
        ).fetchall()
    finally:
        conn.close()
    n = len(rows)
    secuencia = ["entrada", "almuerzo_out", "almuerzo_in", "salida"]
    if n < len(secuencia):
        return secuencia[n]
    return "extra"


def _insert_marcaje(ev: HikAcsEvent) -> bool:
    """Devuelve True si se inserto, False si era duplicado."""
    empleado_id = _resolve_empleado_id(ev.employee_no)
    # Auto-crear empleado si vino employeeNo pero no estaba en la DB todavia.
    # Solo crear si vino con nombre (para no crear empleados fantasma por eventos
    # secundarios como 'puerta abierta' que llegan con employeeNo en blanco).
    if empleado_id is None and ev.employee_no and ev.name:
        empleado_id = _auto_create_empleado(ev.employee_no, ev.name)
    fecha = ev.timestamp.date().isoformat()
    tipo = _infer_tipo(empleado_id, fecha, ev.timestamp, ev.attendance_status)

    conn = get_conn()
    try:
        try:
            conn.execute(
                """INSERT INTO marcajes (hik_event_id, empleado_id, hik_employee_no, ts, fecha,
                                         tipo, metodo, major, minor, nombre_hik, foto_url, raw_event, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ev.event_id,
                    empleado_id,
                    ev.employee_no,
                    ev.timestamp.isoformat(),
                    fecha,
                    tipo,
                    ev.verify_mode,
                    ev.major,
                    ev.minor,
                    ev.name,
                    ev.picture_url,
                    json.dumps(ev.raw, default=str),
                    _now().isoformat(),
                ),
            )
            conn.commit()
            return True
        except Exception as e:
            # UNIQUE constraint -> duplicado, lo ignoramos en silencio
            if "UNIQUE" in str(e) or "constraint" in str(e).lower():
                return False
            raise
    finally:
        conn.close()


def sync_once(*, verbose: bool = False) -> dict:
    """Ejecuta un ciclo de sincronizacion. Devuelve summary."""
    state = _get_state()
    backfill_h = int(get_conf("sync_backfill_horas") or 24)

    end = _now()
    if state.get("last_event_ts"):
        try:
            start = datetime.fromisoformat(state["last_event_ts"]) - timedelta(minutes=5)
        except Exception:
            start = end - timedelta(hours=backfill_h)
    else:
        start = end - timedelta(hours=backfill_h)

    inserted = 0
    skipped = 0
    last_ts: datetime | None = None
    error: str | None = None

    try:
        with HikClient() as hik:
            for ev in hik.iter_events(start, end):
                if _insert_marcaje(ev):
                    inserted += 1
                else:
                    skipped += 1
                if last_ts is None or ev.timestamp > last_ts:
                    last_ts = ev.timestamp
        _save_state(last_event_ts=last_ts, status="ok", error=None, count=inserted)
    except Exception as e:
        error = str(e)
        _save_state(last_event_ts=last_ts, status="error", error=error, count=inserted)

    summary = {
        "inserted": inserted,
        "skipped": skipped,
        "last_event_ts": last_ts.isoformat() if last_ts else None,
        "error": error,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
    }
    if verbose:
        print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    summary = sync_once(verbose=True)
    sys.exit(0 if summary.get("error") is None else 1)
