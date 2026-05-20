"""Motor de precios de 3 niveles.

Politica:
  Nivel 1: precio base segun categoria del cliente (DETAL / MAYORISTA / DISTRIBUIDOR).
  Nivel 2: descuento pronto pago (se aplica al cobrar, no al facturar).
  Nivel 3: excepciones manuales documentadas.

Funciones:
  - sugerir_precio(product_code, customer_id) -> dict
  - obtener_pronto_pago(customer_id) -> list[dict]
  - registrar_excepcion(...)
"""
from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass

from skiimo.db.schema import get_conn


# Categorias canonicas
CATEGORIAS = ("DETAL", "MAYORISTA", "DISTRIBUIDOR")
DEFAULT_CATEGORIA = "DETAL"


@dataclass(slots=True)
class PrecioSugerido:
    product_code: str
    categoria: str           # DETAL/MAYORISTA/DISTRIBUIDOR del cliente
    precio_pre_iva: float    # lo que va al campo `price` en Siigo
    precio_con_iva: float    # para mostrar al usuario
    fuente: str              # tabla_oficial | catalogo_siigo | ultima_venta | desconocido
    confianza: str           # alta | media | baja
    detalle: str | None = None


def get_categoria_cliente(customer_id: str | None) -> str:
    if not customer_id:
        return DEFAULT_CATEGORIA
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT categoria FROM clientes_categoria WHERE customer_id = ?",
            (customer_id,),
        ).fetchone()
    finally:
        conn.close()
    return row["categoria"] if row else DEFAULT_CATEGORIA


def sugerir_precio(product_code: str, customer_id: str | None) -> PrecioSugerido:
    """Devuelve el precio sugerido para un producto y cliente."""
    cat = get_categoria_cliente(customer_id)
    conn = get_conn()
    try:
        # 1) Tabla precios_oficiales: precio aprobado por el dueno
        row = conn.execute(
            """SELECT po.precio_pre_iva, po.precio_con_iva, po.confirmed_by, po.fuente
               FROM precios_oficiales po
               JOIN siigo_products p ON p.code = po.product_code
               WHERE po.product_code = ? AND po.lista = ?""",
            (product_code, cat),
        ).fetchone()
        if row:
            return PrecioSugerido(
                product_code=product_code,
                categoria=cat,
                precio_pre_iva=float(row["precio_pre_iva"]),
                precio_con_iva=float(row["precio_con_iva"] or 0),
                fuente="tabla_oficial" if row["confirmed_by"] else f"tabla_{row['fuente']}",
                confianza="alta",
            )

        # 2) Catalogo Siigo: prices.price_list por posicion
        # DETAL=1, MAYORISTA=2, DISTRIBUIDOR=3 (CORTESIA no aplica como categoria)
        pos_map = {"DETAL": 1, "MAYORISTA": 2, "DISTRIBUIDOR": 3}
        pos = pos_map.get(cat, 1)
        prod = conn.execute(
            "SELECT raw, iva_percentage, tax_included FROM siigo_products WHERE code = ?",
            (product_code,),
        ).fetchone()
        if prod:
            import json
            raw = json.loads(prod["raw"])
            prices = raw.get("prices") or []
            iva_pct = prod["iva_percentage"] or 19.0
            tax_included = bool(prod["tax_included"])
            for currency in prices:
                price_list = currency.get("price_list") or []
                # Buscar en la posicion exacta
                for entry in price_list:
                    if entry.get("position") == pos:
                        value = float(entry.get("value") or 0)
                        if value > 0:
                            if tax_included:
                                pre = value / (1 + iva_pct / 100)
                                con = value
                            else:
                                pre = value
                                con = value * (1 + iva_pct / 100)
                            return PrecioSugerido(
                                product_code=product_code,
                                categoria=cat,
                                precio_pre_iva=pre,
                                precio_con_iva=con,
                                fuente="catalogo_siigo",
                                confianza="media",
                                detalle=f"de la lista pos={pos}",
                            )
                # Fallback: si la posicion buscada esta vacia, usar DETAL (pos 1)
                if cat != "DETAL":
                    for entry in price_list:
                        if entry.get("position") == 1:
                            value = float(entry.get("value") or 0)
                            if value > 0:
                                if tax_included:
                                    pre = value / (1 + iva_pct / 100)
                                    con = value
                                else:
                                    pre = value
                                    con = value * (1 + iva_pct / 100)
                                return PrecioSugerido(
                                    product_code=product_code,
                                    categoria=cat,
                                    precio_pre_iva=pre,
                                    precio_con_iva=con,
                                    fuente="catalogo_detal_fallback",
                                    confianza="baja",
                                    detalle=f"lista {cat} vacia, uso DETAL",
                                )
                break  # solo COP

        # 3) Ultima venta a este cliente del mismo producto (sin categoria)
        if customer_id:
            row = conn.execute(
                """SELECT i.items_json, i.date FROM siigo_invoices i
                   WHERE i.customer_id = ? AND i.items_json LIKE ?
                   ORDER BY i.date DESC LIMIT 1""",
                (customer_id, f'%"code": "{product_code}"%'),
            ).fetchone()
            if row:
                import json
                items = json.loads(row["items_json"])
                for it in items:
                    if it.get("code") == product_code:
                        pre = float(it.get("price") or 0)
                        if pre > 0:
                            return PrecioSugerido(
                                product_code=product_code,
                                categoria=cat,
                                precio_pre_iva=pre,
                                precio_con_iva=pre * 1.19,
                                fuente="ultima_venta_cliente",
                                confianza="media",
                                detalle=f"venta {row['date']}",
                            )
    finally:
        conn.close()

    return PrecioSugerido(
        product_code=product_code,
        categoria=cat,
        precio_pre_iva=0.0,
        precio_con_iva=0.0,
        fuente="desconocido",
        confianza="baja",
        detalle="no hay precio definido en ninguna fuente",
    )


def obtener_pronto_pago(customer_id: str | None) -> list[dict]:
    """Devuelve las escalas de pronto pago de un cliente, ordenadas por dias_max."""
    if not customer_id:
        return []
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT dias_max, descuento_pct, notas FROM clientes_pronto_pago
               WHERE customer_id = ? AND activo = 1
               ORDER BY dias_max ASC""",
            (customer_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def calcular_descuento_pronto_pago(customer_id: str, dias_transcurridos: int) -> tuple[float, str | None]:
    """Para un cliente y N dias, devuelve (descuento_pct, detalle)."""
    escalas = obtener_pronto_pago(customer_id)
    if not escalas:
        return 0.0, None
    # primera escala donde dias_transcurridos <= dias_max
    for e in escalas:
        if dias_transcurridos <= e["dias_max"]:
            return float(e["descuento_pct"]), f"Pago en {dias_transcurridos}d (escala <={e['dias_max']}d = {e['descuento_pct']:.0f}%)"
    return 0.0, "Pagado fuera de plazo de pronto pago"


def registrar_excepcion(
    pedido_id: int | None,
    customer_id: str | None,
    product_code: str,
    precio_oficial: float | None,
    precio_aplicado: float,
    razon: str,
    actor: str,
) -> None:
    delta = ((precio_aplicado / precio_oficial - 1) * 100) if precio_oficial else None
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO excepciones_precio
               (pedido_id, customer_id, product_code, precio_oficial, precio_aplicado, delta_pct, razon, actor, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pedido_id, customer_id, product_code, precio_oficial, precio_aplicado, delta,
             razon, actor, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    # Smoke test: sugerencia sin cliente
    p = sugerir_precio("A1AO", None)
    print(f"A1AO sin cliente: categoria={p.categoria} precio_pre_iva=${p.precio_pre_iva:,.2f}")
