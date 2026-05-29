"""Registro de consumo de IA (uso interno, rol dev).

Estima el costo en USD por llamada a Gemini segun los tokens del usage_metadata,
para controlar el plan mensual. No visible para el cliente.
"""
from __future__ import annotations

import logging
from datetime import datetime

from skiimo.db.schema import get_conn
from skiimo.hikvision import TZ_BOGOTA

log = logging.getLogger("skiimo.uso_ia")

# Precios aproximados por 1M de tokens (Gemini Flash, ajustar si cambia el modelo).
# Sirven solo para estimar el costo interno; no es facturacion exacta.
_PRECIO_IN_POR_1M = 0.10   # USD / 1M tokens entrada
_PRECIO_OUT_POR_1M = 0.40  # USD / 1M tokens salida


def registrar_uso(usage, operacion: str = "consulta", modelo: str = "gemini") -> None:
    """Registra una llamada a la IA. `usage` es response.usage_metadata de Gemini.
    Silencioso ante cualquier error (no debe romper el flujo del bot)."""
    try:
        t_in = int(getattr(usage, "prompt_token_count", 0) or 0)
        t_out = int(getattr(usage, "candidates_token_count", 0) or 0)
        costo = (t_in / 1_000_000) * _PRECIO_IN_POR_1M + (t_out / 1_000_000) * _PRECIO_OUT_POR_1M
        ahora = datetime.now(TZ_BOGOTA)
        conn = get_conn()
        try:
            conn.execute(
                """INSERT INTO uso_ia (fecha, operacion, modelo, tokens_in, tokens_out, costo_usd, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (ahora.date().isoformat(), operacion, modelo, t_in, t_out,
                 round(costo, 6), ahora.isoformat()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        log.debug("No se pudo registrar uso de IA", exc_info=True)


def resumen_mes(anio_mes: str | None = None) -> dict:
    """Resumen del consumo de un mes (YYYY-MM). Default: mes actual (Bogota)."""
    if not anio_mes:
        anio_mes = datetime.now(TZ_BOGOTA).strftime("%Y-%m")
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT COUNT(*) AS ops,
                      COALESCE(SUM(tokens_in),0) AS t_in,
                      COALESCE(SUM(tokens_out),0) AS t_out,
                      COALESCE(SUM(costo_usd),0) AS costo
               FROM uso_ia WHERE substr(fecha,1,7) = ?""",
            (anio_mes,),
        ).fetchone()
        por_op = conn.execute(
            """SELECT operacion, COUNT(*) AS n, COALESCE(SUM(costo_usd),0) AS costo
               FROM uso_ia WHERE substr(fecha,1,7) = ?
               GROUP BY operacion ORDER BY costo DESC""",
            (anio_mes,),
        ).fetchall()
    finally:
        conn.close()
    return {
        "mes": anio_mes,
        "operaciones": row["ops"],
        "tokens_in": row["t_in"],
        "tokens_out": row["t_out"],
        "costo_usd": round(row["costo"], 4),
        "por_operacion": [dict(r) for r in por_op],
    }
