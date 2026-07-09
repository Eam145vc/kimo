"""Escritura a Siigo con idempotencia y audit log."""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

log = logging.getLogger("skiimo.siigo_writer")

import httpx

from siigo_client import SiigoClient
from skiimo.config import (
    DEFAULT_INVOICE_DOC_ID,
    DEFAULT_IVA_TAX_ID,
    DEFAULT_SELLER_ID,
    INVOICE_DOC_ID_ELECTRONIC,
    SIIGO_INVOICE_TEST_MODE,
)


# Mapeo lenguaje natural -> payment_id de Siigo (formas de pago de contado)
# TEMPORAL: la cuenta nueva (ESSKIMO SAS) no tiene Nequi/Daviplata en Siigo;
# van como Efectivo hasta que se creen (actualizar ids aqui).
# OJO: Tarjeta Debito/Credito (1839/1840) estan INACTIVAS en Siigo.
PAYMENT_METHODS_CONTADO: dict[str, int] = {
    "efectivo": 1837,
    "nequi": 1837,
    "daviplata": 1837,
    "banco_ahorros": 13431,  # BANCOLOMBIA ESSKIMO 7556
    "tarjeta_debito": 1839,
    "tarjeta_credito": 1840,
}
CREDITO_PAYMENT_ID = 1838  # "Crédito" en Siigo
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


def _post_to_siigo_with_total_retry(endpoint: str, payload: dict) -> dict:
    """POST a Siigo. Si falla con invalid_total_payments y la diferencia con el total que
    Siigo calcula es <= 5 pesos (redondeo de centavos), reintenta con el total de Siigo.

    Usar para todos los endpoints que tienen 'payments' (invoices, purchases, support docs).
    Asume 1 sola entrada en payments (caso comun FV/FC/DS contado o credito).
    """
    try:
        with SiigoClient() as s:
            return s.post(endpoint, payload)
    except httpx.HTTPStatusError as e:
        body = e.response.text or ""
        if e.response.status_code == 400 and "invalid_total_payments" in body:
            import re as _re
            m = _re.search(r"calculated is (\d+\.?\d*)", body)
            if m:
                siigo_total = float(m.group(1))
                payments = payload.get("payments") or []
                if payments:
                    nuestro_total = float(payments[0].get("value") or 0)
                    diff = abs(siigo_total - nuestro_total)
                    if diff <= 5.0:
                        log.info(
                            "Retry %s con total de Siigo: ours=%.2f, siigo=%.2f, diff=%.2f",
                            endpoint, nuestro_total, siigo_total, diff,
                        )
                        new_payload = dict(payload)
                        new_payload["payments"] = [
                            dict(p, value=siigo_total) for p in payments
                        ]
                        with SiigoClient() as s2:
                            return s2.post(endpoint, new_payload)
        raise


def _cachear_factura_en_espejo(invoice_response: dict) -> None:
    """Inserta o actualiza una factura recien creada en siigo_invoices local.
    Esto evita el lag entre creacion y sync periodico — el bot ve sus propias
    facturas inmediatamente.
    """
    inv = invoice_response
    cust = inv.get("customer") or {}
    stamp = inv.get("stamp") or {}
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    try:
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
        conn.commit()
    finally:
        conn.close()


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
    doc_id: int | None = None,
) -> InvoiceResult:
    """Crea una factura de venta a partir de un ResolvedPedido.

    Args:
      payment_mode: 'credito' -> factura queda con saldo pendiente (CREDITO_PAYMENT_ID + due_date).
                    'contado' -> registra el pago en el momento (efectivo/nequi/etc).
      payment_method: solo aplica si payment_mode='contado'. Valores:
                      'efectivo', 'nequi', 'daviplata', 'banco_ahorros',
                      'tarjeta_debito', 'tarjeta_credito'.
      due_days: dias de plazo si es credito (default 30).
      doc_id: id del tipo de documento Siigo. Si None usa DEFAULT_INVOICE_DOC_ID (FV-1 tradicional).
              Pasar INVOICE_DOC_ID_ELECTRONIC para factura electronica DIAN.
    """
    # Validaciones de entrada
    if rp.cliente_elegido is None:
        return InvoiceResult(ok=False, error="Sin cliente resuelto")
    if not rp.items or any(i.elegido is None or i.precio_unitario is None for i in rp.items):
        return InvoiceResult(ok=False, error="Hay items sin resolver o sin precio")

    key = rp.idempotency_key
    existing = _existing_invoice_for_key(key)
    if existing:
        # Recuperar total/url de la factura ya sincronizada (puede no estar aun)
        total_prev = None
        url_prev = None
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT total, public_url FROM siigo_invoices WHERE id = ?",
                (existing["siigo_invoice_id"],),
            ).fetchone()
            if row:
                total_prev = row["total"]
                url_prev = row["public_url"]
        finally:
            conn.close()
        return InvoiceResult(
            ok=True,
            siigo_id=existing["siigo_invoice_id"],
            siigo_name=existing["siigo_invoice_name"],
            total=total_prev,
            public_url=url_prev,
            error="Pedido ya creado previamente (idempotencia)",
        )

    # Construir payload Siigo
    if doc_id is None:
        doc_id = DEFAULT_INVOICE_DOC_ID
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
        # IVA segun el TIPO DE FACTURA (no por producto). precio_unitario es PRE-IVA.
        #   - Electronica (FE, INVOICE_DOC_ID_ELECTRONIC): se discrimina el IVA -> price=pre-IVA + taxes[IVA].
        #     El cliente paga pre-IVA*1.19; el IVA se reporta a la DIAN.
        #   - Tradicional: NO se discrimina -> price=pre-IVA*1.19 (IVA incluido), sin taxes.
        #     El cliente paga lo MISMO que en FE, pero el IVA no se reporta.
        es_fe = doc_id == INVOICE_DOC_ID_ELECTRONIC
        tax_id = prod.iva_tax_id or DEFAULT_IVA_TAX_ID
        iva_pct = float(prod.iva_percentage) if prod.iva_percentage is not None else 19.0
        pre_iva = round(float(it.precio_unitario) * factor_descuento, 2)
        qty = float(it.cantidad)
        if es_fe:
            # Siigo aplica el IVA por encima del price
            precio_final = pre_iva
            item = {
                "code": prod.code,
                "description": prod.name,
                "quantity": qty,
                "price": precio_final,
                "taxes": [{"id": tax_id}],
            }
            # total = price * (1 + iva)
            item_total = round(qty * precio_final * (1.0 + iva_pct / 100.0), 2)
        else:
            # Tradicional: IVA incluido en el price, sin taxes (no se discrimina)
            precio_final = round(pre_iva * (1.0 + iva_pct / 100.0), 2)
            item = {
                "code": prod.code,
                "description": prod.name,
                "quantity": qty,
                "price": precio_final,
            }
            # total = price (sin IVA por encima)
            item_total = round(qty * precio_final, 2)
        siigo_items.append(item)
        # Calcular total CON LA MISMA FORMULA que Siigo: por item, redondeado a 2 dec,
        # despues sumamos. Esto evita el error "invalid_total_payments" por 1 centavo
        # de diferencia entre nuestro round y el de Siigo.
        total_value += item_total

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
        response = _post_to_siigo_with_total_retry("/v1/invoices", payload)
    except httpx.HTTPStatusError as e:
        err_msg = e.response.text[:500]
        _audit("invoice", None, "post_error",
               actor, {"status": e.response.status_code, "body": err_msg, "payload": payload})
        return InvoiceResult(ok=False, error=f"Siigo HTTP {e.response.status_code}: {err_msg}")
    except Exception as e:
        _audit("invoice", None, "post_exception", actor, {"error": str(e), "payload": payload})
        return InvoiceResult(ok=False, error=str(e))

    _audit("invoice", response.get("id"), "post_success", actor, response)

    # Cachear la factura en el espejo local inmediatamente
    # (asi ultima_venta, anular_factura, etc. la ven sin esperar al sync periodico)
    try:
        _cachear_factura_en_espejo(response)
    except Exception:
        # Best-effort. Si falla el cache, no falla la creacion.
        pass

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
    tipo_factura: str = "trad",
) -> InvoiceResult:
    """Crea factura de compra en Siigo a partir de un dict FacturaProveedor.

    Si doc_id no se pasa, se elige segun la categoria detectada:
      materias_primas -> PURCHASE_DOC_ID_MATERIAS
      gasto_administrativo / otro -> DEFAULT_PURCHASE_DOC_ID
    (En la cuenta ESSKIMO SAS ambos apuntan al mismo doc 7993: solo hay un doc de compras.)

    Args:
      tipo_factura: 'elec' (proveedor emite FE DIAN con CUFE) o 'trad' (sin CUFE).
                    Solo afecta el tag en observations y la auditoria.
                    Siigo no diferencia el doc_id por este motivo.
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
            iva_pct = float(it.get("iva_pct") or 19.0)
            precio_red = round(precio, 2)
            siigo_items.append({
                "type": "Product",
                "code": "GENERIC",  # placeholder; ideal: matchear contra catalogo
                "description": (it.get("descripcion") or "Item factura proveedor")[:200],
                "quantity": qty,
                "price": precio_red,
                "discount": 0.0,
                "taxes": [{"id": DEFAULT_IVA_TAX_ID}],
            })
            # Sumar item-por-item con el mismo redondeo que Siigo (evita off-by-one centavo)
            total_calc += round(qty * precio_red * (1.0 + iva_pct / 100.0), 2)
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

    tipo_tag = "FE" if tipo_factura == "elec" else "TRAD"
    obs_origen = factura.get("origen_obs") or "[CORREO]"
    obs_extra = (factura.get('observaciones') or '')[:180]
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
        "observations": f"{obs_origen} [{tipo_tag}] {obs_extra}".strip()[:300],
        "items": siigo_items,
        "payments": [{
            "id": 3049,  # Credito proveedores (por defecto, queda con saldo)
            "value": round(total_calc, 2),
            "due_date": (date.fromisoformat(fecha) + timedelta(days=30)).isoformat(),
        }],
    }

    _audit("purchase", None, "post_request", actor, payload)
    try:
        r = _post_to_siigo_with_total_retry("/v1/purchases", payload)
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


DS_DOC_ID = 25586
DS_DEFAULT_ACCOUNT_CODE = "511040"  # Honorarios. Se puede sobreescribir por item.


def crear_gasto_manual_ds(
    *,
    monto: float,
    descripcion: str,
    proveedor_nit: str,
    proveedor_nombre: str,
    fecha: str | None = None,
    actor: str = "bot",
    payment_mode: str = "contado",
    payment_method: str = "efectivo",
    due_days: int = 30,
) -> InvoiceResult:
    """Crea un DS (Documento Soporte) para un gasto manual sin factura del proveedor.

    Caso de uso: gastos chicos del dia a dia donde el proveedor no emitio factura
    electronica DIAN (taxis, mensajeros, honorarios, propinas profesionales, etc.).

    Si el proveedor no existe en Siigo, se intenta crearlo como Person.
    """
    if monto <= 0:
        return InvoiceResult(ok=False, error="Monto debe ser > 0")
    if not descripcion or not descripcion.strip():
        return InvoiceResult(ok=False, error="Descripcion requerida")
    if not proveedor_nit or not proveedor_nombre:
        return InvoiceResult(ok=False, error="NIT/cedula y nombre del proveedor son obligatorios para DS")

    # Verificar si el proveedor existe; si no, crearlo
    nit_clean = "".join(c for c in proveedor_nit if c.isdigit())
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM siigo_customers WHERE identification = ? LIMIT 1",
            (nit_clean,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        prov_result = crear_proveedor(
            nit=nit_clean, nombre=proveedor_nombre, tipo="persona", actor=actor,
        )
        if not prov_result.ok:
            return InvoiceResult(ok=False, error=f"No se pudo crear proveedor: {prov_result.error}")

    factura_dict = {
        "proveedor_nit": nit_clean,
        "proveedor_nombre": proveedor_nombre,
        "fecha": fecha or date.today().isoformat(),
        "prefijo_factura": "DS",
        "numero_factura": datetime.now().strftime("%Y%m%d%H%M%S"),
        "total": float(monto),
        "subtotal": float(monto),
        "items": [{
            "descripcion": descripcion[:200],
            "cantidad": 1,
            "precio_unitario": float(monto),
        }],
        "observaciones": f"[GASTO_MANUAL] {descripcion[:120]}",
        "origen_obs": "[CHAT]",
    }

    return crear_documento_soporte(
        factura_dict, actor=actor,
        payment_mode=payment_mode, payment_method=payment_method, due_days=due_days,
    )


def crear_documento_soporte(
    factura: dict,
    actor: str = "bot",
    *,
    payment_mode: str = "credito",
    payment_method: str = "efectivo",
    due_days: int = 30,
) -> InvoiceResult:
    """Crea Documento Soporte (DS) en Siigo para gastos a personas naturales
    o proveedores sin factura electronica DIAN.

    Endpoint: POST /v1/purchase-support-documents

    Args:
      payment_mode: 'credito' -> queda con saldo (id 3049, due_date a 30 dias).
                    'contado' -> registra pago al momento (efectivo/nequi/etc).
      payment_method: solo si payment_mode='contado'. Mismas claves que ventas:
                      efectivo, nequi, daviplata, banco_ahorros, tarjeta_debito, tarjeta_credito.
      due_days: dias de plazo si payment_mode='credito'.
    """
    nit = factura.get("proveedor_nit")
    if not nit:
        return InvoiceResult(ok=False, error="Sin NIT/cedula del proveedor")

    fecha = factura.get("fecha") or date.today().isoformat()
    try:
        date.fromisoformat(fecha)
    except ValueError:
        fecha = date.today().isoformat()

    items_in = factura.get("items") or []
    siigo_items = []
    total_calc = 0.0
    if items_in:
        for it in items_in:
            qty = float(it.get("cantidad") or 1)
            precio = float(it.get("precio_unitario") or 0)
            if precio <= 0:
                continue
            precio_red = round(precio, 2)
            siigo_items.append({
                "type": "Account",
                "code": DS_DEFAULT_ACCOUNT_CODE,
                "description": (it.get("descripcion") or "Servicio")[:200],
                "quantity": qty,
                "price": precio_red,
                "discount": 0.0,
            })
            # Suma item-por-item con redondeo simetrico (DS no lleva IVA en items Account)
            total_calc += round(qty * precio_red, 2)
    if not siigo_items:
        total = float(factura.get("total") or 0)
        if total <= 0:
            return InvoiceResult(ok=False, error="Sin items ni total")
        siigo_items.append({
            "type": "Account",
            "code": DS_DEFAULT_ACCOUNT_CODE,
            "description": f"Servicio - {factura.get('numero_factura', 'sin numero')}",
            "quantity": 1,
            "price": round(total, 2),
            "discount": 0.0,
        })
        total_calc = total

    # Bloque de pagos: contado vs credito
    total_pago = round(total_calc, 2)
    if payment_mode == "contado":
        pay_id = PAYMENT_METHODS_CONTADO.get(payment_method, PAYMENT_METHODS_CONTADO["efectivo"])
        payments_block = [{"id": pay_id, "value": total_pago}]
    else:
        payments_block = [{
            "id": 3049,  # Credito proveedores
            "value": total_pago,
            "due_date": (date.fromisoformat(fecha) + timedelta(days=due_days)).isoformat(),
        }]

    obs_origen = factura.get("origen_obs") or "[CORREO]"
    obs_extra = (factura.get('observaciones') or '')[:180]

    # supplier_receipt_number.number debe ser un consecutivo valido (Siigo rechaza "0").
    # Para gastos manuales sin numero de factura fisica generamos un consecutivo propio
    # basado en fecha+hora (unico y trazable), p.ej. 260527112412.
    numero_factura = str(factura.get("numero_factura") or "").strip()
    if not numero_factura or numero_factura == "0":
        numero_factura = datetime.now().strftime("%y%m%d%H%M%S")
    payload = {
        "document": {"id": DS_DOC_ID},
        "date": fecha,
        "supplier": {"identification": nit, "branch_office": 0},
        "supplier_receipt_number": {
            "prefix": (factura.get("prefijo_factura") or "DS")[:10],
            "number": numero_factura,
        },
        "discount_type": "Value",
        "observations": f"{obs_origen} {obs_extra}".strip()[:300],
        "items": siigo_items,
        "payments": payments_block,
    }

    _audit("support_document", None, "post_request", actor, payload)
    try:
        r = _post_to_siigo_with_total_retry("/v1/purchase-support-documents", payload)
    except httpx.HTTPStatusError as e:
        body = e.response.text[:500]
        _audit("support_document", None, "post_error", actor,
               {"status": e.response.status_code, "body": body})
        return InvoiceResult(ok=False, error=f"HTTP {e.response.status_code}: {body}")
    except Exception as e:
        return InvoiceResult(ok=False, error=str(e))

    _audit("support_document", r.get("id"), "post_success", actor, r)
    return InvoiceResult(
        ok=True, siigo_id=r.get("id"), siigo_name=r.get("name"),
        total=r.get("total"), raw=r,
    )


@dataclass(slots=True)
class CrearProveedorResult:
    ok: bool
    siigo_id: str | None = None
    identification: str | None = None
    name: str | None = None
    error: str | None = None
    raw: dict | None = None


def crear_proveedor(
    *,
    nit: str,
    nombre: str,
    tipo: str = "empresa",
    actor: str = "bot",
) -> CrearProveedorResult:
    """Crea un tercero proveedor en Siigo (POST /v1/customers).

    Args:
      nit: NIT/cedula sin guiones ni dv.
      nombre: razon social o nombre completo.
      tipo: 'empresa' -> Company + NIT (id_type 31).
            'persona' -> Person + cedula (id_type 13).
    """
    nit_clean = "".join(c for c in (nit or "") if c.isdigit())
    if not nit_clean:
        return CrearProveedorResult(ok=False, error="NIT/cedula invalido")
    nombre = (nombre or "").strip()
    if not nombre:
        return CrearProveedorResult(ok=False, error="Nombre requerido")

    person_type = "Company" if tipo == "empresa" else "Person"
    id_type_code = "31" if tipo == "empresa" else "13"

    # Construir lista de nombres segun tipo (Siigo separa nombres y apellidos para Person)
    if person_type == "Company":
        name_list = [nombre[:200]]
    else:
        partes = nombre.split()
        if len(partes) >= 2:
            name_list = [" ".join(partes[:-1])[:100], partes[-1][:100]]
        else:
            name_list = [nombre[:100], "."]  # Siigo exige minimo 2 elementos para Person

    payload = {
        "type": "Supplier",
        "person_type": person_type,
        "id_type": {"code": id_type_code},
        "identification": nit_clean,
        "name": name_list,
        "commercial_name": nombre[:200],
        "active": True,
        "vat_responsible": False,
        "fiscal_responsibilities": [{"code": "R-99-PN"}],  # No responsable
        "address": {
            "address": "Sin direccion",
            "city": {"country_code": "Co", "state_code": "11", "city_code": "11001"},
        },
        "contacts": [{
            "first_name": name_list[0][:50],
            "last_name": (name_list[1] if len(name_list) > 1 else nombre)[:50],
        }],
    }

    _audit("supplier", None, "post_request", actor, payload)
    try:
        with SiigoClient() as s:
            r = s.post("/v1/customers", payload)
    except httpx.HTTPStatusError as e:
        body = e.response.text[:500]
        _audit("supplier", None, "post_error", actor,
               {"status": e.response.status_code, "body": body, "payload": payload})
        return CrearProveedorResult(ok=False, error=f"HTTP {e.response.status_code}: {body}")
    except Exception as e:
        _audit("supplier", None, "post_exception", actor, {"error": str(e)})
        return CrearProveedorResult(ok=False, error=str(e))

    _audit("supplier", r.get("id"), "post_success", actor, r)

    # Cachear en espejo local
    try:
        conn = get_conn()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO siigo_customers
                   (id, type, identification, name, commercial_name, email, active, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
                (
                    r.get("id"),
                    "Supplier",
                    nit_clean,
                    nombre[:200],
                    nombre[:200],
                    "",
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # best-effort

    return CrearProveedorResult(
        ok=True,
        siigo_id=r.get("id"),
        identification=nit_clean,
        name=nombre,
        raw=r,
    )


@dataclass(slots=True)
class CrearClienteResult:
    ok: bool
    siigo_id: str | None = None
    identification: str | None = None
    name: str | None = None
    error: str | None = None
    raw: dict | None = None


def crear_cliente(
    *,
    nit: str,
    nombre: str,
    tipo: str = "empresa",
    categoria: str = "DETAL",
    email: str = "",
    actor: str = "bot",
) -> CrearClienteResult:
    """Crea un cliente en Siigo (POST /v1/customers con type=Customer).

    Args:
      nit: NIT/cedula sin guiones ni dv.
      nombre: razon social o nombre completo.
      tipo: 'empresa' -> Company + NIT (id_type 31).
            'persona' -> Person + cedula (id_type 13).
      categoria: DETAL | MAYORISTA | DISTRIBUIDOR (afecta precios sugeridos).
    """
    nit_clean = "".join(c for c in (nit or "") if c.isdigit())
    if not nit_clean:
        return CrearClienteResult(ok=False, error="NIT/cedula invalido")
    nombre = (nombre or "").strip()
    if not nombre:
        return CrearClienteResult(ok=False, error="Nombre requerido")
    if categoria not in ("DETAL", "MAYORISTA", "DISTRIBUIDOR"):
        categoria = "DETAL"

    person_type = "Company" if tipo == "empresa" else "Person"
    id_type_code = "31" if tipo == "empresa" else "13"

    if person_type == "Company":
        name_list = [nombre[:200]]
    else:
        partes = nombre.split()
        if len(partes) >= 2:
            name_list = [" ".join(partes[:-1])[:100], partes[-1][:100]]
        else:
            name_list = [nombre[:100], "."]

    payload = {
        "type": "Customer",
        "person_type": person_type,
        "id_type": {"code": id_type_code},
        "identification": nit_clean,
        "name": name_list,
        "commercial_name": nombre[:200],
        "active": True,
        "vat_responsible": False,
        "fiscal_responsibilities": [{"code": "R-99-PN"}],
        "address": {
            "address": "Sin direccion",
            "city": {"country_code": "Co", "state_code": "11", "city_code": "11001"},
        },
        "contacts": [{
            "first_name": name_list[0][:50],
            "last_name": (name_list[1] if len(name_list) > 1 else nombre)[:50],
            "email": email[:80] if email else None,
        }],
    }
    if email:
        payload["contacts"][0]["email"] = email[:80]

    _audit("customer", None, "post_request", actor, payload)
    try:
        with SiigoClient() as s:
            r = s.post("/v1/customers", payload)
    except httpx.HTTPStatusError as e:
        body = e.response.text[:500]
        _audit("customer", None, "post_error", actor,
               {"status": e.response.status_code, "body": body, "payload": payload})
        return CrearClienteResult(ok=False, error=f"HTTP {e.response.status_code}: {body}")
    except Exception as e:
        _audit("customer", None, "post_exception", actor, {"error": str(e)})
        return CrearClienteResult(ok=False, error=str(e))

    _audit("customer", r.get("id"), "post_success", actor, r)

    siigo_id = r.get("id")
    # Cachear en espejo local + setear categoria comercial
    try:
        conn = get_conn()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO siigo_customers
                   (id, type, identification, name, commercial_name, email, active, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?)""",
                (
                    siigo_id, "Customer", nit_clean,
                    nombre[:200], nombre[:200], email[:80],
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            conn.execute(
                """INSERT OR REPLACE INTO clientes_categoria (customer_id, categoria, updated_at)
                   VALUES (?, ?, ?)""",
                (siigo_id, categoria, datetime.now().isoformat(timespec="seconds")),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass  # best-effort

    return CrearClienteResult(
        ok=True,
        siigo_id=siigo_id,
        identification=nit_clean,
        name=nombre,
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


def crear_nota_credito_anulacion(invoice_id: str, motivo: str = "Anulacion solicitada",
                                    actor: str = "bot") -> InvoiceResult:
    """Crea nota credito que neutraliza toda la factura (devolucion total).
    Usar cuando /annul no funciona (factura electronica DIAN o con pagos).

    Toma los items originales de la factura y los replica en la NC.
    """
    # Traer la factura completa
    try:
        with SiigoClient() as s:
            inv = s.get(f"/v1/invoices/{invoice_id}")
    except Exception as e:
        return InvoiceResult(ok=False, error=f"No pude obtener la factura: {e}")

    fv_doc_id = (inv.get("document") or {}).get("id")
    nc_doc_id = 42547 if fv_doc_id == 42546 else 7995  # electronica vs tradicional

    items_fv = inv.get("items") or []
    if not items_fv:
        return InvoiceResult(ok=False, error="La factura no tiene items")

    nc_items = []
    for it in items_fv:
        nc_it = {
            "code": it.get("code"),
            "description": it.get("description"),
            "quantity": float(it.get("quantity") or 1),
            "price": float(it.get("price") or 0),
        }
        # Replicar fielmente los impuestos de la factura original: si el item
        # era excluido (sin taxes) NO le inventamos IVA, o la NC descuadra.
        taxes_orig = it.get("taxes")
        if taxes_orig:
            nc_it["taxes"] = taxes_orig
        nc_items.append(nc_it)

    total = float(inv.get("total") or 0)
    customer_ident = (inv.get("customer") or {}).get("identification") or ""

    payload = {
        "document": {"id": nc_doc_id},
        "date": date.today().isoformat(),
        "invoice": invoice_id,
        "reason": 2,  # 2 = Anulacion
        "customer": {"identification": customer_ident, "branch_office": 0},
        "seller": DEFAULT_SELLER_ID,
        "observations": f"Anulacion factura {inv.get('name')} - {motivo}"[:300],
        "items": nc_items,
        "payments": [{"id": 1837, "value": round(total, 2)}],  # Efectivo (formalismo)
    }

    _audit("credit_note", None, "annul_request", actor,
           {"invoice_id": invoice_id, "payload": payload})
    try:
        with SiigoClient() as s:
            r = s.post("/v1/credit-notes", payload)
    except httpx.HTTPStatusError as e:
        body = e.response.text[:400]
        _audit("credit_note", None, "annul_error", actor,
               {"invoice_id": invoice_id, "status": e.response.status_code, "body": body})
        return InvoiceResult(ok=False, error=f"HTTP {e.response.status_code}: {body}")
    except Exception as e:
        return InvoiceResult(ok=False, error=str(e))

    _audit("credit_note", r.get("id"), "annul_success", actor, r)
    return InvoiceResult(
        ok=True,
        siigo_id=r.get("id"),
        siigo_name=r.get("name"),
        total=r.get("total"),
        raw=r,
    )
