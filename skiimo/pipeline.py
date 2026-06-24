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
        # Auto-match estricto: score >= 92 + cobertura de tokens.
        # Si el vendedor dijo "Hernan Marin" (2 palabras), el cliente debe contener AMBAS
        # palabras para considerarlo match seguro. Esto evita que "Hernan Marin" matchee
        # con "HERNAN ." (que solo contiene "hernan").
        if candidatos:
            best = candidatos[0]
            # Cobertura de tokens: cada palabra del vendedor debe coincidir con algun
            # token del nombre del candidato (substring match). Asi "Hernan Marin"
            # NO matchea "HERNAN ." (no hay token con "marin"), pero "arqui distribu"
            # SI matchea "ARQUI DISTRIBUCIONES" (distribu es prefijo de distribuciones).
            import re as _re
            def _tokens(s: str) -> list[str]:
                s_ = _re.sub(r"[^a-zA-Z\sñ]", " ", (s or "").lower())
                return [t for t in s_.split() if len(t) >= 3]
            q_tokens = _tokens(pedido.cliente_nombre)
            name_tokens = _tokens(best.name)
            def _cubre(qt: str, nts: list[str]) -> bool:
                # qt cubre si es substring de algun nt o viceversa (prefijo).
                return any(qt in nt or nt in qt for nt in nts)
            cobertura_ok = (not q_tokens) or all(_cubre(qt, name_tokens) for qt in q_tokens)
            # Threshold: con cobertura completa, basta score 80 (la cobertura es el filtro real).
            # Sin tokens validos, exigir 92.
            min_score = 80 if q_tokens else 92

            # Ambiguedad: si hay 2+ candidatos con score >= 95 y todos cubren los tokens,
            # NO auto-match. El vendedor elige cual es el correcto (puede que sea el 2do
            # aunque el 1ro tenga mas historial).
            ambiguos = [c for c in candidatos
                        if c.score >= 95 and
                        all(_cubre(qt, _tokens(c.name)) for qt in q_tokens)]
            hay_ambiguedad = len(ambiguos) >= 2

            if best.score >= min_score and cobertura_ok and not hay_ambiguedad:
                cliente_real_para_precios = best
                resolved.cliente_elegido = best
            elif hay_ambiguedad:
                problemas.append(
                    f"Hay varios clientes que coinciden con '{pedido.cliente_nombre}'. "
                    f"Elegí el correcto:"
                )
            else:
                # No auto-match: vendedor elige entre los candidatos via boton
                problemas.append(
                    f"Cliente '{pedido.cliente_nombre}' no es exacto. "
                    f"Elegí uno de los candidatos o crealo si es nuevo."
                )
        else:
            problemas.append(f"Cliente '{pedido.cliente_nombre}' no encontrado")
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


def format_summary(rp: ResolvedPedido, dtype: str | None = None) -> str:
    """Resumen visual para mostrar en el chat antes de confirmar.
    Usa formato Markdown de Telegram (negrita, monospace) sin emojis.

    dtype controla como se muestra el IVA (el TOTAL es el mismo siempre):
      - None  -> resumen inicial: solo el total, sin desglose de IVA.
      - 'elec'-> FE: IVA discriminado (Base + IVA), porque se reporta a la DIAN.
      - 'trad'-> Tradicional: IVA incluido, NO se discrimina (se lo quedan).
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

    # Items con formato limpio: nombre en bold, qty x precio = total c/IVA destacado,
    # y debajo en gris/italic el desglose de IVA.
    subtotal_sin_iva = 0.0
    iva_total = 0.0
    for item in rp.items:
        if item.elegido:
            precio = item.precio_unitario or 0.0  # SIN IVA
            qty = item.cantidad
            sub_item = precio * qty
            iva_pct = item.elegido.iva_percentage or 19.0
            iva_item = round(sub_item * (iva_pct / 100.0), 2)
            con_iva_item = round(sub_item + iva_item, 2)
            subtotal_sin_iva += sub_item
            iva_total += iva_item
            qty_str = f"{int(qty)}" if qty == int(qty) else f"{qty:g}"
            # Nombre del producto en bold
            lines.append(f"🥤 *{item.elegido.name}*")
            # Linea principal: cantidad x precio (con IVA incl.) → total (destacado)
            precio_unit_con_iva = round(precio * (1 + iva_pct / 100.0), 2)
            lines.append(
                f"   {qty_str} × ${precio_unit_con_iva:,.0f}  →  *${con_iva_item:,.0f}*"
            )
            # Desglose Base+IVA SOLO en FE (en tradicional el IVA no se discrimina)
            if dtype == "elec":
                lines.append(
                    f"   _Base ${sub_item:,.0f} + IVA {iva_pct:.0f}% (${iva_item:,.0f})_"
                )
            lines.append("")  # espacio entre items
        else:
            lines.append(f"❌ _no encontrado:_ {item.raw.descripcion}")
            lines.append("")

    # Bloque de totales: tabla limpia con bold en TOTAL.
    # El TOTAL es el mismo en FE y tradicional; solo FE muestra Base/IVA.
    total_con_iva = subtotal_sin_iva + iva_total
    dto_pct = rp.raw.descuento_pct or 0.0
    lines.append("━━━━━━━━━━━━━━━━━━")
    if dto_pct > 0:
        dto_val_sin_iva = subtotal_sin_iva * dto_pct / 100
        iva_dto = iva_total * dto_pct / 100
        descuento_total = dto_val_sin_iva + iva_dto
        final = total_con_iva - descuento_total
        motivo = rp.raw.descuento_motivo or ""
        if dtype == "elec":
            lines.append(f"Subtotal sin IVA:  ${subtotal_sin_iva:,.0f}")
            lines.append(f"IVA:               ${iva_total:,.0f}")
        lines.append(f"Descuento {dto_pct:.0f}%:     −${descuento_total:,.0f}")
        if motivo:
            lines.append(f"_({motivo})_")
        lines.append(f"*TOTAL: ${final:,.0f}*")
    else:
        if dtype == "elec":
            lines.append(f"Subtotal sin IVA:  ${subtotal_sin_iva:,.0f}")
            lines.append(f"IVA:               ${iva_total:,.0f}")
        lines.append(f"*TOTAL: ${total_con_iva:,.0f}*")
    if dtype == "trad":
        lines.append("_(IVA no discriminado)_")

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
