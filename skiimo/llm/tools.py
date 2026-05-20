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
    doc_id = int(inv.get("document_id") or 0)
    es_electronica = doc_id == 27703  # FV-2 con DIAN
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


def facturas_pendientes_cobro(limit: int = 10) -> dict:
    """Facturas con balance > 0 (sin cobrar)."""
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
# REGISTRO DE TOOLS (signature declarations para Gemini)
# =============================================================================

# Dict que mapea nombre tool -> funcion python
TOOLS_MAP: dict[str, Any] = {
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
    "analizar_pago_factura": analizar_pago_factura,
    "analizar_pago_a_proveedor": analizar_pago_a_proveedor,
    "proponer_anular_factura": proponer_anular_factura,
    "proponer_anular_ultima_factura_cliente": proponer_anular_ultima_factura_cliente,
    "repetir_pedido_cliente": repetir_pedido_cliente,
    "estado_cuenta_cliente": estado_cuenta_cliente,
    "consultar_stock": consultar_stock,
}


# Declaraciones para Gemini (function declarations)
# Estos no incluyen "registrar_pedido" porque ese flujo se maneja con structured output aparte.
TOOL_DECLARATIONS: list[dict] = [
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
            "Cambia el precio oficial de un producto en una categoria especifica. "
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
]
