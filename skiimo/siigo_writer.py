"""Escritura a Siigo con idempotencia y audit log."""
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import httpx

from siigo_client import SiigoClient
from skiimo.config import (
    DEFAULT_INVOICE_DOC_ID,
    DEFAULT_IVA_TAX_ID,
    DEFAULT_SELLER_ID,
    SIIGO_INVOICE_TEST_MODE,
)


# Mapeo lenguaje natural -> payment_id de Siigo (formas de pago de contado)
PAYMENT_METHODS_CONTADO: dict[str, int] = {
    "efectivo": 3043,
    "nequi": 8102,
    "daviplata": 8103,
    "banco_ahorros": 8104,
    "tarjeta_debito": 3045,
    "tarjeta_credito": 3046,
}
CREDITO_PAYMENT_ID = 3044  # "Crédito" en Siigo
from skiimo.db.schema import get_conn
from skiimo.pipeline import ResolvedPedido


@dataclass(slots=True)
class InvoiceResult:
    ok: bool
    siigo_id: str | None = None
    siigo_name: str | None = None
    total: float | None = None
    public_url: str | None = None
    error: str | None = None
    raw: dict | None = None


def _audit(entity: str, entity_id: str | None, action: str, actor: str, payload: dict) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO audit_log (entity, entity_id, action, actor, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (entity, entity_id, action, actor,
             json.dumps(payload, ensure_ascii=False, default=str),
             datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()


def _existing_invoice_for_key(idempotency_key: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, siigo_invoice_id, siigo_invoice_name, estado FROM bot_pedidos "
            "WHERE idempotency_key = ? AND estado = 'enviado'",
            (idempotency_key,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def crear_factura_venta(
    rp: ResolvedPedido,
    actor: str = "bot",
    *,
    payment_mode: str = "credito",
    payment_method: str = "efectivo",
    due_days: int = 30,
) -> InvoiceResult:
    """Crea una factura de venta a partir de un ResolvedPedido.

    Args:
      payment_mode: 'credito' -> factura queda con saldo pendiente (id 3044 + due_date).
                    'contado' -> registra el pago en el momento (efectivo/nequi/etc).
      payment_method: solo aplica si payment_mode='contado'. Valores:
                      'efectivo', 'nequi', 'daviplata', 'banco_ahorros',
                      'tarjeta_debito', 'tarjeta_credito'.
      due_days: dias de plazo si es credito (default 30).
    """
    # Validaciones de entrada
    if rp.cliente_elegido is None:
        return InvoiceResult(ok=False, error="Sin cliente resuelto")
    if not rp.items or any(i.elegido is None or i.precio_unitario is None for i in rp.items):
        return InvoiceResult(ok=False, error="Hay items sin resolver o sin precio")

    key = rp.idempotency_key
    existing = _existing_invoice_for_key(key)
    if existing:
        return InvoiceResult(
            ok=True,
            siigo_id=existing["siigo_invoice_id"],
            siigo_name=existing["siigo_invoice_name"],
            error="Pedido ya creado previamente (idempotencia)",
        )

    # Construir payload Siigo
    doc_id = DEFAULT_INVOICE_DOC_ID  # tradicional para modo test/dev
    fecha = rp.raw.fecha_entrega or date.today().isoformat()
    try:
        # validar fecha
        date.fromisoformat(fecha)
    except ValueError:
        fecha = date.today().isoformat()

    # Descuento puntual: lo aplicamos proporcional sobre cada item (bajamos price).
    # Siigo no tiene "descuento global" en el payload; el factor lo aplicamos linea a linea.
    dto_pct = rp.raw.descuento_pct or 0.0
    factor_descuento = max(0.0, 1.0 - (dto_pct / 100.0)) if dto_pct > 0 else 1.0

    siigo_items = []
    total_value = 0.0
    for it in rp.items:
        prod = it.elegido
        if prod is None or it.precio_unitario is None:
            return InvoiceResult(ok=False, error=f"Item '{it.raw.descripcion}' sin producto o precio")
        if it.precio_unitario <= 0:
            return InvoiceResult(ok=False, error=f"Item '{prod.code}' tiene precio cero")
        tax_id = prod.iva_tax_id or DEFAULT_IVA_TAX_ID
        # Siigo solo acepta hasta 2 decimales en price
        precio_final = round(float(it.precio_unitario) * factor_descuento, 2)
        siigo_items.append({
            "code": prod.code,
            "description": prod.name,
            "quantity": float(it.cantidad),
            "price": precio_final,
            "taxes": [{"id": tax_id}],
        })
        total_value += (it.total_estimado or 0.0) * factor_descuento

    obs_partes = []
    if SIIGO_INVOICE_TEST_MODE:
        obs_partes.append("[TEST BOT]")
    if dto_pct > 0:
        motivo = rp.raw.descuento_motivo or "sin motivo"
        obs_partes.append(f"Descuento puntual {dto_pct:.0f}% ({motivo})")
    if rp.raw.observaciones:
        obs_partes.append(rp.raw.observaciones)
    observaciones = " | ".join(obs_partes)[:300]

    # Construir bloque de pagos segun modo
    total_payment = round(total_value, 2)
    if payment_mode == "contado":
        pay_id = PAYMENT_METHODS_CONTADO.get(payment_method, PAYMENT_METHODS_CONTADO["efectivo"])
        payments_block = [{"id": pay_id, "value": total_payment}]
    else:
        # Credito: requiere due_date
        from datetime import timedelta as _td
        due = (date.fromisoformat(fecha) + _td(days=due_days)).isoformat()
        payments_block = [{
            "id": CREDITO_PAYMENT_ID,
            "value": total_payment,
            "due_date": due,
        }]

    payload = {
        "document": {"id": doc_id},
        "date": fecha,
        "customer": {
            "identification": rp.cliente_elegido.identification,
            "branch_office": 0,
        },
        "seller": DEFAULT_SELLER_ID,
        "observations": observaciones,
        "items": siigo_items,
        "payments": payments_block,
    }

    _audit("invoice", None, "post_request", actor, payload)

    try:
        with SiigoClient() as s:
            response = s.post("/v1/invoices", payload)
    except httpx.HTTPStatusError as e:
        err_msg = e.response.text[:500]
        _audit("invoice", None, "post_error",
               actor, {"status": e.response.status_code, "body": err_msg, "payload": payload})
        return InvoiceResult(ok=False, error=f"Siigo HTTP {e.response.status_code}: {err_msg}")
    except Exception as e:
        _audit("invoice", None, "post_exception", actor, {"error": str(e), "payload": payload})
        return InvoiceResult(ok=False, error=str(e))

    _audit("invoice", response.get("id"), "post_success", actor, response)

    # Si hubo descuento puntual, registrar la excepcion
    if dto_pct > 0:
        try:
            from skiimo.pricing.engine import registrar_excepcion
            for it in rp.items:
                if it.elegido and it.precio_unitario:
                    registrar_excepcion(
                        pedido_id=None,
                        customer_id=rp.cliente_elegido.id if rp.cliente_elegido else None,
                        product_code=it.elegido.code,
                        precio_oficial=float(it.precio_unitario),
                        precio_aplicado=float(it.precio_unitario) * factor_descuento,
                        razon=f"Descuento puntual {dto_pct:.0f}% - {rp.raw.descuento_motivo or 'sin motivo'}",
                        actor=actor,
                    )
        except Exception:
            pass

    return InvoiceResult(
        ok=True,
        siigo_id=response.get("id"),
        siigo_name=response.get("name"),
        total=response.get("total"),
        public_url=response.get("public_url"),
        raw=response,
    )


def crear_factura_compra(
    factura: dict,
    *,
    doc_id: int | None = None,
    actor: str = "bot",
) -> InvoiceResult:
    """Crea factura de compra en Siigo a partir de un dict FacturaProveedor.

    Si doc_id no se pasa, se elige segun la categoria detectada:
      materias_primas -> 13219 (MATERIAS PRIMAS)
      gasto_administrativo / otro -> 27394 (GASTO ADMINISTRATIVO)
    """
    from skiimo.config import DEFAULT_PURCHASE_DOC_ID, PURCHASE_DOC_ID_MATERIAS

    nit = factura.get("proveedor_nit")
    if not nit:
        return InvoiceResult(ok=False, error="Sin NIT del proveedor")

    # Elegir doc_id
    if doc_id is None:
        cat = (factura.get("categoria") or "gasto_administrativo").lower()
        doc_id = PURCHASE_DOC_ID_MATERIAS if cat == "materias_primas" else DEFAULT_PURCHASE_DOC_ID

    # Fecha
    fecha = factura.get("fecha") or date.today().isoformat()
    try:
        date.fromisoformat(fecha)
    except ValueError:
        fecha = date.today().isoformat()

    # Items: si el OCR no extrajo items, generamos uno generico con el total
    items_in = factura.get("items") or []
    siigo_items = []
    total_calc = 0.0
    if items_in:
        for it in items_in:
            qty = float(it.get("cantidad") or 1)
            precio = float(it.get("precio_unitario") or 0)
            if precio <= 0:
                continue
            siigo_items.append({
                "type": "Product",
                "code": "GENERIC",  # placeholder; ideal: matchear contra catalogo
                "description": (it.get("descripcion") or "Item factura proveedor")[:200],
                "quantity": qty,
                "price": round(precio, 2),
                "discount": 0.0,
                "taxes": [{"id": DEFAULT_IVA_TAX_ID}],
            })
            total_calc += qty * precio * 1.19  # con IVA estimado
    if not siigo_items:
        # Fallback: 1 item generico con el total
        total = float(factura.get("total") or 0)
        if total <= 0:
            return InvoiceResult(ok=False, error="Sin items ni total")
        # Sacar pre-IVA
        subtotal = float(factura.get("subtotal") or (total / 1.19))
        siigo_items.append({
            "type": "Product",
            "code": "GENERIC",
            "description": f"Factura {factura.get('numero_factura', 'sin numero')}",
            "quantity": 1,
            "price": round(subtotal, 2),
            "discount": 0.0,
            "taxes": [{"id": DEFAULT_IVA_TAX_ID}],
        })
        total_calc = total

    payload = {
        "document": {"id": doc_id},
        "date": fecha,
        "supplier": {"identification": nit, "branch_office": 0},
        "provider_invoice": {
            "prefix": (factura.get("prefijo_factura") or "")[:10],
            "number": str(factura.get("numero_factura") or "0"),
        },
        "discount_type": "Value",
        "supplier_by_item": False,
        "observations": f"[CORREO] {(factura.get('observaciones') or '')[:200]}",
        "items": siigo_items,
        "payments": [{
            "id": 3049,  # Credito proveedores (por defecto, queda con saldo)
            "value": round(total_calc, 2),
            "due_date": (date.fromisoformat(fecha) + timedelta(days=30)).isoformat(),
        }],
    }

    _audit("purchase", None, "post_request", actor, payload)
    try:
        with SiigoClient() as s:
            r = s.post("/v1/purchases", payload)
    except httpx.HTTPStatusError as e:
        body = e.response.text[:500]
        _audit("purchase", None, "post_error", actor,
               {"status": e.response.status_code, "body": body, "payload": payload})
        return InvoiceResult(ok=False, error=f"HTTP {e.response.status_code}: {body}")
    except Exception as e:
        return InvoiceResult(ok=False, error=str(e))

    _audit("purchase", r.get("id"), "post_success", actor, r)
    return InvoiceResult(
        ok=True,
        siigo_id=r.get("id"),
        siigo_name=r.get("name"),
        total=r.get("total"),
        raw=r,
    )


def get_invoice_pdf(invoice_id: str) -> bytes | None:
    """Devuelve el PDF de la factura como bytes."""
    try:
        with SiigoClient() as s:
            data = s.get(f"/v1/invoices/{invoice_id}/pdf")
        b64 = data.get("base64", "")
        return base64.b64decode(b64) if b64 else None
    except httpx.HTTPStatusError:
        return None


def anular_factura(invoice_id: str, actor: str = "bot") -> InvoiceResult:
    """Anula via POST /annul."""
    try:
        with SiigoClient() as s:
            response = s.post(f"/v1/invoices/{invoice_id}/annul", {})
        _audit("invoice", invoice_id, "annul", actor, response)
        return InvoiceResult(ok=True, siigo_id=invoice_id, raw=response)
    except httpx.HTTPStatusError as e:
        _audit("invoice", invoice_id, "annul_error",
               actor, {"status": e.response.status_code, "body": e.response.text[:300]})
        return InvoiceResult(ok=False, error=f"HTTP {e.response.status_code}: {e.response.text[:300]}")
    except Exception as e:
        return InvoiceResult(ok=False, error=str(e))
