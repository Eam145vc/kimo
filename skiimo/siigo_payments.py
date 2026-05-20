"""Registro de pagos de clientes en Siigo.

Funciones:
  - analizar_pago(factura_id_or_name, monto_pagado, fecha_pago=hoy) -> propuesta
  - registrar_pago_completo(factura, monto, metodo)                 -> RC
  - registrar_pago_con_pp(factura, monto, metodo, descuento_pct)    -> RC + NC
  - registrar_abono(factura, monto, metodo)                         -> RC parcial
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import httpx

from siigo_client import SiigoClient
from skiimo.db.schema import get_conn
from skiimo.pricing.engine import obtener_pronto_pago


# IDs descubiertos en exploracion
RC_DOC_ID = 13213  # Recibo de Caja (cobros de clientes)
RP_DOC_ID = 13218  # Recibo de Pago / Egreso (pagos a proveedores)
NC_DOC_ID_TRADICIONAL = 13221  # NC tradicional (asociada a FV tradicional 13214)
NC_DOC_ID_ELECTRONICA = 27704  # NC electronica (asociada a FV electronica 27703)

# Razon NC: 1=Devolucion 2=Anulacion 3=Rebaja 4=Descuento 7=Otros
NC_REASON_DESCUENTO = 4

PAYMENT_METHODS_CONTADO = {
    "efectivo": 3043,
    "nequi": 8102,
    "daviplata": 8103,
    "banco_ahorros": 8104,
    "tarjeta_debito": 3045,
    "tarjeta_credito": 3046,
}


@dataclass(slots=True)
class PagoResult:
    ok: bool
    rc_id: str | None = None
    rc_name: str | None = None
    nc_id: str | None = None
    nc_name: str | None = None
    error: str | None = None


@dataclass(slots=True)
class AnalisisPago:
    """Resultado del analisis de un pago entrante."""
    factura_id: str
    factura_name: str
    factura_total: float
    factura_balance: float  # saldo pendiente actual
    cliente_id: str
    cliente_nombre: str
    monto_pagado: float
    fecha_factura: date
    fecha_pago: date
    dias_transcurridos: int
    diferencia: float                # balance - monto_pagado
    diferencia_pct: float            # diferencia / balance * 100
    opciones: list[dict] = field(default_factory=list)
    # opciones es lista de dicts: {tipo, label, descuento_pct (si aplica), ...}


def _audit(action: str, payload: dict, actor: str = "bot") -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO audit_log (entity, entity_id, action, actor, payload, created_at) "
            "VALUES ('pago', ?, ?, ?, ?, ?)",
            (payload.get("factura_id"), action, actor,
             json.dumps(payload, ensure_ascii=False, default=str),
             datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()


def _buscar_factura_por_nombre(name: str) -> dict | None:
    """Busca factura por su 'name' (ej: FV-1-5192) en el espejo local."""
    conn = get_conn()
    try:
        # Reemplazo flexible: aceptar variantes (FV-1-5192, fv1-5192, 5192)
        norm = name.strip().upper().replace(" ", "")
        row = conn.execute(
            "SELECT * FROM siigo_invoices WHERE UPPER(name) = ? OR UPPER(REPLACE(name, '-', '')) = ?",
            (norm, norm.replace("-", "")),
        ).fetchone()
        if row:
            return dict(row)
        # Buscar por consecutivo numerico
        if norm.isdigit():
            row = conn.execute(
                "SELECT * FROM siigo_invoices WHERE number = ? ORDER BY date DESC LIMIT 1",
                (int(norm),),
            ).fetchone()
            if row:
                return dict(row)
    finally:
        conn.close()
    return None


def _refresh_balance_from_siigo(factura_id: str) -> float | None:
    """Trae el balance actualizado de Siigo (por si hubo abonos previos)."""
    try:
        with SiigoClient() as s:
            inv = s.get(f"/v1/invoices/{factura_id}")
            return float(inv.get("balance") or 0)
    except Exception:
        return None


def analizar_pago(
    factura_query: str,
    monto_pagado: float,
    fecha_pago: str | None = None,
) -> AnalisisPago | None:
    """Analiza un pago entrante y devuelve opciones para que el usuario elija."""
    inv = _buscar_factura_por_nombre(factura_query)
    if not inv:
        return None

    # Balance actualizado desde Siigo
    saldo_actual = _refresh_balance_from_siigo(inv["id"])
    if saldo_actual is None:
        saldo_actual = float(inv.get("balance") or inv.get("total") or 0)

    fecha_factura = date.fromisoformat(inv["date"])
    fecha_pago_d = date.fromisoformat(fecha_pago) if fecha_pago else date.today()
    dias = (fecha_pago_d - fecha_factura).days

    cliente_id = inv["customer_id"]
    conn = get_conn()
    try:
        c = conn.execute("SELECT name FROM siigo_customers WHERE id = ?", (cliente_id,)).fetchone()
        cli_nombre = c["name"] if c else "?"
    finally:
        conn.close()

    diff = saldo_actual - monto_pagado
    diff_pct = (diff / saldo_actual * 100) if saldo_actual > 0 else 0

    a = AnalisisPago(
        factura_id=inv["id"],
        factura_name=inv["name"],
        factura_total=float(inv["total"]),
        factura_balance=saldo_actual,
        cliente_id=cliente_id,
        cliente_nombre=cli_nombre,
        monto_pagado=monto_pagado,
        fecha_factura=fecha_factura,
        fecha_pago=fecha_pago_d,
        dias_transcurridos=dias,
        diferencia=diff,
        diferencia_pct=diff_pct,
    )

    # ===== Generar opciones =====
    # Opcion: pago exacto (saldar)
    if abs(diff) < 0.5:  # tolerancia de 50 centavos
        a.opciones.append({
            "tipo": "completo",
            "label": "Pago completo (saldar factura)",
            "monto_recibo": monto_pagado,
        })
        return a

    # Si pago MAS que el saldo: no permitir
    if monto_pagado > saldo_actual + 0.5:
        a.opciones.append({
            "tipo": "error",
            "label": f"Pagó MÁS que el saldo ({diff:.0f} de más). Revisar.",
        })
        return a

    # Pago menor que el saldo: 2 caminos posibles
    # Camino A: Pronto pago (si el cliente lo tiene y el monto coincide con algun escalon)
    escalas = obtener_pronto_pago(cliente_id)
    for e in escalas:
        dto_pct = float(e["descuento_pct"])
        dias_max = int(e["dias_max"])
        # Monto esperado si aplica este escalon
        esperado = saldo_actual * (1 - dto_pct / 100)
        if abs(esperado - monto_pagado) < 1.0:  # tolerancia $1
            dentro_plazo = dias <= dias_max
            a.opciones.append({
                "tipo": "pp" if dentro_plazo else "pp_forzar",
                "label": f"Pronto pago {dto_pct:.0f}% ({'dentro de' if dentro_plazo else 'FUERA de'} plazo {dias_max}d)",
                "monto_recibo": monto_pagado,
                "monto_nc": diff,
                "descuento_pct": dto_pct,
                "dias_max": dias_max,
                "dentro_plazo": dentro_plazo,
            })

    # Camino B: Abono (siempre disponible)
    a.opciones.append({
        "tipo": "abono",
        "label": f"Abono parcial (queda saldo de ${diff:,.0f})",
        "monto_recibo": monto_pagado,
        "saldo_restante": diff,
    })

    return a


def registrar_pago_completo(
    factura_id: str,
    factura_name: str,
    cliente_ident: str,
    monto: float,
    metodo: str,
    fecha_pago: str | None = None,
    actor: str = "bot",
) -> PagoResult:
    """Crea RC en Siigo que salda completamente la factura."""
    return _crear_recibo_caja(factura_id, factura_name, cliente_ident, monto, metodo, fecha_pago, actor)


def registrar_pago_con_pp(
    factura_id: str,
    factura_name: str,
    cliente_ident: str,
    monto_recibo: float,
    monto_nc: float,
    metodo: str,
    descuento_pct: float,
    fecha_pago: str | None = None,
    actor: str = "bot",
) -> PagoResult:
    """Crea RC por monto_recibo + NC por monto_nc (descuento PP)."""
    # 1) Recibo de caja
    rc = _crear_recibo_caja(factura_id, factura_name, cliente_ident, monto_recibo, metodo, fecha_pago, actor)
    if not rc.ok:
        return rc

    # 2) Nota credito por descuento pronto pago
    nc = _crear_nota_credito_pp(factura_id, factura_name, cliente_ident, monto_nc, descuento_pct, actor)
    if not nc.ok:
        # RC se creó pero NC falló: devolver error pero mantener RC
        return PagoResult(
            ok=False,
            rc_id=rc.rc_id, rc_name=rc.rc_name,
            error=f"Recibo creado pero NC falló: {nc.error}",
        )
    return PagoResult(
        ok=True,
        rc_id=rc.rc_id, rc_name=rc.rc_name,
        nc_id=nc.nc_id, nc_name=nc.nc_name,
    )


def registrar_abono(
    factura_id: str,
    factura_name: str,
    cliente_ident: str,
    monto: float,
    metodo: str,
    fecha_pago: str | None = None,
    actor: str = "bot",
) -> PagoResult:
    """Igual que pago_completo pero conceptualmente es abono (la factura queda con saldo)."""
    return _crear_recibo_caja(factura_id, factura_name, cliente_ident, monto, metodo, fecha_pago, actor)


# =============================================================================
# PAGOS A PROVEEDOR (compras)
# =============================================================================

@dataclass(slots=True)
class AnalisisPagoProveedor:
    """Resultado del analisis de un pago saliente (a proveedor)."""
    compra_id: str
    compra_name: str
    compra_total: float
    compra_balance: float
    proveedor_id: str
    proveedor_nombre: str
    monto_pagado: float
    fecha_compra: date
    fecha_pago: date
    dias_transcurridos: int
    diferencia: float
    diferencia_pct: float
    opciones: list[dict] = field(default_factory=list)


def _buscar_compra_por_nombre(name: str) -> dict | None:
    """Busca compra por su 'name' (ej: FC-1-417) o por numero de factura del proveedor."""
    conn = get_conn()
    try:
        norm = name.strip().upper().replace(" ", "")
        row = conn.execute(
            "SELECT * FROM siigo_purchases WHERE UPPER(name) = ? OR UPPER(REPLACE(name, '-', '')) = ?",
            (norm, norm.replace("-", "")),
        ).fetchone()
        if row:
            return dict(row)
        # Buscar por provider_inv_number (numero que viene en la factura del proveedor)
        row = conn.execute(
            """SELECT * FROM siigo_purchases
               WHERE UPPER(provider_inv_number) = ? OR provider_inv_number = ?
               ORDER BY date DESC LIMIT 1""",
            (norm, name.strip()),
        ).fetchone()
        if row:
            return dict(row)
        # Por numero solo
        if norm.isdigit():
            row = conn.execute(
                "SELECT * FROM siigo_purchases WHERE number = ? ORDER BY date DESC LIMIT 1",
                (int(norm),),
            ).fetchone()
            if row:
                return dict(row)
    finally:
        conn.close()
    return None


def _refresh_purchase_balance(compra_id: str) -> float | None:
    try:
        with SiigoClient() as s:
            inv = s.get(f"/v1/purchases/{compra_id}")
            return float(inv.get("balance") or 0)
    except Exception:
        return None


def analizar_pago_proveedor(
    compra_query: str,
    monto_pagado: float,
    fecha_pago: str | None = None,
) -> AnalisisPagoProveedor | None:
    """Analiza un pago saliente sobre una factura de compra y devuelve opciones."""
    inv = _buscar_compra_por_nombre(compra_query)
    if not inv:
        return None

    saldo_actual = _refresh_purchase_balance(inv["id"])
    if saldo_actual is None:
        saldo_actual = float(inv.get("balance") or inv.get("total") or 0)

    fecha_compra = date.fromisoformat(inv["date"])
    fecha_pago_d = date.fromisoformat(fecha_pago) if fecha_pago else date.today()
    dias = (fecha_pago_d - fecha_compra).days

    proveedor_id = inv["supplier_id"]
    conn = get_conn()
    try:
        c = conn.execute("SELECT name FROM siigo_customers WHERE id = ?", (proveedor_id,)).fetchone()
        prov_nombre = c["name"] if c else "?"
    finally:
        conn.close()

    diff = saldo_actual - monto_pagado
    diff_pct = (diff / saldo_actual * 100) if saldo_actual > 0 else 0

    a = AnalisisPagoProveedor(
        compra_id=inv["id"],
        compra_name=inv["name"],
        compra_total=float(inv["total"]),
        compra_balance=saldo_actual,
        proveedor_id=proveedor_id,
        proveedor_nombre=prov_nombre,
        monto_pagado=monto_pagado,
        fecha_compra=fecha_compra,
        fecha_pago=fecha_pago_d,
        dias_transcurridos=dias,
        diferencia=diff,
        diferencia_pct=diff_pct,
    )

    if saldo_actual <= 0:
        a.opciones.append({
            "tipo": "error",
            "label": f"Factura ya saldada (balance $0). No hay nada que pagar.",
        })
        return a

    if abs(diff) < 0.5:
        a.opciones.append({
            "tipo": "completo",
            "label": "Pago completo (saldar factura)",
            "monto_recibo": monto_pagado,
        })
        return a

    if monto_pagado > saldo_actual + 0.5:
        a.opciones.append({
            "tipo": "error",
            "label": f"Pagaste MAS que el saldo (${abs(diff):,.0f} de mas). Revisar.",
        })
        return a

    # Pago menor que saldo -> abono
    a.opciones.append({
        "tipo": "abono",
        "label": f"Abono parcial (queda saldo de ${diff:,.0f})",
        "monto_recibo": monto_pagado,
        "saldo_restante": diff,
    })
    return a


def registrar_pago_proveedor(
    compra_id: str,
    compra_name: str,
    proveedor_ident: str,
    monto: float,
    metodo: str,
    fecha_pago: str | None = None,
    actor: str = "bot",
) -> PagoResult:
    """Crea Recibo de Pago / Egreso (RP) que paga total o parcialmente una factura de compra."""
    # Parsear nombre compra: FC-1-417 -> prefix=FC-1, consecutive=417
    parts = compra_name.split("-")
    if len(parts) >= 3:
        prefix = "-".join(parts[:-1])  # FC-1
        try:
            consecutive = int(parts[-1])
        except ValueError:
            return PagoResult(ok=False, error=f"No pude parsear consecutivo de {compra_name}")
    else:
        return PagoResult(ok=False, error=f"Nombre de compra raro: {compra_name}")

    # Fecha compra desde DB
    conn = get_conn()
    try:
        row = conn.execute("SELECT date FROM siigo_purchases WHERE id = ?", (compra_id,)).fetchone()
        fecha_compra = row["date"] if row else date.today().isoformat()
    finally:
        conn.close()

    payment_id = PAYMENT_METHODS_CONTADO.get(metodo, 8104)  # default BANCO AHORROS
    fecha_recibo = fecha_pago or date.today().isoformat()
    monto_2 = round(float(monto), 2)

    payload = {
        "document": {"id": RP_DOC_ID},
        "date": fecha_recibo,
        "type": "DebtPayment",
        "supplier": {"identification": proveedor_ident, "branch_office": 0},
        "items": [{
            "due": {
                "prefix": prefix,
                "consecutive": consecutive,
                "quote": 1,
                "date": fecha_compra,
            },
            "value": monto_2,
        }],
        "payment": {"id": payment_id, "value": monto_2},
        "observations": f"Pago factura {compra_name}",
    }
    _audit("rp_request", {"compra_id": compra_id, "payload": payload}, actor)
    try:
        with SiigoClient() as s:
            r = s.post("/v1/payment-receipts", payload)
    except httpx.HTTPStatusError as e:
        body = e.response.text[:400]
        _audit("rp_error", {"compra_id": compra_id, "status": e.response.status_code, "body": body}, actor)
        return PagoResult(ok=False, error=f"HTTP {e.response.status_code}: {body}")
    except Exception as e:
        _audit("rp_exception", {"compra_id": compra_id, "error": str(e)}, actor)
        return PagoResult(ok=False, error=str(e))

    _audit("rp_ok", {"compra_id": compra_id, "rp_id": r.get("id"), "rp_name": r.get("name")}, actor)
    return PagoResult(ok=True, rc_id=r.get("id"), rc_name=r.get("name"))


def _crear_recibo_caja(
    factura_id: str,
    factura_name: str,
    cliente_ident: str,
    monto: float,
    metodo: str,
    fecha_pago: str | None,
    actor: str,
) -> PagoResult:
    """POST /v1/vouchers (recibo de caja)."""
    # Parsear nombre factura: FV-1-5192 -> prefix=FV-1, consecutive=5192
    parts = factura_name.split("-")
    if len(parts) >= 3:
        prefix = "-".join(parts[:-1])  # FV-1
        try:
            consecutive = int(parts[-1])
        except ValueError:
            return PagoResult(ok=False, error=f"No pude parsear consecutivo de {factura_name}")
    else:
        return PagoResult(ok=False, error=f"Nombre de factura raro: {factura_name}")

    # Fecha factura desde DB
    conn = get_conn()
    try:
        row = conn.execute("SELECT date FROM siigo_invoices WHERE id = ?", (factura_id,)).fetchone()
        fecha_factura = row["date"] if row else date.today().isoformat()
    finally:
        conn.close()

    payment_id = PAYMENT_METHODS_CONTADO.get(metodo, 3043)
    fecha_recibo = fecha_pago or date.today().isoformat()
    monto_2 = round(float(monto), 2)

    payload = {
        "document": {"id": RC_DOC_ID},
        "date": fecha_recibo,
        "type": "DebtPayment",
        "customer": {"identification": cliente_ident, "branch_office": 0},
        "items": [{
            "due": {
                "prefix": prefix,
                "consecutive": consecutive,
                "quote": 1,
                "date": fecha_factura,
            },
            "value": monto_2,
        }],
        "payment": {"id": payment_id, "value": monto_2},
        "observations": f"Cobro factura {factura_name}",
    }
    _audit("rc_request", {"factura_id": factura_id, "payload": payload}, actor)
    try:
        with SiigoClient() as s:
            r = s.post("/v1/vouchers", payload)
    except httpx.HTTPStatusError as e:
        body = e.response.text[:400]
        _audit("rc_error", {"factura_id": factura_id, "status": e.response.status_code, "body": body}, actor)
        return PagoResult(ok=False, error=f"HTTP {e.response.status_code}: {body}")
    except Exception as e:
        _audit("rc_exception", {"factura_id": factura_id, "error": str(e)}, actor)
        return PagoResult(ok=False, error=str(e))

    _audit("rc_ok", {"factura_id": factura_id, "rc_id": r.get("id"), "rc_name": r.get("name")}, actor)
    return PagoResult(ok=True, rc_id=r.get("id"), rc_name=r.get("name"))


def _crear_nota_credito_pp(
    factura_id: str,
    factura_name: str,
    cliente_ident: str,
    monto_nc: float,
    descuento_pct: float,
    actor: str,
) -> PagoResult:
    """POST /v1/credit-notes para registrar descuento por pronto pago.

    Estrategia: crear NC sobre la factura original con motivo Descuento (reason 4).
    Necesitamos llevar al menos 1 item con descripcion 'Descuento pronto pago'.
    Sin embargo, Siigo NC requiere usar codes de productos del catalogo; lo que hacemos:
    referenciamos el primer item de la factura con la porcion proporcional.
    """
    # Determinar doc-type de NC segun la factura
    conn = get_conn()
    try:
        row = conn.execute("SELECT document_id, items_json FROM siigo_invoices WHERE id = ?", (factura_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return PagoResult(ok=False, error="Factura no encontrada para NC")
    fv_doc = int(row["document_id"])
    nc_doc_id = NC_DOC_ID_ELECTRONICA if fv_doc == 27703 else NC_DOC_ID_TRADICIONAL

    # Items: reducir proporcionalmente el primer item para construir NC del monto requerido
    try:
        items_factura = json.loads(row["items_json"])
    except Exception:
        return PagoResult(ok=False, error="No pude leer items de la factura")
    if not items_factura:
        return PagoResult(ok=False, error="Factura sin items")

    primer = items_factura[0]
    iva_pct = 19.0
    for t in primer.get("taxes", []):
        if t.get("type") == "IVA":
            iva_pct = float(t.get("percentage") or 19.0)
            break
    # monto_nc es CON IVA. Sacar pre-IVA.
    pre_iva = round(monto_nc / (1 + iva_pct / 100), 2)

    payload: dict[str, Any] = {
        "document": {"id": nc_doc_id},
        "date": date.today().isoformat(),
        "invoice": factura_id,
        "reason": NC_REASON_DESCUENTO,
        "customer": {"identification": cliente_ident, "branch_office": 0},
        "seller": 341,
        "observations": f"Descuento pronto pago {descuento_pct:.0f}% (factura {factura_name})",
        "items": [{
            "code": primer.get("code"),
            "description": f"Descuento pronto pago {descuento_pct:.0f}%",
            "quantity": 1.0,
            "price": pre_iva,
            "taxes": [{"id": 7108}],  # IVA 19%
        }],
        "payments": [{"id": 3043, "value": round(monto_nc, 2)}],  # Efectivo (formalismo)
    }
    _audit("nc_request", {"factura_id": factura_id, "payload": payload}, actor)
    try:
        with SiigoClient() as s:
            r = s.post("/v1/credit-notes", payload)
    except httpx.HTTPStatusError as e:
        body = e.response.text[:400]
        _audit("nc_error", {"factura_id": factura_id, "status": e.response.status_code, "body": body}, actor)
        return PagoResult(ok=False, error=f"HTTP {e.response.status_code}: {body}")
    except Exception as e:
        return PagoResult(ok=False, error=str(e))
    _audit("nc_ok", {"factura_id": factura_id, "nc_id": r.get("id"), "nc_name": r.get("name")}, actor)
    return PagoResult(ok=True, nc_id=r.get("id"), nc_name=r.get("name"))
