"""Comandos Telegram de asistencia + jobs (sync periodico y resumen).

Comandos:
  /quien_esta        Lista quien esta adentro de la fabrica ahora mismo
  /asistencia_hoy    Resumen de marcajes del dia
  /llegadas_tarde    Empleados que llegaron tarde esta semana
  /quincena          Resumen de horas y a pagar (quincena actual)
  /asistencia_help   Ayuda

Jobs:
  job_sync_asistencia    Cada N min jala marcajes del Hikvision
  job_alerta_tarde       Cada manana ~30 min despues del turno, alerta llegadas tarde
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from skiimo.asistencia.config import get_conf
from skiimo.asistencia.sync import sync_once
from skiimo.config import ADMIN_TELEGRAM_CHAT_ID
from skiimo.db.schema import get_conn
from skiimo.hikvision import TZ_BOGOTA

log = logging.getLogger("skiimo.bot.asistencia")


def _now_bogota() -> datetime:
    return datetime.now(TZ_BOGOTA)


def _is_admin(chat_id: int) -> bool:
    return ADMIN_TELEGRAM_CHAT_ID and str(chat_id) == str(ADMIN_TELEGRAM_CHAT_ID)


# =============================================================================
# Comandos
# =============================================================================


async def cmd_quien_esta(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Lista quien esta adentro de la fabrica ahora mismo."""
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("Solo el admin puede ver asistencia.")
        return

    hoy = _now_bogota().date().isoformat()
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT m.empleado_id, e.nombre, m.tipo, m.ts
               FROM marcajes m
               LEFT JOIN empleados e ON e.id = m.empleado_id
               WHERE m.fecha = ? AND m.empleado_id IS NOT NULL
               ORDER BY m.ts DESC""",
            (hoy,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        await update.message.reply_text("Nadie ha marcado hoy todavia.")
        return

    # Ultimo tipo por empleado
    estado: dict[int, dict] = {}
    for r in rows:
        if r["empleado_id"] not in estado:
            estado[r["empleado_id"]] = {"nombre": r["nombre"], "tipo": r["tipo"], "ts": r["ts"]}

    dentro = [e for e in estado.values() if e["tipo"] in ("entrada", "almuerzo_in", "extra")]
    afuera = [e for e in estado.values() if e["tipo"] not in ("entrada", "almuerzo_in", "extra")]

    lines = [f"*Asistencia hoy* ({hoy})", ""]
    if dentro:
        lines.append(f"🟢 *Dentro ({len(dentro)}):*")
        for e in dentro:
            hora = e["ts"][11:16] if e["ts"] else ""
            lines.append(f"  • {e['nombre']}  _{e['tipo']} {hora}_")
    if afuera:
        lines.append("")
        lines.append(f"⚪ *Ya salieron ({len(afuera)}):*")
        for e in afuera:
            hora = e["ts"][11:16] if e["ts"] else ""
            lines.append(f"  • {e['nombre']}  _{e['tipo']} {hora}_")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_asistencia_hoy(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Resumen del dia: total marcajes, llegadas tarde, no marcaron."""
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("Solo admin.")
        return

    hoy = _now_bogota().date().isoformat()
    conn = get_conn()
    try:
        emp_activos = conn.execute(
            "SELECT id, nombre FROM empleados WHERE activo = 1"
        ).fetchall()
        marcajes = conn.execute(
            """SELECT m.empleado_id, e.nombre, MIN(m.ts) AS primera, MAX(m.ts) AS ultima, COUNT(*) AS n
               FROM marcajes m
               LEFT JOIN empleados e ON e.id = m.empleado_id
               WHERE m.fecha = ? AND m.empleado_id IS NOT NULL
               GROUP BY m.empleado_id""",
            (hoy,),
        ).fetchall()
    finally:
        conn.close()

    total = len(emp_activos)
    marcaron_ids = {m["empleado_id"] for m in marcajes}
    no_marcaron = [e for e in emp_activos if e["id"] not in marcaron_ids]

    lines = [f"*Asistencia {hoy}*", ""]
    lines.append(f"Empleados activos: *{total}*")
    lines.append(f"Marcaron hoy:     *{len(marcaron_ids)}*")
    lines.append(f"No marcaron:      *{len(no_marcaron)}*")
    if no_marcaron:
        lines.append("")
        lines.append("_No marcaron:_")
        for e in no_marcaron:
            lines.append(f"  • {e['nombre']}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_llegadas_tarde(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Llegadas tarde de la ultima semana."""
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("Solo admin.")
        return

    end = _now_bogota().date()
    start = end - timedelta(days=7)
    hora_esp = get_conf("turno_default_entrada") or "07:00"
    tolerancia = int(get_conf("tolerancia_entrada_min") or 10)

    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT e.nombre, m.fecha, MIN(m.ts) AS primera
               FROM marcajes m
               JOIN empleados e ON e.id = m.empleado_id
               WHERE m.fecha BETWEEN ? AND ?
               GROUP BY e.id, m.fecha
               ORDER BY m.fecha DESC""",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()

    # Calcular tardanzas
    hora_esp_t = datetime.strptime(hora_esp, "%H:%M").time()
    tardanzas = []
    for r in rows:
        try:
            ts = datetime.fromisoformat(r["primera"])
        except Exception:
            continue
        esperado = datetime.combine(ts.date(), hora_esp_t).replace(tzinfo=ts.tzinfo)
        delta_min = int((ts - esperado).total_seconds() / 60)
        if delta_min > tolerancia:
            tardanzas.append({"nombre": r["nombre"], "fecha": r["fecha"], "min": delta_min})

    if not tardanzas:
        await update.message.reply_text(f"Sin llegadas tarde en los ultimos 7 dias (tolerancia {tolerancia} min).")
        return

    lines = [f"*Llegadas tarde* (ultimos 7 dias)", f"_Tolerancia {tolerancia} min sobre {hora_esp}_", ""]
    for t in tardanzas[:30]:
        lines.append(f"  • {t['fecha']}  {t['nombre']}  _+{t['min']} min_")
    if len(tardanzas) > 30:
        lines.append(f"  …y {len(tardanzas) - 30} mas")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_quincena(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """Resumen de quincena actual con horas y pago estimado."""
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("Solo admin.")
        return

    from skiimo.asistencia.horas import calcular_dia

    hoy = _now_bogota().date()
    if hoy.day <= 15:
        inicio = hoy.replace(day=1)
        fin = hoy.replace(day=15)
    else:
        inicio = hoy.replace(day=16)
        if inicio.month == 12:
            fin = inicio.replace(year=inicio.year + 1, month=1, day=1) - timedelta(days=1)
        else:
            fin = inicio.replace(month=inicio.month + 1, day=1) - timedelta(days=1)

    conn = get_conn()
    try:
        empleados = conn.execute(
            "SELECT id, nombre, valor_hora_ord FROM empleados WHERE activo = 1 ORDER BY nombre"
        ).fetchall()
        total_pago = 0.0
        lines = [f"*Quincena {inicio.isoformat()} a {fin.isoformat()}*", ""]
        for emp in empleados:
            marc = conn.execute(
                "SELECT ts FROM marcajes WHERE empleado_id = ? AND fecha BETWEEN ? AND ? ORDER BY ts",
                (emp["id"], inicio.isoformat(), fin.isoformat()),
            ).fetchall()
            por_dia: dict[str, list[datetime]] = {}
            for r in marc:
                ts = datetime.fromisoformat(r["ts"])
                por_dia.setdefault(ts.date().isoformat(), []).append(ts)

            tot_h = 0.0
            tot_ext = 0.0
            pago = 0.0
            vh = emp["valor_hora_ord"] or 0
            for fecha_str, lista in por_dia.items():
                t = calcular_dia(date.fromisoformat(fecha_str), sorted(lista))
                tot_h += t.total_horas()
                tot_ext += (t.extra_diurnas + t.extra_nocturnas
                             + t.dom_fest_extra_diurnas + t.dom_fest_extra_nocturnas)
                pago += t.valorizar(vh)
            total_pago += pago
            if tot_h > 0:
                extra_label = f" _+{tot_ext:.1f}h extra_" if tot_ext > 0 else ""
                lines.append(f"  • {emp['nombre']}: *{tot_h:.1f}h*{extra_label}  ~${pago:,.0f}")
        lines.append("")
        lines.append(f"💰 *Total quincena: ${total_pago:,.0f}*")
    finally:
        conn.close()

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_asistencia_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "*Comandos de asistencia*\n\n"
        "/quien\\_esta — quien esta adentro ahora\n"
        "/asistencia\\_hoy — resumen del dia\n"
        "/llegadas\\_tarde — tardanzas ultimos 7 dias\n"
        "/quincena — horas y pago estimado quincena actual\n\n"
        "Panel completo en `/asistencia`, `/empleados`, `/quincena` del panel web."
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


# =============================================================================
# Jobs
# =============================================================================


async def job_sync_asistencia(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Job que jala marcajes nuevos del Hikvision."""
    try:
        summary = await asyncio.to_thread(sync_once)
        if summary.get("inserted"):
            log.info("sync asistencia: %d nuevos marcajes", summary["inserted"])
        if summary.get("error"):
            log.warning("sync asistencia error: %s", summary["error"])
    except Exception:
        log.exception("Error en job_sync_asistencia")


async def job_alerta_tarde(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cada manana ~30 min despues del turno, alerta empleados que llegaron tarde o no llegaron."""
    if not ADMIN_TELEGRAM_CHAT_ID:
        return

    hora_esp = get_conf("turno_default_entrada") or "07:00"
    tolerancia = int(get_conf("alertar_llegada_tarde_min") or 15)
    alerta_no_llego_h = int(get_conf("alertar_no_llego_h") or 1)
    hoy = _now_bogota().date()
    hoy_iso = hoy.isoformat()

    conn = get_conn()
    try:
        empleados = conn.execute(
            "SELECT id, nombre FROM empleados WHERE activo = 1"
        ).fetchall()
        primera_por_emp: dict[int, str] = {}
        for r in conn.execute(
            "SELECT empleado_id, MIN(ts) AS primera FROM marcajes WHERE fecha = ? AND empleado_id IS NOT NULL GROUP BY empleado_id",
            (hoy_iso,),
        ).fetchall():
            primera_por_emp[r["empleado_id"]] = r["primera"]
    finally:
        conn.close()

    hora_esp_t = datetime.strptime(hora_esp, "%H:%M").time()
    esperado = datetime.combine(hoy, hora_esp_t).replace(tzinfo=TZ_BOGOTA)
    ahora = _now_bogota()

    tarde = []
    no_llego = []
    for e in empleados:
        primera = primera_por_emp.get(e["id"])
        if primera:
            try:
                ts = datetime.fromisoformat(primera)
                delta_min = int((ts - esperado).total_seconds() / 60)
                if delta_min > tolerancia:
                    tarde.append({"nombre": e["nombre"], "min": delta_min})
            except Exception:
                pass
        else:
            # Si paso hace mas de N horas y no marco
            if ahora >= esperado + timedelta(hours=alerta_no_llego_h):
                no_llego.append(e["nombre"])

    if not tarde and not no_llego:
        return  # nada que reportar

    lines = [f"⚠️ *Alerta asistencia* ({hoy_iso})", ""]
    if tarde:
        lines.append(f"_Llegaron tarde (> {tolerancia} min):_")
        for t in tarde:
            lines.append(f"  • {t['nombre']}  +{t['min']} min")
    if no_llego:
        if tarde:
            lines.append("")
        lines.append("_No marcaron entrada:_")
        for n in no_llego:
            lines.append(f"  • {n}")

    try:
        await context.bot.send_message(
            chat_id=int(ADMIN_TELEGRAM_CHAT_ID),
            text="\n".join(lines),
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception:
        log.exception("No se pudo enviar alerta de tarde")
