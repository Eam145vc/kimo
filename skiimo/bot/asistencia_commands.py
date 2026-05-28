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


# =============================================================================
# TRABAJADORES (usuarios normales): consultan SOLO su propia info.
# Se vinculan dando su cedula -> empleados.telegram_chat_id.
# =============================================================================


def empleado_por_chat(chat_id: int) -> dict | None:
    """Devuelve el empleado vinculado a este chat de Telegram, o None."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM empleados WHERE telegram_chat_id = ? AND activo = 1",
            (str(chat_id),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def vincular_por_cedula(chat_id: int, cedula: str) -> dict | None:
    """Vincula un chat de Telegram a un empleado por su cedula.

    Devuelve el empleado vinculado, o None si no existe esa cedula.
    """
    cedula = (cedula or "").strip().replace(".", "").replace(",", "")
    if not cedula.isdigit():
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM empleados WHERE cedula = ? AND activo = 1", (cedula,)
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE empleados SET telegram_chat_id = ? WHERE id = ?",
            (str(chat_id), row["id"]),
        )
        conn.commit()
        return dict(row)
    finally:
        conn.close()


async def _resumen_propio(emp_id: int, desde: str, hasta: str) -> list[dict]:
    """Reutiliza el motor del panel para traer SOLO los items de este empleado."""
    from skiimo.panel.asistencia_routes import api_resumen_diario

    # se parchea la auth porque aca ya validamos por chat de Telegram
    import skiimo.panel.asistencia_routes as _ar
    _orig = _ar._require_user
    _ar._require_user = lambda t: {"username": "bot"}
    try:
        r = await api_resumen_diario(
            session_token="bot", empleado_id=emp_id, desde=desde, hasta=hasta
        )
    finally:
        _ar._require_user = _orig
    return [i for i in r.get("items", []) if i.get("empleado_id") == emp_id]


def _h12(hms: str | None) -> str:
    """HH:MM:SS -> '5:01 p.m.' (los trabajadores ven 12h)."""
    if not hms:
        return "—"
    try:
        h, m = int(hms[:2]), hms[3:5]
        ap = "a.m." if h < 12 else "p.m."
        h = h % 12 or 12
        return f"{h}:{m} {ap}"
    except Exception:
        return hms


def _hm(horas) -> str:
    """horas decimales -> 'Xh Ymin'."""
    total = round((float(horas) or 0) * 60)
    h, m = divmod(total, 60)
    if h and m:
        return f"{h}h {m}min"
    if h:
        return f"{h}h"
    return f"{m}min"


async def cmd_mi_asistencia(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """El trabajador ve sus marcajes de HOY."""
    chat_id = update.effective_chat.id
    emp = empleado_por_chat(chat_id)
    if not emp:
        await update.message.reply_text(
            "Primero envíame tu número de cédula para identificarte."
        )
        return
    hoy = _now_bogota().date().isoformat()
    items = await _resumen_propio(emp["id"], hoy, hoy)
    if not items or not items[0].get("primera_entrada"):
        await update.message.reply_text(
            f"Hola {emp['nombre']}, hoy todavía no tienes marcajes registrados."
        )
        return
    i = items[0]
    lines = [f"*Tu asistencia de hoy* ({hoy})", ""]
    lines.append(f"🟢 Entrada: {_h12(i.get('primera_entrada'))}")
    lines.append(f"🔵 Salida almuerzo: {_h12(i.get('almuerzo_out'))}")
    lines.append(f"🔵 Regreso almuerzo: {_h12(i.get('almuerzo_in'))}")
    lines.append(f"🟠 Salida: {_h12(i.get('ultima_salida'))}")
    if i.get("extra_in"):
        lines.append(f"🟡 Extras: {_h12(i.get('extra_in'))} → {_h12(i.get('extra_out'))}")
    lines.append("")
    lines.append(f"Horas: *{_hm(i.get('horas'))}*")
    # avisar si llego tarde
    if i.get("status_entrada") == "tarde":
        lines.append("⚠️ _Llegaste tarde hoy._")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_mi_nomina(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """El trabajador ve cuánto lleva esta quincena."""
    chat_id = update.effective_chat.id
    emp = empleado_por_chat(chat_id)
    if not emp:
        await update.message.reply_text("Primero envíame tu número de cédula.")
        return
    hoy = _now_bogota().date()
    if hoy.day <= 15:
        desde = hoy.replace(day=1)
    else:
        desde = hoy.replace(day=16)
    items = await _resumen_propio(emp["id"], desde.isoformat(), hoy.isoformat())
    tot_h = sum((i.get("horas") or 0) for i in items if not i.get("es_excepcion"))
    tot_ext = sum((i.get("horas_extras") or 0) for i in items)
    tot_pago = sum((i.get("pago_total") or 0) for i in items)
    lines = [
        f"*Tu nómina* ({desde.isoformat()} → {hoy.isoformat()})", "",
        f"Horas trabajadas: *{_hm(tot_h)}*",
    ]
    if tot_ext > 0:
        lines.append(f"Horas extra: *{_hm(tot_ext)}*")
    lines.append(f"Total estimado: *${round(tot_pago):,.0f}*".replace(",", "."))
    lines.append("")
    lines.append("_Estimado, sujeto a ajustes de la administración._")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_mis_kpis(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    """El trabajador ve sus KPIs del mes (asistencia, tardanzas)."""
    chat_id = update.effective_chat.id
    emp = empleado_por_chat(chat_id)
    if not emp:
        await update.message.reply_text("Primero envíame tu número de cédula.")
        return
    from skiimo.panel.asistencia_routes import api_empleado_perfil
    import skiimo.panel.asistencia_routes as _ar
    hoy = _now_bogota().date()
    desde = hoy.replace(day=1).isoformat()
    _orig = _ar._require_user
    _ar._require_user = lambda t: {"username": "bot"}
    try:
        r = await api_empleado_perfil(
            emp_id=emp["id"], desde=desde, hasta=hoy.isoformat(), session_token="bot"
        )
    finally:
        _ar._require_user = _orig
    k = r["kpis"]
    lines = [
        f"*Tus indicadores* (mes actual)", "",
        f"Asistencia: *{k['pct_asistencia']}%*  ({k['dias_trabajados']}/{k['dias_esperados']} días)",
        f"Llegadas tarde: *{k['llegadas_tarde']}*",
        f"  - Entrada: {k.get('tarde_entrada', 0)}",
        f"  - Almuerzo: {k.get('tarde_almuerzo', 0)}",
        f"Horas trabajadas: *{_hm(k['horas_ord'])}*",
    ]
    if k.get("horas_extra", 0) > 0:
        lines.append(f"Horas extra: *{_hm(k['horas_extra'])}*")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_mi_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    emp = empleado_por_chat(update.effective_chat.id)
    if not emp:
        await update.message.reply_text(
            "Envíame tu número de cédula para identificarte y poder consultar tu información."
        )
        return
    await update.message.reply_text(
        f"Hola {emp['nombre']}. Puedes consultar:\n\n"
        "/mi_asistencia — tus marcajes de hoy\n"
        "/mi_nomina — cuánto llevas esta quincena\n"
        "/mis_kpis — tu asistencia y tardanzas del mes",
    )


# =============================================================================
# Jobs de notificacion a TRABAJADORES (solo a vinculados)
# =============================================================================


async def job_aviso_no_marco(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Avisa al TRABAJADOR vinculado que no ha marcado entrada (tras 7:15)."""
    hoy = _now_bogota().date()
    if hoy.weekday() >= 5:
        return  # findes no
    hoy_iso = hoy.isoformat()
    conn = get_conn()
    try:
        # empleados vinculados que NO marcaron hoy
        rows = conn.execute(
            """SELECT e.id, e.nombre, e.telegram_chat_id
               FROM empleados e
               WHERE e.activo = 1 AND e.telegram_chat_id IS NOT NULL
                 AND e.id NOT IN (
                   SELECT DISTINCT empleado_id FROM marcajes
                   WHERE fecha = ? AND empleado_id IS NOT NULL
                 )""",
            (hoy_iso,),
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        try:
            await context.bot.send_message(
                chat_id=int(r["telegram_chat_id"]),
                text=f"⚠️ {r['nombre']}, aún no registras tu entrada de hoy. "
                     f"No olvides marcar en el equipo.",
            )
        except Exception:
            log.exception("No se pudo avisar no-marco a %s", r["nombre"])


async def job_aviso_sin_salida(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Al final de la jornada, recuerda marcar salida a quien entró y no salió."""
    hoy = _now_bogota().date()
    if hoy.weekday() >= 5:
        return
    hoy_iso = hoy.isoformat()
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT e.id, e.nombre, e.telegram_chat_id,
                      SUM(CASE WHEN m.tipo='salida' THEN 1 ELSE 0 END) AS salidas,
                      COUNT(*) AS total
               FROM empleados e
               JOIN marcajes m ON m.empleado_id = e.id AND m.fecha = ?
               WHERE e.activo = 1 AND e.telegram_chat_id IS NOT NULL
               GROUP BY e.id""",
            (hoy_iso,),
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        if r["salidas"] == 0 and r["total"] > 0:
            try:
                await context.bot.send_message(
                    chat_id=int(r["telegram_chat_id"]),
                    text=f"🔔 {r['nombre']}, no registraste tu salida hoy. "
                         f"Recuerda marcar al salir (y el cierre de extras si trabajaste).",
                )
            except Exception:
                log.exception("No se pudo avisar sin-salida a %s", r["nombre"])
