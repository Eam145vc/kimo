"""Pipeline: mensaje crudo -> Pedido extraido -> matching catalogo -> propuesta lista para confirmar.

Esta capa no toca Telegram ni Siigo todavia. Solo prepara el resumen para mostrar al vendedor.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date

from skiimo.config import SIIGO_INVOICE_TEST_MODE, SIIGO_TEST_CUSTOMER_ID
from skiimo.llm.schemas import Pedido, PedidoItem
from skiimo.matcher import CustomerHit, Matcher, ProductHit


@dataclass(slots=True)
class ResolvedItem:
    """Item con producto Siigo resuelto."""
    raw: PedidoItem
    candidatos: list[ProductHit] = field(default_factory=list)
    elegido: ProductHit | None = None
    cantidad: float = 1.0
    precio_unitario: float | None = None  # pre-IVA

    @property
    def total_estimado(self) -> float | None:
        if self.elegido is None or self.precio_unitario is None:
            return None
        base = self.cantidad * self.precio_unitario
        if self.elegido.iva_percentage:
            return base * (1 + self.elegido.iva_percentage / 100)
        return base


@dataclass(slots=True)
class ResolvedPedido:
    """Pedido con todo resuelto contra catalogos."""
    raw: Pedido
    cliente_candidatos: list[CustomerHit] = field(default_factory=list)
    cliente_elegido: CustomerHit | None = None
    items: list[ResolvedItem] = field(default_factory=list)
    test_mode: bool = SIIGO_INVOICE_TEST_MODE
    necesita_input_humano: list[str] = field(default_factory=list)  # lista de problemas
    cliente_real_para_precios: CustomerHit | None = None  # solo en modo test, el cliente que se uso para precios

    @property
    def subtotal_estimado(self) -> float:
        """Total antes de descuento puntual."""
        return sum((i.total_estimado or 0.0) for i in self.items)

    @property
    def descuento_valor(self) -> float:
        """Valor absoluto del descuento puntual."""
        pct = self.raw.descuento_pct or 0.0
        if pct <= 0:
            return 0.0
        return self.subtotal_estimado * pct / 100.0

    @property
    def total_estimado(self) -> float:
        """Total final con descuento puntual aplicado."""
        return self.subtotal_estimado - self.descuento_valor

    @property
    def idempotency_key(self) -> str:
        material = {
            "cliente": self.cliente_elegido.identification if self.cliente_elegido else (self.raw.cliente_nombre or ""),
            "items": [
                {
                    "code": i.elegido.code if i.elegido else i.raw.descripcion,
                    "qty": i.cantidad,
                    "price": i.precio_unitario,
                }
                for i in self.items
            ],
            "fecha": self.raw.fecha_entrega or date.today().isoformat(),
        }
        blob = json.dumps(material, sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(blob).hexdigest()[:32]


def resolve_pedido(pedido: Pedido, matcher: Matcher) -> ResolvedPedido:
    """Toma un Pedido recien extraido y lo resuelve contra el catalogo local."""
    resolved = ResolvedPedido(raw=pedido)
    problemas: list[str] = []

    # En modo test seguimos usando el cliente test para enviar a Siigo,
    # pero buscamos el cliente "real" mencionado por el vendedor para calcular precio
    # con su categoria correcta. Asi las pruebas reflejan la realidad.
    cliente_real_para_precios = None

    # --- CLIENTE ---
    if pedido.cliente_nit:
        hit = matcher.find_customer_by_identification(pedido.cliente_nit)
        if hit:
            cliente_real_para_precios = hit
            resolved.cliente_elegido = hit
        else:
            problemas.append(f"NIT {pedido.cliente_nit} no encontrado en el sistema")
    elif pedido.cliente_nombre:
        candidatos = matcher.search_customer(pedido.cliente_nombre, limit=5)
        resolved.cliente_candidatos = candidatos
        if candidatos and candidatos[0].score >= 90:
            cliente_real_para_precios = candidatos[0]
            resolved.cliente_elegido = candidatos[0]
        elif not candidatos:
            problemas.append(f"Cliente '{pedido.cliente_nombre}' no encontrado")
        else:
            problemas.append(f"Cliente '{pedido.cliente_nombre}' tiene varios candidatos, elegir uno")
    else:
        problemas.append("No se especifico cliente")

    # En modo test: sobrescribir cliente_elegido con el cliente test (para que la factura
    # vaya alli), pero conservamos cliente_real_para_precios para calcular precio correcto
    if SIIGO_INVOICE_TEST_MODE and SIIGO_TEST_CUSTOMER_ID:
        for c in matcher._customers:
            if c["id"] == SIIGO_TEST_CUSTOMER_ID:
                resolved.cliente_real_para_precios = cliente_real_para_precios
                resolved.cliente_elegido = CustomerHit(
                    id=c["id"],
                    identification=c["identification"],
                    name=c["name"],
                    commercial_name=c.get("commercial_name") or "",
                    email=c.get("email") or "",
                    score=100.0,
                )
                # quitar problemas relacionados con cliente
                problemas = [p for p in problemas if "cliente" not in p.lower() and "NIT" not in p]
                break

    # --- ITEMS ---
    from skiimo.pricing.engine import sugerir_precio

    # Si tenemos cliente real, usamos su categoria para el precio. Si no, el del cliente_elegido (test).
    customer_id_para_precio = (cliente_real_para_precios.id if cliente_real_para_precios
                                else (resolved.cliente_elegido.id if resolved.cliente_elegido else None))
    customer_id = customer_id_para_precio
    for raw_item in pedido.items:
        # Si el LLM dio codigo, intentar match directo primero
        hit_directo = None
        codigo_llm = (getattr(raw_item, "codigo", None) or "").strip().upper()
        if codigo_llm:
            hit_directo = matcher.find_product_by_code(codigo_llm)

        if hit_directo:
            # Tambien traemos candidatos por descripcion para que el usuario pueda cambiar si el codigo del LLM fue erroneo
            candidatos = matcher.search_product(raw_item.descripcion or hit_directo.name, limit=5)
            # Asegurar que el hit_directo este en candidatos
            if not any(c.id == hit_directo.id for c in candidatos):
                candidatos = [hit_directo] + candidatos[:4]
            ri = ResolvedItem(raw=raw_item, candidatos=candidatos, cantidad=raw_item.cantidad)
            ri.elegido = hit_directo
        else:
            candidatos = matcher.search_product(raw_item.descripcion, limit=5)
            ri = ResolvedItem(raw=raw_item, candidatos=candidatos, cantidad=raw_item.cantidad)
            if candidatos:
                ri.elegido = candidatos[0]
            else:
                problemas.append(f"Producto '{raw_item.descripcion}' no encontrado en el catalogo")
                resolved.items.append(ri)
                continue

        # Precio: prioridad
        # 1. Lo que dijo el vendedor explicitamente
        # 2. Motor de precios (tabla oficial segun categoria del cliente)
        # 3. Catalogo Siigo crudo
        if raw_item.precio_unitario is not None:
            ri.precio_unitario = raw_item.precio_unitario
        else:
            sug = sugerir_precio(ri.elegido.code, customer_id)
            if sug.fuente != "desconocido" and sug.precio_pre_iva > 0:
                ri.precio_unitario = sug.precio_pre_iva
            elif ri.elegido.price_default is not None:
                # Fallback: catalogo crudo
                if ri.elegido.tax_included and ri.elegido.iva_percentage:
                    ri.precio_unitario = ri.elegido.price_default / (1 + ri.elegido.iva_percentage / 100)
                else:
                    ri.precio_unitario = ri.elegido.price_default
            else:
                problemas.append(f"Item '{raw_item.descripcion}': sin precio definido. Hay que indicar uno a mano.")

        resolved.items.append(ri)

    resolved.necesita_input_humano = problemas
    return resolved


def format_summary(rp: ResolvedPedido) -> str:
    """Resumen visual para mostrar en el chat antes de confirmar.
    Usa formato Markdown de Telegram (negrita, monospace) sin emojis.
    """
    from skiimo.pricing.engine import get_categoria_cliente, obtener_pronto_pago

    lines: list[str] = []

    # Encabezado modo prueba (sutil)
    if rp.test_mode:
        lines.append("_modo prueba — factura tradicional_")
        lines.append("")

    # Cliente: en modo test, mostrar el cliente REAL (al que va dirigido el pedido)
    # y aparte indicar que la factura se manda al cliente test.
    cli_para_mostrar = rp.cliente_real_para_precios if rp.cliente_real_para_precios else rp.cliente_elegido
    if cli_para_mostrar:
        cat = get_categoria_cliente(cli_para_mostrar.id)
        cat_label = {"DETAL": "Detal", "MAYORISTA": "Mayorista", "DISTRIBUIDOR": "Distribuidor"}.get(cat, cat)
        lines.append(f"*Cliente*")
        lines.append(f"{cli_para_mostrar.name}")
        lines.append(f"NIT {cli_para_mostrar.identification}  ·  _{cat_label}_")
        # En modo test, avisar que se envia al cliente test
        if rp.test_mode and rp.cliente_real_para_precios:
            lines.append(f"_(factura enviada a {rp.cliente_elegido.name} en modo prueba)_")
    else:
        lines.append("*Cliente:* pendiente")

    lines.append("")
    lines.append("*Productos*")

    # Items con bullets y monospace
    subtotal = 0.0
    for item in rp.items:
        if item.elegido:
            sub = (item.total_estimado or 0.0)
            subtotal += sub
            precio = item.precio_unitario or 0
            qty_str = f"{int(item.cantidad)}" if item.cantidad == int(item.cantidad) else f"{item.cantidad:g}"
            # Linea 1: nombre y cantidad
            lines.append(f"• {item.elegido.name}")
            # Linea 2: detalles tipo recibo
            lines.append(f"  `{qty_str} x ${precio:>10,.0f}  =  ${sub:>10,.0f}`")
        else:
            lines.append(f"• _no encontrado_: {item.raw.descripcion}")

    lines.append("")

    # Bloque de totales
    dto_pct = rp.raw.descuento_pct or 0.0
    if dto_pct > 0:
        dto_val = subtotal * dto_pct / 100
        final = subtotal - dto_val
        motivo = f" — {rp.raw.descuento_motivo}" if rp.raw.descuento_motivo else ""
        lines.append(f"Subtotal      `${subtotal:>10,.0f}`")
        lines.append(f"Descuento {dto_pct:.0f}%{motivo}")
        lines.append(f"              `-${dto_val:>9,.0f}`")
        lines.append(f"*TOTAL*       `${final:>10,.0f}`")
    else:
        lines.append(f"*TOTAL con IVA*  `${subtotal:>10,.0f}`")

    # Metadata opcional
    meta_lines = []
    if rp.raw.fecha_entrega:
        meta_lines.append(f"📅 Entrega: {rp.raw.fecha_entrega}")
    if rp.raw.forma_pago:
        meta_lines.append(f"💵 Pago: {rp.raw.forma_pago}")
    if rp.raw.observaciones:
        meta_lines.append(f"📝 {rp.raw.observaciones}")
    if meta_lines:
        lines.append("")
        lines.extend(meta_lines)

    # Pronto pago disponible (solo si no hay descuento puntual ya aplicado)
    # Se calcula sobre el cliente real (no el test)
    if cli_para_mostrar and dto_pct == 0:
        escalas = obtener_pronto_pago(cli_para_mostrar.id)
        if escalas:
            lines.append("")
            lines.append("*Pronto pago disponible:*")
            for e in escalas:
                dto = subtotal * e["descuento_pct"] / 100
                final = subtotal - dto
                lines.append(f"• Paga en {e['dias_max']} días → -{e['descuento_pct']:.0f}%  =  `${final:,.0f}`")

    # Pendientes (errores)
    if rp.necesita_input_humano:
        lines.append("")
        lines.append("⚠️ *Atención:*")
        for p in rp.necesita_input_humano:
            lines.append(f"• {p}")

    return "\n".join(lines)


if __name__ == "__main__":
    from skiimo.llm.gemini import extract_pedido

    matcher = Matcher()
    msgs = [
        "Doña Marta me pidio 5 bolsas chicle 6L y 3 perlas mango bicheñas grandes en efectivo",
        "Para Tienda La 35: 10 BOLSAS CHICLE, 5 BOLSAS OJO DE DIABLO, 4 PERLAS FRESA 1200 GR",
        "NIT 32160242 necesita 2 sachets miami y 3 perlas explosivas mango bich 1200",
    ]
    for m in msgs:
        print("=" * 70)
        print(f"MENSAJE: {m}")
        print("-" * 70)
        p = extract_pedido(m)
        rp = resolve_pedido(p, matcher)
        print(format_summary(rp))
        print()
