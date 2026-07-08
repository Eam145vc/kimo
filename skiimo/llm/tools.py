"""Tools que el agente Gemini puede invocar.

Cada tool es una funcion Python pura que recibe parametros validados
y devuelve un dict serializable. Gemini las invoca por function calling.

Tools disponibles:
  - registrar_pedido(...)
  - consultar_ventas(...)
  - consultar_gastos(...)
  - top_clientes(...)
  - top_productos(...)
  - ultima_venta(...)
  - buscar_cliente(...)
  - buscar_producto(...)
  - resumen_dia(...)
  - facturas_pendientes_cobro(...)
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

from skiimo.db.schema import get_conn


# =============================================================================
# REPORTES / CONSULTAS
# =============================================================================

def _parse_periodo(periodo: str | None) -> tuple[str, str, str]:
    """Devuelve (start_iso, end_iso, label).
    periodos: 'hoy', 'ayer', 'esta_semana', 'este_mes', 'mes_pasado', 'este_anio'
    """
    today = date.today()
    p = (periodo or "este_mes").lower().replace(" ", "_")
    if p == "hoy":
        return today.isoformat(), today.isoformat(), "hoy"
    if p == "ayer":
        y = today - timedelta(days=1)
        return y.isoformat(), y.isoformat(), "ayer"
    if p in ("esta_semana", "semana"):
        start = today - timedelta(days=today.weekday())
        return start.isoformat(), today.isoformat(), "esta semana"
    if p in ("este_mes", "mes"):
        start = today.replace(day=1)
        return start.isoformat(), today.isoformat(), "este mes"
    if p == "mes_pasado":
        first_this = today.replace(day=1)
        last_prev = first_this - timedelta(days=1)
        first_prev = last_prev.replace(day=1)
        return first_prev.isoformat(), last_prev.isoformat(), "mes pasado"
    if p in ("este_anio", "este_año", "anio", "ano"):
        start = today.replace(month=1, day=1)
        return start.isoformat(), today.isoformat(), "este año"
    # default: este mes
    start = today.replace(day=1)
    return start.isoformat(), today.isoformat(), "este mes"


def consultar_ventas(periodo: str = "este_mes", vendedor_id: int | None = None) -> dict:
    """Total y cantidad de facturas de venta en un periodo."""
    # Sync ligero (ultimos 2 dias) para asegurar datos al momento
    _sync_invoices_recientes(dias=2)
    start, end, label = _parse_periodo(periodo)
    conn = get_conn()
    try:
        q = (
            "SELECT COUNT(*) as n, COALESCE(SUM(total),0) as t, "
            "COALESCE(AVG(total),0) as prom "
            "FROM siigo_invoices WHERE date >= ? AND date <= ?"
        )
        params: list = [start, end]
        if vendedor_id is not None:
            q += " AND seller_id = ?"
            params.append(vendedor_id)
        row = conn.execute(q, params).fetchone()
    finally:
        conn.close()
    return {
        "periodo": label,
        "desde": start,
        "hasta": end,
        "vendedor_id": vendedor_id,
        "cantidad_facturas": row["n"],
        "total_ventas": float(row["t"] or 0),
        "ticket_promedio": float(row["prom"] or 0),
    }


def consultar_gastos(periodo: str = "este_mes") -> dict:
    """Total y cantidad de facturas de compra en un periodo."""
    _sync_purchases_recientes(dias=2)
    start, end, label = _parse_periodo(periodo)
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) as n, COALESCE(SUM(total),0) as t "
            "FROM siigo_purchases WHERE date >= ? AND date <= ?",
            (start, end),
        ).fetchone()
    finally:
        conn.close()
    return {
        "periodo": label,
        "desde": start,
        "hasta": end,
        "cantidad_compras": row["n"],
        "total_gastos": float(row["t"] or 0),
    }


def top_clientes(periodo: str = "este_mes", limit: int = 5) -> dict:
    """Top-N clientes por monto comprado en el periodo."""
    _sync_invoices_recientes(dias=2)
    start, end, _ = _parse_periodo(periodo)
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT i.customer_ident, c.name, COUNT(*) as facturas, SUM(i.total) as total
            FROM siigo_invoices i
            LEFT JOIN siigo_customers c ON c.id = i.customer_id
            WHERE i.date >= ? AND i.date <= ?
            GROUP BY i.customer_ident, c.name
            ORDER BY total DESC
            LIMIT ?
            """,
            (start, end, limit),
        ).fetchall()
    finally:
        conn.close()
    return {
        "periodo": (start, end),
        "clientes": [
            {
                "nit": r["customer_ident"],
                "nombre": r["name"] or "(sin nombre)",
                "facturas": r["facturas"],
                "total": float(r["total"]),
            }
            for r in rows
        ],
    }


def top_productos(periodo: str = "este_mes", limit: int = 5) -> dict:
    """Top-N productos mas vendidos (por monto) en el periodo.
    Itera items_json de invoices.
    """
    _sync_invoices_recientes(dias=2)
    start, end, _ = _parse_periodo(periodo)
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT items_json FROM siigo_invoices WHERE date >= ? AND date <= ?",
            (start, end),
        ).fetchall()
    finally:
        conn.close()
    agg: dict[str, dict] = {}
    for r in rows:
        try:
            items = json.loads(r["items_json"] or "[]")
        except Exception:
            continue
        for it in items:
            code = it.get("code") or "?"
            desc = it.get("description") or ""
            qty = float(it.get("quantity") or 0)
            tot = float(it.get("total") or 0)
            key = code
            if key not in agg:
                agg[key] = {"code": code, "name": desc, "qty": 0.0, "total": 0.0}
            agg[key]["qty"] += qty
            agg[key]["total"] += tot
    sorted_ = sorted(agg.values(), key=lambda x: x["total"], reverse=True)[:limit]
    return {
        "periodo": (start, end),
        "productos": sorted_,
    }


def _sync_purchases_recientes(dias: int = 7) -> int:
    """Mismo concepto que invoices pero para facturas de COMPRA (gastos)."""
    from datetime import timedelta
    from siigo_client import SiigoClient
    start = (date.today() - timedelta(days=dias)).isoformat()
    n = 0
    try:
        with SiigoClient() as s:
            data = s.get("/v1/purchases", params={
                "created_start": start, "page_size": 100, "page": 1,
            })
    except Exception:
        return 0
    conn = get_conn()
    try:
        from datetime import datetime as _dt
        now = _dt.now().isoformat(timespec="seconds")
        for pur in (data.get("results") if isinstance(data, dict) else []) or []:
            sup = pur.get("supplier") or {}
            prov = pur.get("provider_invoice") or {}
            conn.execute(
                """INSERT INTO siigo_purchases (
                    id, name, number, document_id, date, supplier_id, supplier_ident,
                    total, balance, provider_inv_prefix, provider_inv_number, observations,
                    items_json, payments_json, raw, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    total=excluded.total, balance=excluded.balance,
                    items_json=excluded.items_json, payments_json=excluded.payments_json,
                    raw=excluded.raw, updated_at=excluded.updated_at""",
                (
                    pur["id"], pur.get("name", ""), pur.get("number"),
                    (pur.get("document") or {}).get("id"), pur.get("date", ""),
                    sup.get("id"), sup.get("identification"),
                    float(pur.get("total", 0) or 0),
                    float(pur.get("balance", 0) or 0) if pur.get("balance") is not None else None,
                    prov.get("prefix", "") if isinstance(prov, dict) else "",
                    prov.get("number", "") if isinstance(prov, dict) else "",
                    pur.get("observations", ""),
                    json.dumps(pur.get("items") or [], ensure_ascii=False),
                    json.dumps(pur.get("payments") or [], ensure_ascii=False),
                    json.dumps(pur, ensure_ascii=False),
                    (pur.get("metadata") or {}).get("created", now), now,
                ),
            )
            n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def _sync_invoices_recientes(dias: int = 7) -> int:
    """Trae las facturas creadas en los ultimos N dias de Siigo y las cachea local.
    Devuelve cuantas inserto/actualizo. Best-effort; no falla si Siigo no responde.
    """
    from datetime import timedelta
    from siigo_client import SiigoClient
    start = (date.today() - timedelta(days=dias)).isoformat()
    n = 0
    try:
        with SiigoClient() as s:
            data = s.get("/v1/invoices", params={
                "created_start": start, "page_size": 100, "page": 1,
            })
    except Exception:
        return 0
    conn = get_conn()
    try:
        from datetime import datetime as _dt
        now = _dt.now().isoformat(timespec="seconds")
        for inv in (data.get("results") if isinstance(data, dict) else []) or []:
            cust = inv.get("customer") or {}
            stamp = inv.get("stamp") or {}
            conn.execute(
                """INSERT INTO siigo_invoices (
                    id, name, number, prefix, document_id, date, customer_id, customer_ident,
                    seller_id, total, balance, stamp_status, public_url, observations,
                    items_json, payments_json, raw, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    total=excluded.total, balance=excluded.balance,
                    stamp_status=excluded.stamp_status, public_url=excluded.public_url,
                    items_json=excluded.items_json, payments_json=excluded.payments_json,
                    raw=excluded.raw, updated_at=excluded.updated_at""",
                (
                    inv["id"], inv.get("name", ""), inv.get("number"), inv.get("prefix", ""),
                    (inv.get("document") or {}).get("id"), inv.get("date", ""),
                    cust.get("id"), cust.get("identification"), inv.get("seller"),
                    float(inv.get("total", 0) or 0),
                    float(inv.get("balance", 0) or 0) if inv.get("balance") is not None else None,
                    stamp.get("status") if isinstance(stamp, dict) else None,
                    inv.get("public_url", ""), inv.get("observations", ""),
                    json.dumps(inv.get("items") or [], ensure_ascii=False),
                    json.dumps(inv.get("payments") or [], ensure_ascii=False),
                    json.dumps(inv, ensure_ascii=False),
                    (inv.get("metadata") or {}).get("created", now), now,
                ),
            )
            n += 1
        conn.commit()
    finally:
        conn.close()
    return n


def ultima_venta(vendedor_id: int | None = None) -> dict:
    """Devuelve la factura mas reciente (opcionalmente filtrada por vendedor).
    Antes de consultar el espejo local, sincroniza las facturas creadas en los
    ultimos 7 dias desde Siigo. Asi siempre devuelve la MAS reciente real,
    incluso si fue creada desde Siigo web u otra integracion.
    """
    # Sync ligero: trae lo de los ultimos 7 dias (max 100 facturas, una sola llamada API)
    _sync_invoices_recientes(dias=7)

    conn = get_conn()
    try:
        q = (
            "SELECT i.id, i.name, i.date, i.total, i.customer_ident, c.name as cname, "
            "i.public_url, i.items_json, i.seller_id "
            "FROM siigo_invoices i LEFT JOIN siigo_customers c ON c.id = i.customer_id"
        )
        params: list = []
        if vendedor_id is not None:
            q += " WHERE i.seller_id = ?"
            params.append(vendedor_id)
        q += " ORDER BY i.date DESC, i.created_at DESC LIMIT 1"
        row = conn.execute(q, params).fetchone()
    finally:
        conn.close()
    if not row:
        return {"encontrado": False}
    items = json.loads(row["items_json"] or "[]")
    return {
        "encontrado": True,
        "factura": row["name"],
        "fecha": row["date"],
        "total": float(row["total"]),
        "cliente_nit": row["customer_ident"],
        "cliente_nombre": row["cname"] or "(sin nombre)",
        "vendedor_id": row["seller_id"],
        "items": [
            {"code": i.get("code"), "desc": i.get("description"), "qty": i.get("quantity"), "total": i.get("total")}
            for i in items
        ],
        "pdf_url": row["public_url"],
    }


def resumen_dia(dia: str | None = None) -> dict:
    """Resumen del dia: ventas, gastos, balance.
    Sincroniza con Siigo en vivo para tener datos del momento.
    """
    _sync_invoices_recientes(dias=2)
    _sync_purchases_recientes(dias=2)
    d = dia or date.today().isoformat()
    conn = get_conn()
    try:
        v = conn.execute(
            "SELECT COUNT(*) as n, COALESCE(SUM(total),0) as t FROM siigo_invoices WHERE date = ?",
            (d,),
        ).fetchone()
        g = conn.execute(
            "SELECT COUNT(*) as n, COALESCE(SUM(total),0) as t FROM siigo_purchases WHERE date = ?",
            (d,),
        ).fetchone()
    finally:
        conn.close()
    return {
        "dia": d,
        "ventas_cantidad": v["n"],
        "ventas_total": float(v["t"]),
        "gastos_cantidad": g["n"],
        "gastos_total": float(g["t"]),
        "balance": float(v["t"]) - float(g["t"]),
    }


def buscar_cliente(query: str, limit: int = 5) -> dict:
    """Busca clientes por nombre o NIT en el espejo local."""
    from skiimo.matcher import Matcher
    m = Matcher()
    hits = m.search_customer(query, limit=limit)
    return {
        "query": query,
        "resultados": [
            {
                "id": h.id,
                "nit": h.identification,
                "nombre": h.name,
                "email": h.email,
                "score": h.score,
            }
            for h in hits
        ],
    }


def buscar_producto(query: str, limit: int = 5) -> dict:
    """Busca productos por nombre o codigo en el espejo local."""
    from skiimo.matcher import Matcher
    m = Matcher()
    hits = m.search_product(query, limit=limit)
    return {
        "query": query,
        "resultados": [
            {
                "id": h.id,
                "code": h.code,
                "nombre": h.name,
                "familia": h.account_group_name,
                "precio": h.price_default,
                "iva_pct": h.iva_percentage,
                "score": h.score,
            }
            for h in hits
        ],
    }


def facturas_proveedor_pendientes(limit: int = 20) -> dict:
    """Facturas de COMPRA con saldo pendiente (lo que TU debes a proveedores)."""
    # Pre-sync: si el contador pago en Siigo web o llego una FC nueva, verla
    _sync_purchases_recientes(dias=14)
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT p.name, p.date, p.total, p.balance, p.supplier_ident, c.name as cname,
                      p.provider_inv_prefix, p.provider_inv_number, p.payments_json
               FROM siigo_purchases p LEFT JOIN siigo_customers c ON c.id = p.supplier_id
               WHERE p.balance > 0
               ORDER BY p.date ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    today = date.today()
    facturas = []
    for r in rows:
        # extraer due_date del primer payment a credito
        due_date = None
        dias_vencido = None
        try:
            payments = json.loads(r["payments_json"] or "[]")
            for pay in payments:
                if pay.get("due_date"):
                    due_date = pay["due_date"]
                    try:
                        d = date.fromisoformat(due_date)
                        dias_vencido = (today - d).days
                    except Exception:
                        pass
                    break
        except Exception:
            pass
        facturas.append({
            "factura_siigo": r["name"],
            "factura_proveedor": f'{r["provider_inv_prefix"] or ""}-{r["provider_inv_number"] or ""}'.strip("-"),
            "fecha": r["date"],
            "total": float(r["total"]),
            "saldo": float(r["balance"]),
            "proveedor_nit": r["supplier_ident"],
            "proveedor_nombre": r["cname"] or "(sin nombre)",
            "vencimiento": due_date,
            "dias_vencido": dias_vencido,  # positivo = vencida, negativo = futura
        })
    total = sum(f["saldo"] for f in facturas)
    vencidas = sum(1 for f in facturas if f["dias_vencido"] is not None and f["dias_vencido"] > 0)
    return {"facturas": facturas, "total_pendiente": total, "cantidad": len(facturas), "vencidas": vencidas}


def proveedores_a_pagar(limit: int = 10) -> dict:
    """Agrupa por proveedor: total adeudado, cantidad de facturas, factura mas antigua."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT p.supplier_ident, c.name as nombre, c.email as email,
                      COUNT(*) as facturas, SUM(p.balance) as deuda,
                      MIN(p.date) as factura_mas_antigua
               FROM siigo_purchases p LEFT JOIN siigo_customers c ON c.id = p.supplier_id
               WHERE p.balance > 0
               GROUP BY p.supplier_ident, c.name, c.email
               ORDER BY deuda DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return {
        "proveedores": [
            {
                "nit": r["supplier_ident"],
                "nombre": r["nombre"] or "(sin nombre)",
                "email": r["email"] or "",
                "facturas_pendientes": r["facturas"],
                "deuda_total": float(r["deuda"]),
                "factura_mas_antigua": r["factura_mas_antigua"],
            }
            for r in rows
        ],
        "total_general": sum(float(r["deuda"]) for r in rows),
    }


def vencimientos_proximos(dias: int = 7) -> dict:
    """Facturas de compra cuyo vencimiento cae en los proximos N dias (o ya vencidas)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT p.name, p.balance, p.payments_json, p.supplier_ident, c.name as cname
               FROM siigo_purchases p LEFT JOIN siigo_customers c ON c.id = p.supplier_id
               WHERE p.balance > 0""",
        ).fetchall()
    finally:
        conn.close()
    today = date.today()
    horizonte = today + timedelta(days=dias)
    proximas: list[dict] = []
    vencidas: list[dict] = []
    for r in rows:
        try:
            payments = json.loads(r["payments_json"] or "[]")
        except Exception:
            continue
        for pay in payments:
            dd = pay.get("due_date")
            if not dd:
                continue
            try:
                d = date.fromisoformat(dd)
            except Exception:
                continue
            item = {
                "factura": r["name"],
                "proveedor_nit": r["supplier_ident"],
                "proveedor_nombre": r["cname"] or "(sin nombre)",
                "saldo": float(r["balance"]),
                "vencimiento": dd,
                "dias": (d - today).days,
            }
            if d < today:
                vencidas.append(item)
            elif d <= horizonte:
                proximas.append(item)
            break
    vencidas.sort(key=lambda x: x["dias"])  # mas antiguas primero
    proximas.sort(key=lambda x: x["dias"])
    return {
        "dias_horizonte": dias,
        "vencidas": vencidas,
        "proximas": proximas,
        "total_vencido": sum(v["saldo"] for v in vencidas),
        "total_proximo": sum(p["saldo"] for p in proximas),
    }


def repetir_pedido_cliente(cliente_query: str, n: int = 1) -> dict:
    """Devuelve el pedido N-esimo mas reciente de un cliente (por defecto el ultimo).
    Resuelto en items con codigo, descripcion, cantidad, precio promedio cobrado.
    Util para que el vendedor diga 'mandale lo de siempre a X'.
    """
    from skiimo.matcher import Matcher
    m = Matcher()
    # Buscamos hasta 5 candidatos y preferimos los que tengan historial de compras
    hits = m.search_customer(cliente_query, limit=5)
    if not hits:
        return {"error": f"Cliente '{cliente_query}' no encontrado"}

    # Re-ranquear: cliente con mas facturas gana
    conn = get_conn()
    try:
        scored = []
        for h in hits:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM siigo_invoices WHERE customer_id = ?",
                (h.id,),
            ).fetchone()[0]
            scored.append((cnt, h))
        scored.sort(key=lambda x: x[0], reverse=True)
    finally:
        conn.close()
    c = scored[0][1] if scored[0][0] > 0 else hits[0]
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT name, date, total, items_json, payments_json FROM siigo_invoices
               WHERE customer_id = ? ORDER BY date DESC, created_at DESC
               LIMIT ?""",
            (c.id, max(n, 1)),
        ).fetchall()
    finally:
        conn.close()
    if not rows or len(rows) < n:
        return {
            "cliente": c.name,
            "nit": c.identification,
            "error": f"Sin pedidos previos suficientes (encontradas {len(rows)})",
        }
    inv = rows[n - 1]
    items = json.loads(inv["items_json"] or "[]")
    items_out = []
    for it in items:
        items_out.append({
            "code": it.get("code"),
            "descripcion": it.get("description"),
            "cantidad": it.get("quantity"),
            "precio_unitario_anterior": it.get("price"),
            "total_anterior": it.get("total"),
        })
    payments = json.loads(inv["payments_json"] or "[]")
    return {
        "cliente": c.name,
        "nit": c.identification,
        "factura_referencia": inv["name"],
        "fecha_referencia": inv["date"],
        "total_referencia": float(inv["total"]),
        "items": items_out,
        "forma_pago_anterior": payments[0].get("name") if payments else None,
        "instruccion": "Sugiere al usuario armar el mismo pedido. Si confirma, llamar a registrar_pedido con esos items.",
    }


def estado_cuenta_cliente(cliente_query: str) -> dict:
    """Estado de cuenta completo de un cliente: deuda, facturas pendientes,
    historial de pago, categoria y pronto pago configurado.
    """
    from datetime import date as _date
    from skiimo.matcher import Matcher
    from skiimo.pricing.engine import get_categoria_cliente, obtener_pronto_pago

    # Pre-sync: traer facturas recientes (puede haber cambiado el saldo si el cliente pago en Siigo web)
    _sync_invoices_recientes(dias=7)

    m = Matcher()
    hits = m.search_customer(cliente_query, limit=5)
    if not hits:
        return {"error": f"Cliente '{cliente_query}' no encontrado"}

    # Preferir cliente con historial de compras (mas facturas)
    conn = get_conn()
    try:
        scored = []
        for h in hits:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM siigo_invoices WHERE customer_id = ?",
                (h.id,),
            ).fetchone()[0]
            scored.append((cnt, h))
        scored.sort(key=lambda x: x[0], reverse=True)
        c = scored[0][1] if scored[0][0] > 0 else hits[0]
    finally:
        conn.close()

    conn = get_conn()
    try:
        # Facturas pendientes (saldo > 0)
        pendientes = conn.execute(
            """SELECT name, date, total, balance, payments_json FROM siigo_invoices
               WHERE customer_id = ? AND balance > 0
               ORDER BY date ASC""",
            (c.id,),
        ).fetchall()

        # Estadisticas ultimos 12 meses
        stats = conn.execute(
            """SELECT COUNT(*) as n, COALESCE(SUM(total),0) as t, COALESCE(AVG(total),0) as avg
               FROM siigo_invoices
               WHERE customer_id = ? AND date >= date('now', '-365 days')""",
            (c.id,),
        ).fetchone()

        # TODO: tiempo promedio de pago cuando sincronicemos vouchers (recibos de caja)
    finally:
        conn.close()

    today = _date.today()
    pendientes_out = []
    total_pendiente = 0.0
    vencidas = 0
    for p in pendientes:
        bal = float(p["balance"])
        total_pendiente += bal
        # Vencimiento desde payments_json
        venc = None
        try:
            pays = json.loads(p["payments_json"] or "[]")
            for pay in pays:
                if pay.get("due_date"):
                    venc = pay["due_date"]
                    break
        except Exception:
            pass
        dias_venc = None
        if venc:
            try:
                d = _date.fromisoformat(venc)
                dias_venc = (today - d).days
                if dias_venc > 0:
                    vencidas += 1
            except Exception:
                pass
        pendientes_out.append({
            "factura": p["name"],
            "fecha": p["date"],
            "total": float(p["total"]),
            "saldo": bal,
            "vencimiento": venc,
            "dias_vencido": dias_venc,
        })

    categoria = get_categoria_cliente(c.id)
    escalas_pp = obtener_pronto_pago(c.id)

    return {
        "cliente": c.name,
        "nit": c.identification,
        "categoria": categoria,
        "deuda_total": total_pendiente,
        "facturas_pendientes": len(pendientes_out),
        "facturas_vencidas": vencidas,
        "detalle_pendientes": pendientes_out,
        "compras_12m": {
            "cantidad": stats["n"],
            "total": float(stats["t"]),
            "ticket_promedio": float(stats["avg"]),
        },
        "pronto_pago_configurado": [
            {"dias": e["dias_max"], "descuento_pct": e["descuento_pct"]} for e in escalas_pp
        ],
    }


def consultar_stock(producto_query: str) -> dict:
    """Stock actual de un producto + estadisticas de rotacion para estimar
    dias de cobertura.
    """
    from skiimo.matcher import Matcher
    m = Matcher()
    hits = m.search_product(producto_query, limit=3)
    if not hits:
        return {"error": f"Producto '{producto_query}' no encontrado"}

    conn = get_conn()
    try:
        out_productos = []
        for h in hits:
            raw_row = conn.execute(
                "SELECT raw, available_quantity FROM siigo_products WHERE code = ?",
                (h.code,),
            ).fetchone()
            available = float(raw_row["available_quantity"] or 0) if raw_row else 0.0

            # Ventas ultimos 30 dias para calcular rotacion
            rows = conn.execute(
                """SELECT items_json FROM siigo_invoices WHERE date >= date('now', '-30 days')"""
            ).fetchall()
            unidades_30d = 0.0
            for r in rows:
                try:
                    items = json.loads(r["items_json"])
                except Exception:
                    continue
                for it in items:
                    if it.get("code") == h.code:
                        unidades_30d += float(it.get("quantity") or 0)
            promedio_diario = unidades_30d / 30 if unidades_30d else 0
            dias_cobertura = (available / promedio_diario) if promedio_diario > 0 else None

            # Alerta si stock cubre menos de 7 dias
            alerta = None
            if dias_cobertura is not None and dias_cobertura < 7:
                alerta = f"BAJO: solo {dias_cobertura:.1f} dias de cobertura"
            elif available == 0:
                alerta = "SIN STOCK"

            out_productos.append({
                "code": h.code,
                "nombre": h.name,
                "familia": h.account_group_name,
                "stock_disponible": available,
                "unidades_30d": unidades_30d,
                "promedio_dia": round(promedio_diario, 1),
                "dias_cobertura": round(dias_cobertura, 1) if dias_cobertura is not None else None,
                "alerta": alerta,
            })
    finally:
        conn.close()
    return {"query": producto_query, "productos": out_productos}


def proponer_anular_ultima_factura_cliente(
    cliente_query: str,
    n: int = 1,
    motivo: str = "Anulacion solicitada por admin",
) -> dict:
    """Propone anular la N-esima factura mas reciente de un cliente.

    n=1 -> ultima
    n=2 -> penultima
    n=3 -> antepenultima
    etc.

    Usar cuando el admin dice:
    - 'anula la ultima factura de Hugo'
    - 'cancela la penultima factura de Diego'
    - 'anulame el ultimo pedido de Maria'
    - 'borra la antepenultima de X'
    """
    from skiimo.matcher import Matcher
    m = Matcher()
    hits = m.search_customer(cliente_query, limit=5)
    if not hits:
        return {"error": f"Cliente '{cliente_query}' no encontrado"}

    # Re-rankear: cliente con mas facturas gana
    conn = get_conn()
    try:
        scored = []
        for h in hits:
            cnt = conn.execute(
                "SELECT COUNT(*) FROM siigo_invoices WHERE customer_id = ?",
                (h.id,),
            ).fetchone()[0]
            scored.append((cnt, h))
        scored.sort(key=lambda x: x[0], reverse=True)
        c = scored[0][1] if scored[0][0] > 0 else hits[0]

        # Buscar las facturas mas recientes
        rows = conn.execute(
            """SELECT name, date FROM siigo_invoices
               WHERE customer_id = ?
               ORDER BY date DESC, created_at DESC
               LIMIT ?""",
            (c.id, max(n, 1) + 2),  # traigo unas extras por si acaso
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return {
            "error": f"{c.name} no tiene facturas registradas",
            "cliente": c.name,
        }
    if len(rows) < n:
        return {
            "error": f"{c.name} solo tiene {len(rows)} factura(s), no la #{n}",
            "cliente": c.name,
            "facturas_disponibles": [r["name"] for r in rows],
        }

    factura_target = rows[n - 1]["name"]
    # Delegar a proponer_anular_factura usando el nombre exacto
    resultado = proponer_anular_factura(factura_target, motivo)
    if isinstance(resultado, dict) and resultado.get("pendiente_confirmacion_anulacion"):
        ordinal = {1: "ultima", 2: "penultima", 3: "antepenultima"}.get(n, f"#{n}")
        resultado["cliente_match"] = c.name
        resultado["ordinal"] = ordinal
    return resultado


def agregar_usuario(
    chat_id: int,
    nombre: str,
    rol: str = "vendedor",
    siigo_seller_id: int | None = None,
) -> dict:
    """Registra a un usuario en bot_vendedores para que pueda usar el bot.

    Usar cuando el admin dice:
    - 'agrega como admin al chat_id 123456 llamado Maria'
    - 'da de alta a Frank con chat 999, rol vendedor'
    - 'registra al chat 555 como admin con nombre Juan'

    chat_id: el chat_id de Telegram del nuevo usuario
    nombre: como llamarlo (ej. 'Maria', 'Frank Tabares')
    rol: 'admin' o 'vendedor' (default vendedor)
    siigo_seller_id: id de vendedor en Siigo. Si no se especifica, usa el default del .env.
    """
    from datetime import datetime as _dt
    from skiimo.config import DEFAULT_SELLER_ID

    rol_clean = (rol or "vendedor").lower().strip()
    if rol_clean not in ("admin", "vendedor"):
        return {"error": f"Rol invalido '{rol}'. Debe ser 'admin' o 'vendedor'."}

    seller = int(siigo_seller_id) if siigo_seller_id else DEFAULT_SELLER_ID

    conn = get_conn()
    try:
        # Verificar si ya existe
        existing = conn.execute(
            "SELECT nombre, rol, activo FROM bot_vendedores WHERE telegram_chat_id = ?",
            (chat_id,),
        ).fetchone()
        accion = "actualizado" if existing else "creado"
        conn.execute(
            """INSERT INTO bot_vendedores (telegram_chat_id, nombre, siigo_seller_id, rol, activo, created_at)
               VALUES (?, ?, ?, ?, 1, ?)
               ON CONFLICT(telegram_chat_id) DO UPDATE SET
                 nombre = excluded.nombre,
                 siigo_seller_id = excluded.siigo_seller_id,
                 rol = excluded.rol,
                 activo = 1""",
            (chat_id, nombre, seller, rol_clean,
             _dt.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": True,
        "accion": accion,
        "chat_id": chat_id,
        "nombre": nombre,
        "rol": rol_clean,
        "siigo_seller_id": seller,
    }


def listar_usuarios() -> dict:
    """Lista los usuarios registrados en el bot (vendedores y admins)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT telegram_chat_id, nombre, rol, activo, siigo_seller_id, created_at
               FROM bot_vendedores ORDER BY rol DESC, created_at"""
        ).fetchall()
    finally:
        conn.close()
    return {
        "usuarios": [
            {
                "chat_id": r["telegram_chat_id"],
                "nombre": r["nombre"],
                "rol": r["rol"],
                "activo": bool(r["activo"]),
                "siigo_seller_id": r["siigo_seller_id"],
            }
            for r in rows
        ],
    }


def desactivar_usuario(chat_id: int) -> dict:
    """Desactiva un usuario para que no pueda usar el bot.
    Usar cuando el admin dice: 'sacale acceso al chat X', 'desactiva a Y'.
    No lo borra, solo marca activo=0.
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT nombre FROM bot_vendedores WHERE telegram_chat_id = ?",
            (chat_id,),
        ).fetchone()
        if not row:
            return {"error": f"chat_id {chat_id} no esta registrado"}
        conn.execute(
            "UPDATE bot_vendedores SET activo = 0 WHERE telegram_chat_id = ?",
            (chat_id,),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "chat_id": chat_id, "nombre": row["nombre"], "estado": "desactivado"}


def proponer_anular_factura(factura: str, motivo: str = "Anulacion solicitada por admin") -> dict:
    """Propone anular una factura de venta. NO la anula directamente; devuelve info
    para que el sistema muestre botones de confirmacion al admin.

    Usar cuando el admin dice: 'anula la factura X', 'cancela esa factura',
    'borra el pedido Y', 'tira esa factura'.

    factura: nombre (FV-1-5192) o consecutivo (5192).
    motivo: motivo de la anulacion para el audit log.
    """
    from skiimo.siigo_payments import _buscar_factura_por_nombre
    inv = _buscar_factura_por_nombre(factura)
    if not inv:
        return {"error": f"Factura '{factura}' no encontrada"}

    # Refrescar saldo desde Siigo
    from skiimo.siigo_payments import _refresh_balance_from_siigo
    saldo_actual = _refresh_balance_from_siigo(inv["id"])
    if saldo_actual is None:
        saldo_actual = float(inv.get("balance") or 0)

    total = float(inv.get("total") or 0)
    fue_pagada = saldo_actual < total - 0.5

    # Tipo de doc para decidir camino
    from skiimo.config import INVOICE_DOC_ID_ELECTRONIC
    doc_id = int(inv.get("document_id") or 0)
    # 27703 era la FV electronica de la cuenta vieja (facturas historicas)
    es_electronica = doc_id in (INVOICE_DOC_ID_ELECTRONIC, 27703)  # FV-2 con DIAN
    stamp_status = inv.get("stamp_status")
    tiene_stamp = stamp_status in ("Accepted", "ACCEPTED")

    # Recomendar mecanismo
    metodo_recomendado = "annul"
    razones = []
    if es_electronica or tiene_stamp:
        metodo_recomendado = "credit_note"
        razones.append("factura electronica con CUFE DIAN - obligatorio nota credito")
    if fue_pagada:
        metodo_recomendado = "credit_note"
        razones.append(f"factura tiene cobros (saldo ${saldo_actual:,.0f} < total ${total:,.0f})")

    return {
        "pendiente_confirmacion_anulacion": True,
        "factura_id": inv["id"],
        "factura_name": inv["name"],
        "total": total,
        "saldo": saldo_actual,
        "fecha": inv.get("date"),
        "cliente_ident": inv.get("customer_ident"),
        "doc_id": doc_id,
        "es_electronica": es_electronica,
        "fue_pagada": fue_pagada,
        "metodo_recomendado": metodo_recomendado,
        "razones": razones,
        "motivo": motivo[:200],
    }


def analizar_pago_a_proveedor(compra: str, monto: float, metodo_pago: str = "banco_ahorros") -> dict:
    """Analiza un pago saliente sobre una factura de COMPRA y devuelve opciones.

    Usar cuando el usuario dice 'pague a X', 'le transferi a proveedor Y',
    'salio pago para Arqui', 'cubri la factura EI-23573'.

    compra: nombre de la factura de compra (FC-1-417) o numero de factura del proveedor.
    """
    from skiimo.siigo_payments import analizar_pago_proveedor
    a = analizar_pago_proveedor(compra, monto)
    if a is None:
        return {"error": f"Factura de compra '{compra}' no encontrada"}
    return {
        "pendiente_confirmacion_pago_proveedor": True,
        "compra": a.compra_name,
        "compra_id": a.compra_id,
        "proveedor": a.proveedor_nombre,
        "proveedor_id": a.proveedor_id,
        "total_compra": a.compra_total,
        "saldo_actual": a.compra_balance,
        "monto_pagado": a.monto_pagado,
        "diferencia": round(a.diferencia, 2),
        "diferencia_pct": round(a.diferencia_pct, 1),
        "fecha_compra": a.fecha_compra.isoformat(),
        "fecha_pago": a.fecha_pago.isoformat(),
        "dias_transcurridos": a.dias_transcurridos,
        "metodo_pago": metodo_pago,
        "opciones": a.opciones,
    }


def analizar_pago_factura(factura: str, monto: float, metodo_pago: str = "efectivo") -> dict:
    """Analiza un pago entrante sobre una factura y devuelve opciones para confirmar.

    factura: nombre de la factura (FV-1-5192 o solo 5192).
    monto: lo que el cliente pago (con IVA).
    metodo_pago: efectivo, nequi, daviplata, banco_ahorros, tarjeta_debito, tarjeta_credito.

    NOTA: este analisis NO registra el pago. Solo devuelve las opciones para que el
    usuario elija via botones en Telegram.
    """
    from skiimo.siigo_payments import analizar_pago
    a = analizar_pago(factura, monto)
    if a is None:
        return {"error": f"Factura '{factura}' no encontrada"}
    return {
        "pendiente_confirmacion": True,
        "factura": a.factura_name,
        "factura_id": a.factura_id,
        "cliente": a.cliente_nombre,
        "total_factura": a.factura_total,
        "saldo_actual": a.factura_balance,
        "monto_pagado": a.monto_pagado,
        "diferencia": round(a.diferencia, 2),
        "diferencia_pct": round(a.diferencia_pct, 1),
        "fecha_factura": a.fecha_factura.isoformat(),
        "fecha_pago": a.fecha_pago.isoformat(),
        "dias_transcurridos": a.dias_transcurridos,
        "metodo_pago": metodo_pago,
        "opciones": a.opciones,
    }


def consultar_precio(product_query: str, categoria: str = "DETAL") -> dict:
    """Devuelve el precio oficial vigente de un producto en una categoria."""
    from skiimo.matcher import Matcher
    m = Matcher()
    hits = m.search_product(product_query, limit=3)
    if not hits:
        return {"error": f"Producto '{product_query}' no encontrado"}
    h = hits[0]
    cat = (categoria or "DETAL").upper()
    if cat not in ("DETAL", "MAYORISTA", "DISTRIBUIDOR"):
        cat = "DETAL"
    conn = get_conn()
    try:
        # Todas las listas
        rows = conn.execute(
            "SELECT lista, precio_pre_iva, precio_con_iva, fuente, confirmed_by "
            "FROM precios_oficiales WHERE product_code = ?",
            (h.code,),
        ).fetchall()
    finally:
        conn.close()
    precios = {r["lista"]: {"pre_iva": float(r["precio_pre_iva"]),
                             "con_iva": float(r["precio_con_iva"] or 0),
                             "fuente": r["fuente"],
                             "confirmado_dueno": bool(r["confirmed_by"] == "dueno")} for r in rows}
    return {
        "producto": {"code": h.code, "nombre": h.name, "familia": h.account_group_name},
        "precios": precios,
    }


def cambiar_precio(
    product_query: str,
    categoria: str,
    nuevo_precio_con_iva: float,
    actor: str = "chat",
) -> dict:
    """Cambia el precio oficial de un producto en una categoria especifica.

    El precio se entiende como CON IVA (ej: 27000 = $27,000 con IVA).
    El sistema calcula el pre-IVA automaticamente.
    Solo ADMIN debe poder usar esto (se valida en el agente).
    """
    from skiimo.matcher import Matcher
    m = Matcher()
    hits = m.search_product(product_query, limit=1)
    if not hits:
        return {"ok": False, "error": f"Producto '{product_query}' no encontrado"}
    h = hits[0]
    cat = (categoria or "").upper()
    if cat not in ("DETAL", "MAYORISTA", "DISTRIBUIDOR"):
        return {"ok": False, "error": f"Categoria '{categoria}' invalida. Use DETAL, MAYORISTA o DISTRIBUIDOR."}
    if nuevo_precio_con_iva <= 0:
        return {"ok": False, "error": "Precio invalido"}

    iva_pct = h.iva_percentage or 19.0
    factor = 1.0 + (iva_pct / 100.0)
    nuevo_pre_iva = nuevo_precio_con_iva / factor

    conn = get_conn()
    try:
        # Obtener product_id y precio anterior
        prod = conn.execute("SELECT id FROM siigo_products WHERE code = ?", (h.code,)).fetchone()
        if not prod:
            return {"ok": False, "error": "Producto no encontrado en DB"}
        anterior = conn.execute(
            "SELECT precio_con_iva FROM precios_oficiales WHERE product_id = ? AND lista = ?",
            (prod["id"], cat),
        ).fetchone()
        precio_anterior = float(anterior["precio_con_iva"]) if anterior else None

        from datetime import datetime as _dt
        now = _dt.now().isoformat(timespec="seconds")
        conn.execute(
            """INSERT INTO precios_oficiales
               (product_id, product_code, lista, precio_pre_iva, precio_con_iva,
                fuente, ventas_referencia, confirmed_by, confirmed_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'manual_chat', 0, 'dueno', ?, ?)
               ON CONFLICT(product_id, lista) DO UPDATE SET
                 precio_pre_iva = excluded.precio_pre_iva,
                 precio_con_iva = excluded.precio_con_iva,
                 fuente = 'manual_chat',
                 confirmed_by = 'dueno',
                 confirmed_at = excluded.confirmed_at,
                 updated_at = excluded.updated_at""",
            (prod["id"], h.code, cat, nuevo_pre_iva, nuevo_precio_con_iva, now, now),
        )
        # Audit
        conn.execute(
            """INSERT INTO audit_log (entity, entity_id, action, actor, payload, created_at)
               VALUES ('precio', ?, 'cambio_precio', ?, ?, ?)""",
            (h.code, actor,
             json.dumps({"categoria": cat, "anterior_con_iva": precio_anterior,
                         "nuevo_con_iva": nuevo_precio_con_iva,
                         "producto": h.name}, ensure_ascii=False),
             now),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": True,
        "producto": h.name,
        "code": h.code,
        "categoria": cat,
        "precio_anterior_con_iva": precio_anterior,
        "precio_nuevo_con_iva": nuevo_precio_con_iva,
        "precio_nuevo_pre_iva": round(nuevo_pre_iva, 2),
    }


def cambiar_precios_grupo(
    grupo: str,
    detal: float | None = None,
    mayorista: float | None = None,
    distribuidor: float | None = None,
    aumento_pct: float | None = None,
    actor: str = "chat",
) -> dict:
    """Cambia precios oficiales de TODO un grupo de productos al mismo precio.

    Usar cuando el usuario dice cosas como:
      - 'pon todos los sachets con licor mayorista a 2000'
      - 'todas las bolsas 6L con licor detal 26 mayor 24 distrib 20'
      - 'sube 5% mayorista de todos los sachets' (aumento_pct=5)
      - 'baja 10% distribuidor de las perlas 1200gr' (aumento_pct=-10)

    Args:
      grupo: filtro del grupo. Ejemplos validos:
        'bolsas 6L con licor', 'bolsas 6L sin licor',
        'cremosos',
        'sachets con licor', 'sachets sin licor', 'sachets' (todos),
        'perlas 350gr', 'perlas 1200gr', 'perlas 3400gr', 'perlas' (todas),
        'gelatinas 330gr', 'gelatinas 1200gr', 'gelatinas 2300gr', 'gelatinas',
        'siropes 360ml', 'siropes 1000ml', 'siropes',
        'sales 250gr', 'sales 500gr', 'sales'.
      detal / mayorista / distribuidor: precio CON IVA para cada lista.
        Solo se actualizan las listas que se especifiquen (no-None).
      aumento_pct: alternativa a precios absolutos. Aplica un % al precio actual.
        Positivo sube, negativo baja. Si se usa, ignora detal/mayorista/distribuidor.

    Devuelve: {ok, grupo, productos_afectados, listas_cambiadas, detalle}
    """
    # Mapeo grupo -> filtro SQL
    g = (grupo or "").lower().strip()

    def match_filtro() -> tuple[str, str, list]:
        """Devuelve (where_clause, descripcion, params)."""
        # Bolsas 6L
        if "bolsa" in g and ("con licor" in g or ("sin licor" not in g and ("6l" in g or "6 l" in g))):
            if "sin licor" in g:
                return (
                    "account_group_name = 'BOLSAS PARA GRANIZADORAS SIN LICOR'", "Bolsas 6L sin licor", []
                )
            return (
                "account_group_name = 'BOLSAS PARA GRANIZADORAS CON LICOR'", "Bolsas 6L con licor", []
            )
        if "bolsa" in g and "sin licor" in g:
            return (
                "account_group_name = 'BOLSAS PARA GRANIZADORAS SIN LICOR'", "Bolsas 6L sin licor", []
            )
        if "cremoso" in g:
            return ("account_group_name = 'CREMOSOS'", "Cremosos", [])
        if "sachet" in g:
            if "sin licor" in g:
                return (
                    "account_group_name = 'SACHETS 08 OZ' AND (UPPER(name) LIKE '%SIN LICOR%' OR UPPER(name) LIKE '%SIN LIC%')",
                    "Sachets 8 oz sin licor", [],
                )
            if "con licor" in g:
                return (
                    "account_group_name = 'SACHETS 08 OZ' AND UPPER(name) NOT LIKE '%SIN LICOR%' AND UPPER(name) NOT LIKE '%SIN LIC%'",
                    "Sachets 8 oz con licor", [],
                )
            return ("account_group_name = 'SACHETS 08 OZ'", "Sachets 8 oz (todos)", [])
        if "perla" in g:
            if "350" in g:
                return (
                    "account_group_name = 'PERLAS EXPLOSIVAS' AND name LIKE '%350%'",
                    "Perlas 350 gr", [],
                )
            if "1200" in g:
                return (
                    "account_group_name = 'PERLAS EXPLOSIVAS' AND name LIKE '%1200%'",
                    "Perlas 1200 gr", [],
                )
            if "3400" in g:
                return (
                    "account_group_name = 'PERLAS EXPLOSIVAS' AND name LIKE '%3400%'",
                    "Perlas 3400 gr", [],
                )
            return ("account_group_name = 'PERLAS EXPLOSIVAS'", "Perlas (todas)", [])
        if "gelatina" in g:
            if "330" in g:
                return ("account_group_name = 'GELATINAS' AND name LIKE '%330%'", "Gelatinas 330 gr", [])
            if "1200" in g:
                return ("account_group_name = 'GELATINAS' AND name LIKE '%1200%'", "Gelatinas 1200 gr", [])
            if "2300" in g:
                return ("account_group_name = 'GELATINAS' AND name LIKE '%2300%'", "Gelatinas 2300 gr", [])
            return ("account_group_name = 'GELATINAS'", "Gelatinas (todas)", [])
        if "sirope" in g or "sirup" in g:
            if "360" in g:
                return (
                    "account_group_name = 'SIROPES' AND name LIKE '%360%'",
                    "Siropes 360 ml", [],
                )
            if "1000" in g:
                return (
                    "account_group_name = 'SIROPES' AND name LIKE '%1000%'",
                    "Siropes 1000 ml", [],
                )
            return ("account_group_name = 'SIROPES'", "Siropes (todos)", [])
        if "sal" in g or "azucar" in g or "michelar" in g:
            if "250" in g:
                return (
                    "account_group_name = 'SALES PARA MICHELAR' AND name LIKE '%250%'",
                    "Sales/Azucares 250 gr", [],
                )
            if "500" in g:
                return (
                    "account_group_name = 'SALES PARA MICHELAR' AND name LIKE '%500%'",
                    "Sales/Azucares 500 gr", [],
                )
            return ("account_group_name = 'SALES PARA MICHELAR'", "Sales para michelar (todas)", [])
        return ("", "", [])

    where_clause, descripcion, params = match_filtro()
    if not where_clause:
        return {
            "ok": False,
            "error": (
                f"No reconozco el grupo '{grupo}'. Grupos validos: "
                "'bolsas 6L con/sin licor', 'cremosos', 'sachets con/sin licor', "
                "'perlas (350/1200/3400)gr', 'gelatinas (330/1200/2300)gr', "
                "'siropes (360/1000)ml', 'sales (250/500)gr'."
            ),
        }

    if aumento_pct is None and detal is None and mayorista is None and distribuidor is None:
        return {"ok": False, "error": "No me dijiste que precio cambiar"}

    # Buscar productos del grupo
    conn = get_conn()
    try:
        productos = conn.execute(
            f"SELECT code, name FROM siigo_products WHERE (active = 1 OR active IS NULL) AND {where_clause}",
            params,
        ).fetchall()
    finally:
        conn.close()
    if not productos:
        return {"ok": False, "error": f"No encontre productos en el grupo '{descripcion}'"}

    # Helpers
    from datetime import datetime as _dt
    IVA = 19.0

    def precio_pre_iva(p_con_iva: float) -> float:
        return round(p_con_iva / (1 + IVA / 100), 2)

    listas_a_cambiar: dict[str, float] = {}
    if aumento_pct is not None:
        # Aumento porcentual: tenemos que aplicar a cada producto su precio actual
        # No se puede mappear a un solo "nuevo_precio"; se hace dentro del loop.
        pass
    else:
        if detal is not None and detal > 0:
            listas_a_cambiar["DETAL"] = float(detal)
        if mayorista is not None and mayorista > 0:
            listas_a_cambiar["MAYORISTA"] = float(mayorista)
        if distribuidor is not None and distribuidor > 0:
            listas_a_cambiar["DISTRIBUIDOR"] = float(distribuidor)

    if aumento_pct is None and not listas_a_cambiar:
        return {"ok": False, "error": "No me dijiste que precio cambiar"}

    conn = get_conn()
    try:
        now = _dt.now().isoformat(timespec="seconds")
        afectados = 0
        listas_resumen: dict[str, int] = {}

        for p in productos:
            code = p["code"]
            if aumento_pct is not None:
                # Para cada producto, aplicar el % al precio actual de CADA lista
                for lista in ("DETAL", "MAYORISTA", "DISTRIBUIDOR"):
                    row = conn.execute(
                        "SELECT precio_con_iva FROM precios_oficiales WHERE product_code = ? AND lista = ?",
                        (code, lista),
                    ).fetchone()
                    if not row or not row["precio_con_iva"]:
                        continue
                    nuevo_con_iva = round(float(row["precio_con_iva"]) * (1 + aumento_pct / 100), 2)
                    nuevo_pre_iva = precio_pre_iva(nuevo_con_iva)
                    conn.execute(
                        """INSERT INTO precios_oficiales
                           (product_code, lista, precio_pre_iva, precio_con_iva, fuente, confirmed_by, updated_at)
                           VALUES (?, ?, ?, ?, 'manual_dueno', 'dueno', ?)
                           ON CONFLICT(product_code, lista) DO UPDATE SET
                             precio_pre_iva = excluded.precio_pre_iva,
                             precio_con_iva = excluded.precio_con_iva,
                             fuente = 'manual_dueno',
                             confirmed_by = 'dueno',
                             updated_at = excluded.updated_at""",
                        (code, lista, nuevo_pre_iva, nuevo_con_iva, now),
                    )
                    listas_resumen[lista] = listas_resumen.get(lista, 0) + 1
                afectados += 1
            else:
                # Precios absolutos
                for lista, p_con_iva in listas_a_cambiar.items():
                    p_pre_iva = precio_pre_iva(p_con_iva)
                    conn.execute(
                        """INSERT INTO precios_oficiales
                           (product_code, lista, precio_pre_iva, precio_con_iva, fuente, confirmed_by, updated_at)
                           VALUES (?, ?, ?, ?, 'manual_dueno', 'dueno', ?)
                           ON CONFLICT(product_code, lista) DO UPDATE SET
                             precio_pre_iva = excluded.precio_pre_iva,
                             precio_con_iva = excluded.precio_con_iva,
                             fuente = 'manual_dueno',
                             confirmed_by = 'dueno',
                             updated_at = excluded.updated_at""",
                        (code, lista, p_pre_iva, p_con_iva, now),
                    )
                    listas_resumen[lista] = listas_resumen.get(lista, 0) + 1
                afectados += 1

        # Audit log
        try:
            conn.execute(
                """INSERT INTO audit_log (entity_type, entity_id, action, actor, payload, created_at)
                   VALUES ('precios_oficiales', ?, ?, ?, ?, ?)""",
                (
                    descripcion[:50],
                    "cambio_grupo",
                    actor,
                    json.dumps({
                        "grupo": grupo,
                        "filtro_aplicado": descripcion,
                        "aumento_pct": aumento_pct,
                        "detal": detal, "mayorista": mayorista, "distribuidor": distribuidor,
                        "productos_afectados": afectados,
                        "listas_resumen": listas_resumen,
                    }, ensure_ascii=False),
                    now,
                ),
            )
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()

    return {
        "ok": True,
        "grupo": descripcion,
        "productos_afectados": afectados,
        "listas_cambiadas": listas_resumen,
        "detalle": (
            f"Aumento {aumento_pct:+.1f}%" if aumento_pct is not None
            else " · ".join(f"{k} ${v:,.0f}" for k, v in listas_a_cambiar.items())
        ),
    }


def cambiar_categoria_cliente(
    cliente_query: str,
    nueva_categoria: str,
    actor: str = "chat",
) -> dict:
    """Cambia la categoria de un cliente (DETAL/MAYORISTA/DISTRIBUIDOR)."""
    from skiimo.matcher import Matcher
    m = Matcher()
    hits = m.search_customer(cliente_query, limit=1)
    if not hits:
        return {"ok": False, "error": f"Cliente '{cliente_query}' no encontrado"}
    c = hits[0]
    cat = (nueva_categoria or "").upper()
    if cat not in ("DETAL", "MAYORISTA", "DISTRIBUIDOR"):
        return {"ok": False, "error": "Categoria invalida"}

    conn = get_conn()
    try:
        anterior = conn.execute(
            "SELECT categoria FROM clientes_categoria WHERE customer_id = ?",
            (c.id,),
        ).fetchone()
        ant_cat = anterior["categoria"] if anterior else "DETAL"
        from datetime import datetime as _dt
        now = _dt.now().isoformat(timespec="seconds")
        if cat == "DETAL":
            # Borrar el registro (DETAL es el default)
            conn.execute("DELETE FROM clientes_categoria WHERE customer_id = ?", (c.id,))
        else:
            conn.execute(
                """INSERT INTO clientes_categoria
                   (customer_id, categoria, fuente, notas, confirmed_by, created_at, updated_at)
                   VALUES (?, ?, 'manual_chat', ?, 'dueno', ?, ?)
                   ON CONFLICT(customer_id) DO UPDATE SET
                     categoria = excluded.categoria,
                     fuente = 'manual_chat',
                     confirmed_by = 'dueno',
                     updated_at = excluded.updated_at""",
                (c.id, cat, f"Cambio desde chat por {actor}", now, now),
            )
        conn.execute(
            """INSERT INTO audit_log (entity, entity_id, action, actor, payload, created_at)
               VALUES ('cliente', ?, 'cambio_categoria', ?, ?, ?)""",
            (c.id, actor,
             json.dumps({"anterior": ant_cat, "nueva": cat, "cliente": c.name}, ensure_ascii=False),
             now),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": True,
        "cliente": c.name,
        "nit": c.identification,
        "categoria_anterior": ant_cat,
        "categoria_nueva": cat,
    }


def configurar_pronto_pago(
    cliente_query: str,
    dias_max: int,
    descuento_pct: float,
    actor: str = "chat",
) -> dict:
    """Configura o actualiza el descuento por pronto pago de un cliente.

    Ej: 'Hugo paga en 8 dias y le doy 10% de descuento'
        -> configurar_pronto_pago(cliente_query='Hugo', dias_max=8, descuento_pct=10)

    Para QUITAR el pronto pago: pasar descuento_pct=0 o dias_max=0.
    """
    from skiimo.matcher import Matcher
    m = Matcher()
    hits = m.search_customer(cliente_query, limit=1)
    if not hits:
        return {"ok": False, "error": f"Cliente '{cliente_query}' no encontrado"}
    c = hits[0]
    try:
        dias_max = int(dias_max)
        descuento_pct = float(descuento_pct)
    except (TypeError, ValueError):
        return {"ok": False, "error": "dias_max debe ser entero y descuento_pct numero"}
    if dias_max < 0 or dias_max > 90:
        return {"ok": False, "error": "dias_max debe estar entre 0 y 90"}
    if descuento_pct < 0 or descuento_pct > 50:
        return {"ok": False, "error": "descuento_pct debe estar entre 0 y 50"}

    from datetime import datetime as _dt
    now = _dt.now().isoformat(timespec="seconds")
    quitar = (dias_max == 0 or descuento_pct == 0)
    conn = get_conn()
    try:
        anterior = conn.execute(
            "SELECT dias_max, descuento_pct FROM clientes_pronto_pago WHERE customer_id = ? AND activo = 1",
            (c.id,),
        ).fetchone()
        ant_text = (
            f"{anterior['descuento_pct']:.0f}% en {anterior['dias_max']} dias"
            if anterior else "(sin pronto pago)"
        )

        if quitar:
            # Desactivar pronto pago
            conn.execute(
                "UPDATE clientes_pronto_pago SET activo = 0 WHERE customer_id = ?",
                (c.id,),
            )
            nuevo_text = "(quitado)"
        else:
            # Upsert manual
            existing = conn.execute(
                "SELECT id FROM clientes_pronto_pago WHERE customer_id = ?",
                (c.id,),
            ).fetchone()
            notas = f"{descuento_pct:.0f}% si paga en {dias_max} dias"
            if existing:
                conn.execute(
                    """UPDATE clientes_pronto_pago
                       SET dias_max = ?, descuento_pct = ?, notas = ?, activo = 1
                       WHERE customer_id = ?""",
                    (dias_max, descuento_pct, notas, c.id),
                )
            else:
                conn.execute(
                    """INSERT INTO clientes_pronto_pago
                       (customer_id, dias_max, descuento_pct, notas, activo, created_at)
                       VALUES (?, ?, ?, ?, 1, ?)""",
                    (c.id, dias_max, descuento_pct, notas, now),
                )
            nuevo_text = f"{descuento_pct:.0f}% en {dias_max} dias"

        # Audit
        try:
            conn.execute(
                """INSERT INTO audit_log (entity_type, entity_id, action, actor, payload, created_at)
                   VALUES ('pronto_pago', ?, ?, ?, ?, ?)""",
                (c.id, "quitar" if quitar else "configurar", actor,
                 json.dumps({"anterior": ant_text, "nuevo": nuevo_text, "cliente": c.name}, ensure_ascii=False),
                 now),
            )
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()
    return {
        "ok": True,
        "cliente": c.name,
        "nit": c.identification,
        "anterior": ant_text,
        "nuevo": nuevo_text,
    }


def listar_pronto_pago(cliente_query: str | None = None) -> dict:
    """Lista los descuentos por pronto pago configurados.

    Sin argumentos: devuelve TODOS los clientes con pronto pago activo.
    Con cliente_query: el pronto pago de ese cliente (o que no tiene).

    Usar cuando preguntan: 'que clientes tienen pronto pago', 'quien tiene
    descuento por pronto pago', 'cual es el pronto pago de Hugo', 'mostrame
    los prontos pagos'.
    """
    conn = get_conn()
    try:
        # Filtrar a un cliente si lo piden
        customer_id = None
        cliente_nombre = None
        if cliente_query:
            from skiimo.matcher import Matcher
            hits = Matcher().search_customer(cliente_query, limit=1)
            if not hits:
                return {"ok": False, "error": f"Cliente '{cliente_query}' no encontrado"}
            customer_id = hits[0].id
            cliente_nombre = hits[0].name

        sql = (
            """SELECT pp.customer_id, pp.dias_max, pp.descuento_pct, pp.notas,
                      COALESCE(c.name, pp.customer_id) AS nombre
               FROM clientes_pronto_pago pp
               LEFT JOIN siigo_customers c ON c.id = pp.customer_id
               WHERE pp.activo = 1"""
        )
        params: list = []
        if customer_id:
            sql += " AND pp.customer_id = ?"
            params.append(customer_id)
        sql += " ORDER BY pp.descuento_pct DESC, nombre ASC"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    items = [
        {
            "cliente": r["nombre"],
            "descuento_pct": r["descuento_pct"],
            "dias_max": r["dias_max"],
            "texto": f"{r['descuento_pct']:.0f}% si paga en {r['dias_max']} dias",
        }
        for r in rows
    ]
    if cliente_query and not items:
        return {
            "ok": True,
            "cliente": cliente_nombre,
            "tiene_pronto_pago": False,
            "items": [],
            "total": 0,
        }
    return {"ok": True, "items": items, "total": len(items)}


def modificar_pedido_actual(
    chat_id: int,
    item_descripcion: str | None = None,
    nuevo_precio: float | None = None,
    nuevo_precio_es_con_iva: bool = True,
    nueva_cantidad: float | None = None,
) -> dict:
    """Ajusta un item del PEDIDO BORRADOR mas reciente del chat.

    Usar cuando el usuario, despues de ver un resumen de pedido, pide cambios
    sobre items de ese pedido (sin crear pedido nuevo). Ejemplos:
      - 'el chicle a 25 mil' -> nuevo_precio=25000
      - 'que sean 5 chicles' -> nueva_cantidad=5
      - 'cambia el cremoso a 32 cada uno' -> item_descripcion='cremoso', nuevo_precio=32

    Args:
      item_descripcion: nombre o parte del nombre del item a ajustar (ej 'chicle').
                        Si None, ajusta el item unico del pedido si es solo 1.
      nuevo_precio: nuevo precio. Asumido CON IVA por default (lo que paga cliente).
      nuevo_precio_es_con_iva: si False, el precio es sin IVA (precio interno).
      nueva_cantidad: nueva cantidad para ese item.

    Devuelve:
      {"ok": bool, "pedido_id": int, "cambios": [...], "error": str?}
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, payload_extraido FROM bot_pedidos "
            "WHERE telegram_chat_id = ? AND estado = 'borrador' "
            "ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"ok": False, "error": "No hay pedido en borrador para modificar"}

    payload = json.loads(row["payload_extraido"] or "{}")
    items = payload.get("items") or []
    if not items:
        return {"ok": False, "error": "El pedido no tiene items"}

    # Buscar item por descripcion
    target_idx = None
    if item_descripcion:
        q = item_descripcion.lower().strip()
        for i, it in enumerate(items):
            eleg = it.get("elegido") or {}
            raw = it.get("raw") or {}
            name = (eleg.get("name") or raw.get("descripcion") or "").lower()
            if q in name or any(t in name for t in q.split()):
                target_idx = i
                break
        if target_idx is None:
            return {"ok": False, "error": f"No encontre item que coincida con '{item_descripcion}'"}
    else:
        if len(items) == 1:
            target_idx = 0
        else:
            return {"ok": False, "error": "El pedido tiene varios items, decime cual modificar"}

    cambios = []
    item = items[target_idx]
    raw = item.get("raw") or {}

    if nuevo_precio is not None and nuevo_precio > 0:
        precio_final = float(nuevo_precio)
        if nuevo_precio_es_con_iva:
            # Convertir a sin IVA para guardar internamente
            precio_final = round(precio_final / 1.19, 2)
        raw["precio_unitario"] = precio_final
        item["raw"] = raw
        item["precio_unitario"] = precio_final
        suffix = " (con IVA)" if nuevo_precio_es_con_iva else " (sin IVA)"
        cambios.append(f"precio: ${nuevo_precio:,.0f}{suffix}")

    if nueva_cantidad is not None and nueva_cantidad > 0:
        raw["cantidad"] = float(nueva_cantidad)
        item["raw"] = raw
        item["cantidad"] = float(nueva_cantidad)
        cambios.append(f"cantidad: {nueva_cantidad:g}")

    if not cambios:
        return {"ok": False, "error": "No me dijiste que cambiar (precio, cantidad...)"}

    # Guardar payload
    from datetime import datetime as _dt2
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE bot_pedidos SET payload_extraido = ?, updated_at = ? WHERE id = ?",
            (
                json.dumps(payload, ensure_ascii=False, default=str),
                _dt2.now().isoformat(timespec="seconds"),
                row["id"],
            ),
        )
        conn.commit()
    finally:
        conn.close()
    eleg = items[target_idx].get("elegido") or {}
    return {
        "ok": True,
        "pedido_id": row["id"],
        "item": eleg.get("name") or items[target_idx].get("raw", {}).get("descripcion"),
        "cambios": cambios,
    }


def facturas_pendientes_cobro(limit: int = 10) -> dict:
    """Facturas con balance > 0 (sin cobrar)."""
    # Pre-sync: si pagaron en Siigo web hace minutos, queremos ver saldo actualizado
    _sync_invoices_recientes(dias=7)
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT i.name, i.date, i.total, i.balance, i.customer_ident, c.name as cname
               FROM siigo_invoices i LEFT JOIN siigo_customers c ON c.id = i.customer_id
               WHERE i.balance > 0
               ORDER BY i.date DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return {
        "facturas": [
            {
                "factura": r["name"],
                "fecha": r["date"],
                "total": float(r["total"]),
                "saldo": float(r["balance"]),
                "cliente_nit": r["customer_ident"],
                "cliente_nombre": r["cname"] or "(sin nombre)",
            }
            for r in rows
        ],
    }


# =============================================================================
# ASISTENCIA: programar extras + crear/editar marcajes (conversacional, admin)
# =============================================================================


def _tz_bogota():
    from skiimo.hikvision import TZ_BOGOTA
    return TZ_BOGOTA


def _fecha_relativa(s: str) -> str | None:
    """'hoy'/'mañana'/'ayer'/YYYY-MM-DD -> fecha ISO. None si no parsea."""
    from datetime import datetime as _dt, timedelta as _td
    hoy = _dt.now(_tz_bogota()).date()
    s = (s or "").strip().lower()
    if s in ("hoy", "today"):
        return hoy.isoformat()
    if s in ("mañana", "manana", "tomorrow"):
        return (hoy + _td(days=1)).isoformat()
    if s in ("ayer", "yesterday"):
        return (hoy - _td(days=1)).isoformat()
    try:
        from datetime import date as _date
        return _date.fromisoformat(s).isoformat()
    except Exception:
        return None


def programar_extras(
    fecha: str,
    hora_fin: str,
    hora_inicio: str = "17:30",
    fecha_hasta: str | None = None,
    nota: str | None = None,
) -> dict:
    """Programa una ventana de horas extra autorizadas (solo admin).

    Usar cuando el admin dice cosas como:
    - 'programa extras hoy de 6 a 8 pm'
    - 'autoriza horas extra mañana de 5:30 a 9pm'
    - 'habilita extras del 30 de mayo al 2 de junio de 6 a 8'

    fecha: dia de las extras ('hoy', 'mañana' o YYYY-MM-DD)
    hora_fin: hora limite de las extras en formato HH:MM 24h (ej. '20:00')
    hora_inicio: hora de inicio HH:MM 24h. Default '17:30'.
    fecha_hasta: si es un rango, la fecha final (YYYY-MM-DD). Opcional.
    nota: nota opcional.
    """
    import re as _re
    from datetime import datetime as _dt
    fd = _fecha_relativa(fecha)
    if not fd:
        return {"error": f"No entendí la fecha '{fecha}'. Usá 'hoy', 'mañana' o AAAA-MM-DD."}
    fh = _fecha_relativa(fecha_hasta) if fecha_hasta else fd
    if not fh:
        return {"error": f"No entendí la fecha final '{fecha_hasta}'."}
    if not (_re.match(r'^\d{1,2}:\d{2}$', hora_inicio or "") and _re.match(r'^\d{1,2}:\d{2}$', hora_fin or "")):
        return {"error": "Las horas deben ser HH:MM (ej. 18:00)."}
    # normalizar a 2 digitos
    def _nz(h):
        hh, mm = h.split(":"); return f"{int(hh):02d}:{mm}"
    hi, hfn = _nz(hora_inicio), _nz(hora_fin)
    if hfn <= hi:
        return {"error": "La hora fin debe ser mayor a la hora inicio."}
    if fh < fd:
        fd, fh = fh, fd
    now = _dt.now(_tz_bogota()).isoformat()
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO extras_autorizadas
               (fecha_desde, fecha_hasta, hora_inicio, hora_fin, nota, creado_por, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (fd, fh, hi, hfn, nota, "bot", now),
        )
        conn.commit()
        eid = cur.lastrowid
    finally:
        conn.close()
    return {
        "ok": True, "id": eid,
        "fecha_desde": fd, "fecha_hasta": fh,
        "hora_inicio": hi, "hora_fin": hfn,
        "_notificar_extras": True,  # señal para que el bot notifique a operarios
    }


def registrar_marcaje_empleado(
    empleado: str,
    fecha: str,
    hora: str,
    tipo: str,
) -> dict:
    """Crea o corrige un marcaje de un trabajador (solo admin).

    Usar cuando el admin dice:
    - 'Daniel entró hoy a las 7'
    - 'registra la salida de Mateo a las 5pm de hoy'
    - 'corrige el regreso de almuerzo de Juan Diego a la 1pm'
    - 'Manuela marcó entrada ayer a las 7:05'

    empleado: nombre (o parte) del empleado.
    fecha: 'hoy', 'mañana', 'ayer' o YYYY-MM-DD.
    hora: hora del marcaje en HH:MM 24h (ej. '07:00', '17:00', '13:00').
    tipo: uno de: entrada, salida, almuerzo_out (salida a almuerzo),
          almuerzo_in (regreso de almuerzo), extra_in (inicio extras),
          extra_out (fin extras).
    """
    import re as _re
    from datetime import datetime as _dt
    tipos_ok = ("entrada", "salida", "almuerzo_out", "almuerzo_in", "extra_in", "extra_out")
    tipo = (tipo or "").strip().lower()
    if tipo not in tipos_ok:
        return {"error": f"Tipo inválido '{tipo}'. Debe ser uno de: {', '.join(tipos_ok)}."}
    fd = _fecha_relativa(fecha)
    if not fd:
        return {"error": f"No entendí la fecha '{fecha}'."}
    if not _re.match(r'^\d{1,2}:\d{2}$', hora or ""):
        return {"error": "La hora debe ser HH:MM (ej. 07:00)."}
    hh, mm = hora.split(":")
    hora_norm = f"{int(hh):02d}:{mm}"
    conn = get_conn()
    try:
        # buscar empleado por nombre (parcial)
        q = f"%{empleado.strip()}%"
        rows = conn.execute(
            "SELECT id, nombre FROM empleados WHERE activo=1 AND nombre LIKE ? COLLATE NOCASE",
            (q,),
        ).fetchall()
        if not rows:
            return {"error": f"No encontré un empleado que coincida con '{empleado}'."}
        if len(rows) > 1:
            nombres = [r["nombre"] for r in rows[:8]]
            return {
                "necesita_aclaracion": True,
                "pregunta": f"Hay varios empleados con '{empleado}'. ¿A cuál te refieres?",
                "opciones": nombres,
            }
        emp = rows[0]
        ts = f"{fd}T{hora_norm}:00-05:00"
        now = _dt.now(_tz_bogota()).isoformat()
        # ¿ya hay un marcaje de ese tipo ese dia? -> corregir (editar) en vez de duplicar
        ya = conn.execute(
            "SELECT id FROM marcajes WHERE empleado_id=? AND fecha=? AND tipo=? LIMIT 1",
            (emp["id"], fd, tipo),
        ).fetchone()
        if ya:
            conn.execute(
                """UPDATE marcajes SET ts=?, metodo='manual', origen='corregido',
                       editado=1, editado_por='bot', editado_at=? WHERE id=?""",
                (ts, now, ya["id"]),
            )
            accion = "corregido"
        else:
            conn.execute(
                """INSERT INTO marcajes (empleado_id, ts, fecha, tipo, metodo, origen,
                                         editado, editado_por, editado_at, created_at)
                   VALUES (?,?,?,?, 'manual','manual', 1, 'bot', ?, ?)""",
                (emp["id"], ts, fd, tipo, now, now),
            )
            accion = "registrado"
        conn.commit()
    finally:
        conn.close()
    etiqueta = {
        "entrada": "entrada", "salida": "salida",
        "almuerzo_out": "salida a almuerzo", "almuerzo_in": "regreso de almuerzo",
        "extra_in": "inicio de extras", "extra_out": "fin de extras",
    }[tipo]
    return {
        "ok": True, "accion": accion, "empleado": emp["nombre"],
        "tipo": etiqueta, "fecha": fd, "hora": hora_norm,
    }


# =============================================================================
# REGISTRO DE TOOLS (signature declarations para Gemini)
# =============================================================================

# Dict que mapea nombre tool -> funcion python
TOOLS_MAP: dict[str, Any] = {
    "programar_extras": programar_extras,
    "registrar_marcaje_empleado": registrar_marcaje_empleado,
    "consultar_ventas": consultar_ventas,
    "consultar_gastos": consultar_gastos,
    "top_clientes": top_clientes,
    "top_productos": top_productos,
    "ultima_venta": ultima_venta,
    "resumen_dia": resumen_dia,
    "buscar_cliente": buscar_cliente,
    "buscar_producto": buscar_producto,
    "facturas_pendientes_cobro": facturas_pendientes_cobro,
    "facturas_proveedor_pendientes": facturas_proveedor_pendientes,
    "proveedores_a_pagar": proveedores_a_pagar,
    "vencimientos_proximos": vencimientos_proximos,
    "consultar_precio": consultar_precio,
    "cambiar_precio": cambiar_precio,
    "cambiar_categoria_cliente": cambiar_categoria_cliente,
    "cambiar_precios_grupo": cambiar_precios_grupo,
    "configurar_pronto_pago": configurar_pronto_pago,
    "listar_pronto_pago": listar_pronto_pago,
    "modificar_pedido_actual": modificar_pedido_actual,
    "analizar_pago_factura": analizar_pago_factura,
    "analizar_pago_a_proveedor": analizar_pago_a_proveedor,
    "proponer_anular_factura": proponer_anular_factura,
    "proponer_anular_ultima_factura_cliente": proponer_anular_ultima_factura_cliente,
    "agregar_usuario": agregar_usuario,
    "listar_usuarios": listar_usuarios,
    "desactivar_usuario": desactivar_usuario,
    "repetir_pedido_cliente": repetir_pedido_cliente,
    "estado_cuenta_cliente": estado_cuenta_cliente,
    "consultar_stock": consultar_stock,
}


# Declaraciones para Gemini (function declarations)
# Estos no incluyen "registrar_pedido" porque ese flujo se maneja con structured output aparte.
TOOL_DECLARATIONS: list[dict] = [
    {
        "name": "programar_extras",
        "description": (
            "Programa una ventana de horas extra autorizadas (SOLO admin). "
            "Usar cuando el admin dice: 'programa extras hoy de 6 a 8pm', "
            "'autoriza horas extra mañana de 5:30 a 9', 'habilita extras el 30 de mayo "
            "de 18:00 a 20:00'. Convertí las horas a formato 24h HH:MM. "
            "Si no dicen hora de inicio, usar 17:30."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "fecha": {"type": "string", "description": "'hoy', 'mañana' o AAAA-MM-DD"},
                "hora_fin": {"type": "string", "description": "Hora límite HH:MM 24h (ej. '20:00')"},
                "hora_inicio": {"type": "string", "description": "Hora inicio HH:MM 24h. Default '17:30'"},
                "fecha_hasta": {"type": "string", "description": "Fecha final si es un rango (AAAA-MM-DD). Opcional."},
                "nota": {"type": "string", "description": "Nota opcional"},
            },
            "required": ["fecha", "hora_fin"],
        },
    },
    {
        "name": "registrar_marcaje_empleado",
        "description": (
            "Crea o corrige un marcaje de asistencia de un trabajador (SOLO admin). "
            "Usar cuando el admin dice: 'Daniel entró hoy a las 7', "
            "'registra la salida de Mateo a las 5pm', 'corrige el regreso de almuerzo "
            "de Juan a la 1pm', 'Manuela marcó entrada ayer 7:05'. "
            "Convertí la hora a formato 24h HH:MM. Si hay varios empleados que coinciden "
            "con el nombre, la herramienta devolverá las opciones para que preguntes cuál."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "empleado": {"type": "string", "description": "Nombre o parte del nombre del empleado"},
                "fecha": {"type": "string", "description": "'hoy', 'ayer', 'mañana' o AAAA-MM-DD"},
                "hora": {"type": "string", "description": "Hora del marcaje HH:MM 24h (ej. '07:00', '17:00')"},
                "tipo": {
                    "type": "string",
                    "enum": ["entrada", "salida", "almuerzo_out", "almuerzo_in", "extra_in", "extra_out"],
                    "description": "Tipo de marcaje. almuerzo_out=salida a almuerzo, almuerzo_in=regreso, extra_in/extra_out=extras",
                },
            },
            "required": ["empleado", "fecha", "hora", "tipo"],
        },
    },
    {
        "name": "consultar_ventas",
        "description": "Consulta total y cantidad de ventas (facturas emitidas) en un periodo. Usar cuando el usuario pregunta por ventas, ingresos, facturacion.",
        "parameters": {
            "type": "object",
            "properties": {
                "periodo": {
                    "type": "string",
                    "enum": ["hoy", "ayer", "esta_semana", "este_mes", "mes_pasado", "este_anio"],
                    "description": "Periodo a consultar. Default este_mes.",
                },
                "vendedor_id": {
                    "type": "integer",
                    "description": "ID del vendedor en Siigo. Omitir para todos.",
                },
            },
        },
    },
    {
        "name": "consultar_gastos",
        "description": "Consulta total y cantidad de gastos (facturas de compra) en un periodo.",
        "parameters": {
            "type": "object",
            "properties": {
                "periodo": {
                    "type": "string",
                    "enum": ["hoy", "ayer", "esta_semana", "este_mes", "mes_pasado", "este_anio"],
                },
            },
        },
    },
    {
        "name": "top_clientes",
        "description": "Top clientes ranqueados por monto total comprado en el periodo.",
        "parameters": {
            "type": "object",
            "properties": {
                "periodo": {"type": "string", "enum": ["hoy", "ayer", "esta_semana", "este_mes", "mes_pasado", "este_anio"]},
                "limit": {"type": "integer", "description": "Cuantos clientes mostrar. Default 5."},
            },
        },
    },
    {
        "name": "top_productos",
        "description": "Top productos mas vendidos por monto total en el periodo.",
        "parameters": {
            "type": "object",
            "properties": {
                "periodo": {"type": "string", "enum": ["hoy", "ayer", "esta_semana", "este_mes", "mes_pasado", "este_anio"]},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "ultima_venta",
        "description": "Devuelve la factura de venta mas reciente. Usar cuando el usuario pregunta por ultima venta, ultima factura, ultimo pedido.",
        "parameters": {
            "type": "object",
            "properties": {
                "vendedor_id": {"type": "integer", "description": "Filtrar por vendedor."},
            },
        },
    },
    {
        "name": "resumen_dia",
        "description": "Resumen de un dia: ventas, gastos y balance. Usar cuando el usuario quiere un panorama del dia.",
        "parameters": {
            "type": "object",
            "properties": {
                "dia": {"type": "string", "description": "YYYY-MM-DD. Default hoy."},
            },
        },
    },
    {
        "name": "buscar_cliente",
        "description": "Busca clientes por nombre o NIT en el espejo local. Devuelve top-5 candidatos.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Texto a buscar (nombre, parte del nombre, o NIT)."},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "buscar_producto",
        "description": "Busca productos por nombre o codigo en el catalogo.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "facturas_pendientes_cobro",
        "description": "Lista facturas de VENTA con saldo pendiente (clientes que TE deben).",
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
        },
    },
    {
        "name": "facturas_proveedor_pendientes",
        "description": (
            "Lista facturas de COMPRA con saldo pendiente (lo que TU debes a proveedores). "
            "Usa este endpoint cuando el usuario pregunta 'que tengo por pagar', 'cuanto debo', "
            "'facturas por pagar', 'cuentas por pagar'. Incluye dias_vencido (positivo=vencida)."
        ),
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "default 20"}},
        },
    },
    {
        "name": "proveedores_a_pagar",
        "description": (
            "Resumen agrupado por proveedor: cuanto le debes a cada uno y cuantas facturas. "
            "Usa este endpoint cuando el usuario pregunta 'a quien le debo', 'top proveedores', "
            "'quienes son mis principales acreedores'."
        ),
        "parameters": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "default 10"}},
        },
    },
    {
        "name": "vencimientos_proximos",
        "description": (
            "Facturas de compra con vencimiento proximo o ya vencidas. "
            "Usa este endpoint cuando el usuario pregunta 'que vence esta semana', "
            "'que esta vencido', 'que tengo que pagar hoy', 'urgencias de pago'."
        ),
        "parameters": {
            "type": "object",
            "properties": {"dias": {"type": "integer", "description": "Horizonte hacia adelante en dias. Default 7."}},
        },
    },
    {
        "name": "consultar_precio",
        "description": (
            "Consulta el precio oficial vigente de un producto en sus 3 listas "
            "(DETAL, MAYORISTA, DISTRIBUIDOR). Usar cuando el usuario pregunta "
            "'cuanto cuesta X', 'cual es el precio de Y', 'a cuanto vendo Z'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "product_query": {"type": "string", "description": "Nombre o codigo del producto"},
                "categoria": {"type": "string", "description": "Categoria opcional (DETAL/MAYORISTA/DISTRIBUIDOR)"},
            },
            "required": ["product_query"],
        },
    },
    {
        "name": "cambiar_precio",
        "description": (
            "Cambia el precio oficial de UN producto especifico en una categoria. "
            "Usar cuando el usuario dice 'subir/bajar/cambiar precio detal de X a $Y', "
            "'la bolsa chicle mayorista ahora cuesta 24000'. "
            "El precio se interpreta SIEMPRE CON IVA. Solo admin puede usar esto."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "product_query": {"type": "string", "description": "Producto a cambiar"},
                "categoria": {"type": "string", "description": "DETAL, MAYORISTA o DISTRIBUIDOR"},
                "nuevo_precio_con_iva": {"type": "number", "description": "Nuevo precio CON IVA"},
            },
            "required": ["product_query", "categoria", "nuevo_precio_con_iva"],
        },
    },
    {
        "name": "cambiar_precios_grupo",
        "description": (
            "Cambia precios de TODO un grupo de productos a la vez (cuando el usuario "
            "habla de grupos, no de productos individuales). Ejemplos: "
            "'todos los sachets con licor mayorista a 2000', "
            "'todas las bolsas 6L con licor: detal 26 mayor 24 distrib 20', "
            "'sube 5% mayorista de todos los sachets'. "
            "Para grupos: bolsas 6L con/sin licor, cremosos, sachets con/sin licor, "
            "perlas (350/1200/3400)gr, gelatinas (330/1200/2300)gr, "
            "siropes (360/1000)ml, sales (250/500)gr. "
            "Precios SIEMPRE CON IVA. Si dice 'a 3 mil' es 3000. Solo admin."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "grupo": {
                    "type": "string",
                    "description": (
                        "Filtro del grupo. Ejemplos: 'bolsas 6L con licor', "
                        "'sachets con licor', 'perlas 1200gr', 'gelatinas', 'siropes 1000ml'."
                    ),
                },
                "detal": {"type": "number", "description": "Precio DETAL con IVA (opcional)"},
                "mayorista": {"type": "number", "description": "Precio MAYORISTA con IVA (opcional)"},
                "distribuidor": {"type": "number", "description": "Precio DISTRIBUIDOR con IVA (opcional)"},
                "aumento_pct": {
                    "type": "number",
                    "description": (
                        "Alternativa: % a aumentar (positivo) o bajar (negativo) sobre el precio actual. "
                        "Si se usa, no pasar precios absolutos."
                    ),
                },
            },
            "required": ["grupo"],
        },
    },
    {
        "name": "analizar_pago_factura",
        "description": (
            "Analiza un pago que el cliente realizo sobre una factura. "
            "Usar cuando el usuario dice 'Hugo me pago X', 'recibi tanto de la factura Y', "
            "'registrar pago de Z', 'cobro de X', 'me transfirieron'. "
            "DEVUELVE opciones (pago completo / pronto pago / abono) que el sistema "
            "presenta como botones. NO registra el pago todavia."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "factura": {"type": "string", "description": "Nombre de factura (FV-1-5192) o consecutivo"},
                "monto": {"type": "number", "description": "Monto pagado por el cliente (con IVA)"},
                "metodo_pago": {
                    "type": "string",
                    "description": "Metodo: efectivo, nequi, daviplata, banco_ahorros, tarjeta_debito, tarjeta_credito",
                },
            },
            "required": ["factura", "monto"],
        },
    },
    {
        "name": "agregar_usuario",
        "description": (
            "Registra a un usuario para que pueda usar el bot. "
            "Usar cuando el admin dice: 'agrega como admin al chat 123 llamado Maria', "
            "'da de alta a Frank con chat 999', 'registra al chat 555 como vendedor'. "
            "Si no especifica rol, asumir 'vendedor'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "integer", "description": "chat_id de Telegram del usuario nuevo"},
                "nombre": {"type": "string", "description": "Como llamarlo (ej: 'Maria', 'Frank Tabares')"},
                "rol": {"type": "string", "description": "'admin' o 'vendedor'. Default vendedor"},
                "siigo_seller_id": {"type": "integer", "description": "ID vendedor en Siigo. Opcional."},
            },
            "required": ["chat_id", "nombre"],
        },
    },
    {
        "name": "listar_usuarios",
        "description": (
            "Lista los usuarios registrados en el bot (vendedores y admins). "
            "Usar cuando el admin dice: 'quien tiene acceso', 'lista de usuarios', "
            "'que vendedores tengo registrados'."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "desactivar_usuario",
        "description": (
            "Desactiva un usuario para que no pueda usar el bot. No lo borra. "
            "Usar cuando el admin dice: 'sacale acceso al chat X', 'desactiva a Y'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "integer", "description": "chat_id del usuario a desactivar"},
            },
            "required": ["chat_id"],
        },
    },
    {
        "name": "proponer_anular_factura",
        "description": (
            "Propone anular una factura de venta CUANDO YA TENES EL NUMERO/NOMBRE EXACTO. "
            "Usar cuando el admin menciona la factura por su identificador: "
            "'anula la factura FV-1-5192', 'cancela la 5192', 'tira la FV-2-680'. "
            "Si el admin dice 'la ultima de Hugo' o 'la penultima de Diego', usar "
            "proponer_anular_ultima_factura_cliente en su lugar. "
            "NO ejecuta nada — devuelve botones de confirmacion."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "factura": {"type": "string", "description": "Nombre de factura (FV-1-5192) o consecutivo"},
                "motivo": {"type": "string", "description": "Motivo de la anulacion. Opcional."},
            },
            "required": ["factura"],
        },
    },
    {
        "name": "proponer_anular_ultima_factura_cliente",
        "description": (
            "Propone anular la N-esima factura mas reciente de un cliente. "
            "n=1 = ultima, n=2 = penultima, n=3 = antepenultima. "
            "Usar cuando el admin dice: 'anula la ultima factura de Hugo', "
            "'cancela el ultimo pedido de Maria', 'borra la penultima de Diego', "
            "'tira la antepenultima factura de X'. "
            "NO ejecuta — devuelve botones de confirmacion."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cliente_query": {"type": "string", "description": "Nombre o NIT del cliente"},
                "n": {"type": "integer", "description": "1=ultima, 2=penultima, 3=antepenultima. Default 1."},
                "motivo": {"type": "string", "description": "Motivo de la anulacion. Opcional."},
            },
            "required": ["cliente_query"],
        },
    },
    {
        "name": "analizar_pago_a_proveedor",
        "description": (
            "Analiza un pago SALIENTE que NOSOTROS hicimos a un proveedor sobre una factura "
            "de compra. Usar cuando el usuario dice 'pague a Arqui', 'le transferi 5M a X', "
            "'salio pago para Y', 'cubri la factura EI-23573', 'le pague al proveedor'. "
            "DEVUELVE opciones (pago completo / abono) para presentar como botones."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "compra": {"type": "string", "description": "Nombre factura de compra (FC-1-417) o numero del proveedor (EI-23573)"},
                "monto": {"type": "number", "description": "Monto que pagamos al proveedor (con IVA)"},
                "metodo_pago": {
                    "type": "string",
                    "description": "Metodo: efectivo, nequi, daviplata, banco_ahorros, tarjeta_debito, tarjeta_credito",
                },
            },
            "required": ["compra", "monto"],
        },
    },
    {
        "name": "repetir_pedido_cliente",
        "description": (
            "Devuelve el ultimo pedido (o N-esimo mas reciente) de un cliente con sus items, "
            "cantidades y precios anteriores. Usar cuando el usuario dice 'repetir pedido de X', "
            "'mandale lo de siempre a X', 'lo mismo que la vez pasada para X', 'el ultimo pedido de Y'. "
            "Despues mostrarle al usuario el detalle y, si confirma, llamar a registrar_pedido."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cliente_query": {"type": "string", "description": "Nombre o NIT del cliente"},
                "n": {"type": "integer", "description": "1=ultimo, 2=penultimo, etc. Default 1"},
            },
            "required": ["cliente_query"],
        },
    },
    {
        "name": "estado_cuenta_cliente",
        "description": (
            "Estado de cuenta completo de un cliente: deuda total, facturas pendientes, "
            "facturas vencidas, compras del ultimo ano, categoria, pronto pago configurado. "
            "Usar cuando el usuario dice 'estado de cuenta de X', 'cuanto me debe X', "
            "'cartera de Y', 'como va el cliente Z', 'facturas pendientes de X'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cliente_query": {"type": "string", "description": "Nombre o NIT del cliente"},
            },
            "required": ["cliente_query"],
        },
    },
    {
        "name": "consultar_stock",
        "description": (
            "Stock actual de un producto + estadisticas de rotacion (ventas ultimos 30 dias, "
            "promedio diario, dias de cobertura, alertas de stock bajo). "
            "Usar cuando el usuario dice 'cuantas bolsas chicle tengo', 'stock de X', "
            "'tengo inventario de Y', 'cuanto me queda de Z', 'rotacion de X'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "producto_query": {"type": "string", "description": "Nombre o codigo del producto"},
            },
            "required": ["producto_query"],
        },
    },
    {
        "name": "cambiar_categoria_cliente",
        "description": (
            "Cambia la categoria de un cliente (DETAL/MAYORISTA/DISTRIBUIDOR). "
            "Usar cuando el usuario dice 'Hugo ahora es distribuidor', "
            "'Pedro pasa a mayorista', 'sacar a Maria de mayorista'. "
            "Solo admin puede usar esto."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cliente_query": {"type": "string", "description": "Nombre o NIT del cliente"},
                "nueva_categoria": {"type": "string", "description": "DETAL, MAYORISTA o DISTRIBUIDOR"},
            },
            "required": ["cliente_query", "nueva_categoria"],
        },
    },
    {
        "name": "modificar_pedido_actual",
        "description": (
            "Ajusta un item del PEDIDO BORRADOR mas reciente del chat. Usar cuando el usuario, "
            "DESPUES de ver el resumen de un pedido, pide cambios sobre items SIN crear pedido nuevo. "
            "Ejemplos: 'el chicle a 25 mil', 'que sean 5 chicles', 'cambia el cremoso a 32'. "
            "NO uses esta tool si el usuario describe un pedido nuevo desde cero."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "integer", "description": "ID del chat actual (te lo pasa el sistema)"},
                "item_descripcion": {
                    "type": "string",
                    "description": "Nombre o parte del nombre del item a modificar (ej 'chicle')",
                },
                "nuevo_precio": {
                    "type": "number",
                    "description": "Nuevo precio. Si el usuario dice '25 mil' es 25000. Asume CON IVA por default.",
                },
                "nuevo_precio_es_con_iva": {
                    "type": "boolean",
                    "description": "True si el precio incluye IVA (default). False si es sin IVA explicito.",
                },
                "nueva_cantidad": {
                    "type": "number",
                    "description": "Si el usuario quiere cambiar la cantidad, no el precio.",
                },
            },
            "required": ["chat_id"],
        },
    },
    {
        "name": "configurar_pronto_pago",
        "description": (
            "Configura o actualiza el descuento por pronto pago de un cliente. "
            "Usar cuando el usuario dice 'Hugo paga en 8 dias y le doy 10%', "
            "'a Zuniga descuento 10% si paga antes de 8 dias', "
            "'cambia el pronto pago de Pedro a 5% en 5 dias'. "
            "Para QUITAR el pronto pago: pasar dias_max=0 o descuento_pct=0. "
            "Solo admin puede usar esto."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cliente_query": {"type": "string", "description": "Nombre o NIT del cliente"},
                "dias_max": {"type": "integer", "description": "Dias maximo para que aplique el descuento (0 para quitar). Ej: 8"},
                "descuento_pct": {"type": "number", "description": "Porcentaje de descuento (0 para quitar). Ej: 10 para 10%"},
            },
            "required": ["cliente_query", "dias_max", "descuento_pct"],
        },
    },
    {
        "name": "listar_pronto_pago",
        "description": (
            "Lista los descuentos por pronto pago configurados. Sin argumentos "
            "devuelve TODOS los clientes que tienen pronto pago activo. Con "
            "cliente_query devuelve el de ese cliente. Usar cuando preguntan "
            "'que clientes tienen pronto pago', 'quien tiene descuento por pronto "
            "pago', 'cual es el pronto pago de Hugo', 'mostrame los prontos pagos'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "cliente_query": {
                    "type": "string",
                    "description": "Opcional. Nombre o NIT de un cliente para ver solo su pronto pago. Omitir para listar todos.",
                },
            },
        },
    },
]
