"""Configuracion editable de asistencia. Defaults sensatos.

Permite que el dueno cambie parametros desde el panel sin tocar codigo.
Persiste en tabla `asistencia_config` (key/value).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from skiimo.db.schema import get_conn

# Defaults aplicados si la key no existe en DB
DEFAULTS: dict[str, Any] = {
    # Empleados
    "salario_minimo_2026": 1_423_500,             # COP/mes (decreto 1572/2025)
    "horas_legales_mes": 230,                     # 220-230 segun calendario; 44h * 4.33 sem
    "valor_hora_minimo": round(1_423_500 / 230),  # ~$6.189 COP/hora ordinaria

    # Jornada
    "jornada_ordinaria_diaria_h": 8.0,            # horas/dia ordinarias antes de extra
    "tolerancia_entrada_min": 10,                 # minutos tarde permitidos sin aviso
    "almuerzo_descuento_min": 60,                 # si no marcan almuerzo

    # Turno default (aplica a empleados nuevos sin turno explicito)
    "turno_default_entrada": "07:00",
    "turno_default_salida": "16:00",
    "turno_default_sabado_entrada": "07:00",
    "turno_default_sabado_salida": "12:00",
    "turno_default_dias": "1,2,3,4,5,6",           # lun-sab

    # Horas extra
    "horas_extra_requieren_aprobacion": False,     # True => el dueno aprueba via Telegram
    "limite_horas_extra_semana": 12,               # CST art. 22: maximo 12h extra/semana

    # Notificaciones
    "alertar_llegada_tarde_min": 15,               # avisar al dueno si tarda > 15min
    "alertar_no_llego_h": 1,                       # si tarda > 1h sin marcar, alertar
    "resumen_diario_hora": "19:00",                # hora del resumen diario por Telegram

    # Sync
    "sync_interval_minutes": 3,                    # cada cuanto jala marcajes
    "sync_backfill_horas": 24,                     # ventana inicial de backfill

    # Equipo Hikvision (IP local en la red de la fabrica)
    "hik_local_ip": "",
    "hik_local_port": 80,
    "hik_local_user": "admin",

    # Jornada estandar de la fabrica (usado para clasificar marcajes
    # automaticamente por hora del dia).
    # Cada marcaje cae en una de 4 franjas: entrada / almuerzo_out / almuerzo_in / salida.
    # Las ventanas son AMPLIAS para tolerar lleegadas tarde / temprano.
    "jornada_entrada_hora": "07:00",        # hora oficial de entrada
    "jornada_almuerzo_inicio": "12:00",     # hora oficial de inicio almuerzo
    "jornada_almuerzo_fin": "13:00",        # hora oficial de fin almuerzo
    "jornada_salida_hora": "17:00",         # hora oficial de salida

    # Ventana de tolerancia para entrada: si marcan ANTES de entrada-tolerancia
    # o DESPUES de almuerzo_inicio - 15 min, no es entrada (sera otra cosa).
    "tolerancia_entrada_horas": 3,          # 3 horas antes/despues de entrada cuenta como entrada
    "tolerancia_salida_horas": 6,           # 6 horas despues de salida cuenta como salida
}


def get_conf(key: str, default: Any = None) -> Any:
    """Lee una config. Cae al default de DEFAULTS si no esta en DB. Castea segun el tipo del default."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT value FROM asistencia_config WHERE key = ?", (key,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return DEFAULTS.get(key, default)

    raw = row["value"]
    default_val = DEFAULTS.get(key, default)
    # Castear al tipo del default
    if isinstance(default_val, bool):
        return raw.lower() in ("1", "true", "yes", "on")
    if isinstance(default_val, int):
        try:
            return int(raw)
        except ValueError:
            return default_val
    if isinstance(default_val, float):
        try:
            return float(raw)
        except ValueError:
            return default_val
    # str / otros
    return raw


def set_conf(key: str, value: Any, descripcion: str = "") -> None:
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO asistencia_config (key, value, descripcion, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                              descripcion = COALESCE(NULLIF(excluded.descripcion,''), asistencia_config.descripcion),
                                              updated_at = excluded.updated_at""",
            (key, str(value), descripcion, datetime.utcnow().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def get_all_conf() -> dict[str, Any]:
    """Devuelve todos los defaults con override de DB. Util para pintar el panel."""
    out = dict(DEFAULTS)
    conn = get_conn()
    try:
        rows = conn.execute("SELECT key, value FROM asistencia_config").fetchall()
    finally:
        conn.close()
    for r in rows:
        default_val = DEFAULTS.get(r["key"])
        if isinstance(default_val, bool):
            out[r["key"]] = r["value"].lower() in ("1", "true", "yes", "on")
        elif isinstance(default_val, int):
            try:
                out[r["key"]] = int(r["value"])
            except ValueError:
                pass
        elif isinstance(default_val, float):
            try:
                out[r["key"]] = float(r["value"])
            except ValueError:
                pass
        else:
            out[r["key"]] = r["value"]
    return out
