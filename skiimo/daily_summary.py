"""Resumen diario que arma el bot cada mañana.

Contiene:
  - facturas por cobrar que vencen hoy o estan vencidas
  - facturas por pagar que vencen hoy o estan vencidas
  - ventas y gastos del dia anterior
  - facturas de correo pendientes de revision
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from skiimo.db.schema import get_conn


def construir_resumen_diario() -> str:
    """Devuelve el texto markdown del resumen para mandar al admin."""
    # Sync con Siigo en vivo para datos al momento
    try:
        from skiimo.llm.tools import _sync_invoices_recientes, _sync_purchases_recientes
        _sync_invoices_recientes(dias=3)
        _sync_purchases_recientes(dias=3)
    except Exception:
        pass

    today = date.today()
    ayer = today - timedelta(days=1)
    horizonte_dias = 7

    conn = get_conn()
    try:
        # ---- POR COBRAR HOY / VENCIDAS (ventas pendientes) ----
        ventas_pend = conn.execute(
            """SELECT i.name, i.balance, i.payments_json, i.customer_ident, c.name as cname
               FROM siigo_invoices i LEFT JOIN siigo_customers c ON c.id = i.customer_id
               WHERE i.balance > 0""",
        ).fetchall()

        cobrar_hoy: list[dict] = []
        vencidas_cobrar: list[dict] = []
        proximas_cobrar: list[dict] = []
        for r in ventas_pend:
            try:
                pays = json.loads(r["payments_json"] or "[]")
            except Exception:
                continue
            due_date = None
            for p in pays:
                if p.get("due_date"):
                    due_date = p["due_date"]
                    break
            if not due_date:
                continue
            try:
                d = date.fromisoformat(due_date)
            except Exception:
                continue
            dias = (d - today).days
            entry = {
                "factura": r["name"],
                "cliente": r["cname"] or "(sin nombre)",
                "saldo": float(r["balance"]),
                "due": due_date,
                "dias": dias,
            }
            if dias < 0:
                vencidas_cobrar.append(entry)
            elif dias == 0:
                cobrar_hoy.append(entry)
            elif dias <= horizonte_dias:
                proximas_cobrar.append(entry)

        # ---- POR PAGAR HOY / VENCIDAS (compras pendientes) ----
        compras_pend = conn.execute(
            """SELECT p.name, p.balance, p.payments_json, p.supplier_ident, c.name as cname
               FROM siigo_purchases p LEFT JOIN siigo_customers c ON c.id = p.supplier_id
               WHERE p.balance > 0""",
        ).fetchall()

        pagar_hoy: list[dict] = []
        vencidas_pagar: list[dict] = []
        proximas_pagar: list[dict] = []
        for r in compras_pend:
            try:
                pays = json.loads(r["payments_json"] or "[]")
            except Exception:
                continue
            due_date = None
            for p in pays:
                if p.get("due_date"):
                    due_date = p["due_date"]
                    break
            if not due_date:
                continue
            try:
                d = date.fromisoformat(due_date)
            except Exception:
                continue
            dias = (d - today).days
            entry = {
                "factura": r["name"],
                "proveedor": r["cname"] or "(sin nombre)",
                "saldo": float(r["balance"]),
                "due": due_date,
                "dias": dias,
            }
            if dias < 0:
                vencidas_pagar.append(entry)
            elif dias == 0:
                pagar_hoy.append(entry)
            elif dias <= horizonte_dias:
                proximas_pagar.append(entry)

        # ---- VENTAS Y GASTOS DE AYER ----
        ayer_iso = ayer.isoformat()
        ventas_ayer = conn.execute(
            "SELECT COUNT(*) as n, COALESCE(SUM(total),0) as t FROM siigo_invoices WHERE date = ?",
            (ayer_iso,),
        ).fetchone()
        cobros_ayer = conn.execute(
            "SELECT COUNT(*) as n FROM bot_pedidos WHERE DATE(updated_at) = ? AND estado = 'enviado'",
            (ayer_iso,),
        ).fetchone()
        gastos_ayer = conn.execute(
            "SELECT COUNT(*) as n, COALESCE(SUM(total),0) as t FROM siigo_purchases WHERE date = ?",
            (ayer_iso,),
        ).fetchone()

        # ---- FACTURAS DE CORREO PENDIENTES DE REVISION ----
        try:
            correo_pend = conn.execute(
                "SELECT COUNT(*) as n FROM facturas_correo WHERE estado = 'pendiente'"
            ).fetchone()
            correo_pendientes = int(correo_pend["n"])
        except Exception:
            correo_pendientes = 0

        # ---- COMPROBANTES PENDIENTES DE APLICAR ----
        try:
            comp_pend = conn.execute(
                "SELECT COUNT(*) as n FROM comprobantes_procesados WHERE estado = 'pendiente'"
            ).fetchone()
            comp_pendientes = int(comp_pend["n"])
        except Exception:
            comp_pendientes = 0

    finally:
        conn.close()

    # Ordenar y limitar
    vencidas_cobrar.sort(key=lambda x: x["dias"])
    cobrar_hoy.sort(key=lambda x: -x["saldo"])
    proximas_cobrar.sort(key=lambda x: x["dias"])
    vencidas_pagar.sort(key=lambda x: x["dias"])
    pagar_hoy.sort(key=lambda x: -x["saldo"])
    proximas_pagar.sort(key=lambda x: x["dias"])

    lines: list[str] = []
    nombre_dia = today.strftime("%A %d %b %Y")
    lines.append(f"☀️ *Buenos días — {nombre_dia}*")
    lines.append("")

    # ============== POR COBRAR ==============
    if vencidas_cobrar or cobrar_hoy or proximas_cobrar:
        total_pend = sum(e["saldo"] for e in vencidas_cobrar + cobrar_hoy + proximas_cobrar)
        lines.append(f"💰 *POR COBRAR* — `${total_pend:,.0f}` en {horizonte_dias} días")
        if vencidas_cobrar:
            lines.append("")
            lines.append(f"  ⚠️ Vencidas ({len(vencidas_cobrar)}):")
            for e in vencidas_cobrar[:5]:
                lines.append(f"    • {e['cliente'][:25]} — `${e['saldo']:,.0f}` ({-e['dias']}d atrás)")
            if len(vencidas_cobrar) > 5:
                lines.append(f"    _...y {len(vencidas_cobrar)-5} más_")
        if cobrar_hoy:
            lines.append("")
            lines.append(f"  📅 Vencen HOY ({len(cobrar_hoy)}):")
            for e in cobrar_hoy[:5]:
                lines.append(f"    • {e['cliente'][:25]} — `${e['saldo']:,.0f}`")
        if proximas_cobrar:
            lines.append("")
            lines.append(f"  🔜 Próximas (en {horizonte_dias}d): {len(proximas_cobrar)}")
        lines.append("")

    # ============== POR PAGAR ==============
    if vencidas_pagar or pagar_hoy or proximas_pagar:
        total_pend = sum(e["saldo"] for e in vencidas_pagar + pagar_hoy + proximas_pagar)
        lines.append(f"💸 *POR PAGAR* — `${total_pend:,.0f}` en {horizonte_dias} días")
        if vencidas_pagar:
            lines.append("")
            lines.append(f"  ⚠️ Vencidas ({len(vencidas_pagar)}):")
            for e in vencidas_pagar[:5]:
                lines.append(f"    • {e['proveedor'][:25]} — `${e['saldo']:,.0f}` ({-e['dias']}d atrás)")
            if len(vencidas_pagar) > 5:
                lines.append(f"    _...y {len(vencidas_pagar)-5} más_")
        if pagar_hoy:
            lines.append("")
            lines.append(f"  📅 Vencen HOY ({len(pagar_hoy)}):")
            for e in pagar_hoy[:5]:
                lines.append(f"    • {e['proveedor'][:25]} — `${e['saldo']:,.0f}`")
        if proximas_pagar:
            lines.append("")
            lines.append(f"  🔜 Próximas (en {horizonte_dias}d): {len(proximas_pagar)}")
        lines.append("")

    # ============== AYER ==============
    lines.append(f"📊 *Ayer ({ayer.strftime('%A %d')})*")
    lines.append(f"  • Ventas: {ventas_ayer['n']} factura(s) — `${float(ventas_ayer['t']):,.0f}`")
    lines.append(f"  • Cobros confirmados: {cobros_ayer['n']}")
    lines.append(f"  • Gastos: {gastos_ayer['n']} factura(s) — `${float(gastos_ayer['t']):,.0f}`")

    # ============== PENDIENTES DE REVISAR ==============
    if correo_pendientes or comp_pendientes:
        lines.append("")
        lines.append("📥 *Pendientes de revisar*")
        if correo_pendientes:
            lines.append(f"  • Facturas de correo: {correo_pendientes} — usá /correos")
        if comp_pendientes:
            lines.append(f"  • Comprobantes de pago: {comp_pendientes}")

    # Si todo vacio
    if not (vencidas_cobrar or cobrar_hoy or vencidas_pagar or pagar_hoy
            or ventas_ayer["n"] or gastos_ayer["n"]):
        lines.append("")
        lines.append("_Sin pendientes destacados. Buen día._")

    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    print(construir_resumen_diario())
