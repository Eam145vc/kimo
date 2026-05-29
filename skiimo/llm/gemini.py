"""Wrapper Gemini para extraccion estructurada y transcripcion de audio.

Soporta:
  - extract_pedido(texto)               -> Pedido
  - extract_pedido_from_audio(bytes)    -> Pedido
  - extract_factura_proveedor(bytes)    -> FacturaProveedor (imagen o PDF)
  - transcribe_audio(bytes)             -> str
"""
from __future__ import annotations

from datetime import date

from google import genai
from google.genai import types as genai_types

from skiimo.config import GEMINI_API_KEY, GEMINI_MODEL
from skiimo.llm.schemas import ComprobantePago, FacturaProveedor, Pedido


_client: genai.Client | None = None


def get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


def _catalogo_para_prompt() -> str:
    """Devuelve el catalogo activo agrupado por categoria, compacto para system prompt.
    Solo incluye los grupos relevantes para pedidos (bolsas, sachets, perlas, gelatinas, sales, siropes)."""
    try:
        from skiimo.db.schema import get_conn
    except Exception:
        return ""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT code, name, COALESCE(account_group_name, '(otro)') AS grupo
               FROM siigo_products
               WHERE (active = 1 OR active IS NULL)
                 AND COALESCE(account_group_name, '') NOT IN ('Materias Primas', 'MAQUINA', 'REPUESTOS MAQUINAS', 'Servicios', 'Productos')
               ORDER BY account_group_name, code"""
        ).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    if not rows:
        return ""
    grupos: dict[str, list[str]] = {}
    for r in rows:
        g = r["grupo"]
        nombre = r["name"]
        # Quitar prefijos del grupo del nombre para compactar
        for prefix in (
            "BOLSA 6 LT ", "BOLSA 6L ",
            "BOLSA SACHET 08 OZ ",
            "PERLAS EXPLOSIVAS ",
            "GELATINA ",
            "SAL PARA MICHELAR ", "SAL MICHELAR ",
            "SIROPE ",
        ):
            if nombre.upper().startswith(prefix):
                nombre = nombre[len(prefix):]
                break
        grupos.setdefault(g, []).append(f"{r['code']} {nombre}")
    lines = ["CATALOGO ACTIVO (codigo + sabor por grupo):"]
    for grupo, items in grupos.items():
        lines.append(f"\n[{grupo}]")
        # Separador · para compactar
        lines.append("  " + " · ".join(items))
    return "\n".join(lines)


def _system_pedido() -> str:
    catalogo = _catalogo_para_prompt()
    return f"""Eres un asistente que extrae pedidos de venta de Esskimo Cocktails (fabrica de granizados en Colombia).
Los vendedores te envian pedidos en lenguaje informal por chat. Ejemplos:
  - "3 coco 2 bombon para Hernan Marin"
  - "5 sachets miami sin licor"
  - "perlas mango grandes para la tienda La 35"

HOY ES {date.today().isoformat()}.

REGLAS DE NEGOCIO PARA ASIGNAR EL CODIGO DEL PRODUCTO (codigo del catalogo):
1. POR DEFECTO ES BOLSA 6L CON LICOR. Solo cambia si dicen explicitamente:
   - "sachet" / "sachets" -> SACHETS 08 OZ
   - "perlas" -> PERLAS EXPLOSIVAS (pedir aclaracion de tamaño si dudoso)
   - "gelatina" -> GELATINAS
   - "sin licor" -> version sin licor del mismo sabor
2. "bombon" SOLO significa bombon regular. NO asumir "bombon manzana verde" salvo que lo digan literal.
3. "coco" sin mas contexto -> A1O (Bolsa 6L Coco Loco con licor) por regla 1.
   "coco sin licor" -> A2X. "sachet coco" -> A3M.
4. Si el sabor mencionado NO existe en el catalogo, deja codigo=null y pon descripcion con lo que dijo el vendedor.
5. Cantidades sin unidad -> unidades. "2 cajas de X" -> cantidad=2 (caja la maneja el sistema).
6. Cada sabor es un item aparte. "3 coco 2 bombon" -> 2 items: (A1O, 3) y (A2AO, 2).

{catalogo}

OTRAS REGLAS:
- Si el vendedor dice "doña Marta" sin apellido, cliente_nombre="Marta", cliente_nit=null.
- Si no menciona precio, precio_unitario=null (el sistema lo calcula segun categoria del cliente).
- confidence 0.9+ si entendiste cliente y TODOS los items con codigo asignado.
- confidence 0.6 si algun item quedo sin codigo (sabor desconocido o ambiguo).
- Si NO es un pedido (saludo, pregunta, reporte): items=[], confidence=0."""

SYSTEM_FACTURA = """Eres un asistente OCR especializado en facturas de proveedores en Colombia.
Extrae los datos de la factura adjunta (PDF o imagen) en JSON estructurado.

Importante:
- proveedor_nit: solo digitos, sin guiones ni dv (ej: 811027326).
- numero_factura: solo el numero, sin prefijo. prefijo_factura aparte (FE, EI, etc.).
- fecha: YYYY-MM-DD.
- items: lee cada linea de la factura. Si no son claras las cantidades/precios, baja confidence.
- NORMALIZACION DE UNIDADES (OBLIGATORIA, estandar Siigo):
  * Pesos/solidos -> SIEMPRE convertir a GRAMOS. unidad="g".
      kg  -> g  (x1000)   |   t/ton -> g (x1.000.000)
      mg  -> g  (/1000)   |   lb    -> g (x453.592)
      oz  -> g  (x28.3495)|   arroba -> g (x12500)
  * Volumenes/liquidos -> SIEMPRE convertir a MILILITROS. unidad="ml".
      L/lt/litro -> ml (x1000)   |   cl -> ml (x10)
      m3         -> ml (x1.000.000)
      gal (US)   -> ml (x3785.41)|   oz fl -> ml (x29.5735)
  * Presentaciones empaquetadas con peso/volumen explicito ("saco de 25 kg", "bolsa de 50 lb",
    "garrafa de 20 L", "caneca de 5 gal", "frasco de 500 ml"): SIEMPRE expandir al contenido
    total en g o ml. NO dejar en "und". El precio por presentacion se reparte sobre el contenido.
      Ej: 3 sacos de 25 kg a $100.000/saco
          -> cantidad = 3 * 25000 = 75000, unidad="g", precio_unitario = 100000 / 25000 = 4 ($/g)
          (subtotal $300.000 intacto)
      Ej: 2 garrafas de 20 L a $80.000/garrafa
          -> cantidad = 2 * 20000 = 40000, unidad="ml", precio_unitario = 80000 / 20000 = 4 ($/ml)
  * Solo dejar unidad="und" cuando NO hay peso ni volumen asociado: servicios, horas, cajas
    de items contables sin gramaje (ej. "1 caja de 24 unidades de servilletas"), rollos, fletes,
    arriendos, papeleria suelta, mantenimientos, asesorias.
  * Guarda SIEMPRE cantidad_original y unidad_original con los valores originales de la factura
    (ej: cantidad_original=3, unidad_original="saco 25 kg").
  * RE-ESCALA precio_unitario para preservar el subtotal del item:
      precio_unitario_normalizado = subtotal_item / cantidad_normalizada
    Equivalente a: precio_unitario_original * (cantidad_original / cantidad_normalizada).
    Ej: 5 kg a $10.000/kg -> cantidad=5000, unidad="g", precio_unitario=10 (subtotal $50.000 intacto).
    Ej: 2 L a $8.000/L    -> cantidad=2000, unidad="ml", precio_unitario=4  (subtotal $16.000 intacto).
- IVA en Colombia generalmente 19%. Si la factura indica otro, ponlo.
- categoria:
  * "materias_primas" si son insumos para produccion (acidos, azucares, saborizantes, empaques, etc.).
  * "gasto_administrativo" si son servicios o gastos generales DE EMPRESAS con factura electronica DIAN
    (arriendo de empresa, servicios publicos, papeleria de empresa, transporte de empresa).
  * "documento_soporte" si el proveedor es una PERSONA NATURAL sin factura electronica DIAN:
    - No tiene NIT empresarial (cedula de ciudadania o RUT de persona natural)
    - Es un freelancer, contratista, consultor independiente
    - Honorarios profesionales (contadores, abogados, asesores)
    - Servicios informales (taxi, mensajero, mantenimiento)
    - Cuando no hay numero de factura DIAN visible (CUFE/CUDE)
    - Reembolsos de gastos personales
    Si dudas entre "gasto_administrativo" y "documento_soporte", elegi documento_soporte solo si
    es claramente persona natural o servicio informal.
- confidence alto (0.85+) solo si todos los campos numericos son claros y suman bien."""


def extract_pedido(texto: str) -> Pedido:
    client = get_client()
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=texto,
        config=genai_types.GenerateContentConfig(
            system_instruction=_system_pedido(),
            response_mime_type="application/json",
            response_schema=Pedido,
            temperature=0.1,
        ),
    )
    try:
        from skiimo.uso_ia import registrar_uso
        registrar_uso(getattr(response,"usage_metadata",None), operacion="pedido", modelo=GEMINI_MODEL)
    except Exception: pass
    parsed = response.parsed
    if isinstance(parsed, Pedido):
        return parsed
    # Fallback: validar JSON crudo
    import json
    raw = response.text or "{}"
    return Pedido.model_validate(json.loads(raw))


def extract_pedido_from_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> Pedido:
    """Transcribe + extrae en una sola llamada (Gemini es multimodal nativo)."""
    client = get_client()
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            "Escucha este audio de un vendedor y extrae el pedido en JSON.",
            genai_types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
        ],
        config=genai_types.GenerateContentConfig(
            system_instruction=_system_pedido(),
            response_mime_type="application/json",
            response_schema=Pedido,
            temperature=0.1,
        ),
    )
    try:
        from skiimo.uso_ia import registrar_uso
        registrar_uso(getattr(response,"usage_metadata",None), operacion="pedido", modelo=GEMINI_MODEL)
    except Exception: pass
    parsed = response.parsed
    if isinstance(parsed, Pedido):
        return parsed
    import json
    return Pedido.model_validate(json.loads(response.text or "{}"))


def transcribe_audio(audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
    client = get_client()
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            "Transcribe este audio palabra por palabra en espanol colombiano.",
            genai_types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
        ],
        config=genai_types.GenerateContentConfig(temperature=0.0),
    )
    try:
        from skiimo.uso_ia import registrar_uso
        registrar_uso(getattr(response,"usage_metadata",None), operacion="audio", modelo=GEMINI_MODEL)
    except Exception: pass
    return response.text or ""


SYSTEM_COMPROBANTE = """Eres un asistente OCR para comprobantes de pago colombianos.
Recibes una imagen que puede ser:
- Pantallazo de Nequi (color rojo/rosado)
- Pantallazo de Daviplata (color rojo)
- Pantallazo de Bancolombia (color amarillo/azul)
- Pantallazo de Davivienda (color rojo)
- Recibo bancario en papel
- Voucher de tarjeta debito/credito

Extrae los datos en JSON estructurado. Reglas:
- monto: SOLO el numero, sin '$', sin puntos de miles, con decimales si los tiene.
  Ej: "$1.250.000,00" -> 1250000.00
- metodo_pago: identifica por colores y logos. Si dice 'NEQUI' arriba -> 'nequi'.
  Si dice 'BANCOLOMBIA' o 'BANCA' con logo amarillo -> 'bancolombia'.
  Si es transferencia bancaria generica -> 'banco_otro'.
- fecha_pago: solo si la ves clara en el comprobante.
- es_comprobante_valido: false si la imagen es otra cosa (un meme, una foto cualquiera).
  true si claramente es un pago aprobado/exitoso.
- confidence: alto (0.9+) si el monto y metodo estan clarisimos. Bajo si dudas."""


def extract_comprobante_pago(
    data: bytes,
    mime_type: str = "image/jpeg",
) -> ComprobantePago:
    """OCR de un comprobante de pago (Nequi/Daviplata/Bancolombia/etc)."""
    client = get_client()
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            "Extrae los datos de este comprobante de pago.",
            genai_types.Part.from_bytes(data=data, mime_type=mime_type),
        ],
        config=genai_types.GenerateContentConfig(
            system_instruction=SYSTEM_COMPROBANTE,
            response_mime_type="application/json",
            response_schema=ComprobantePago,
            temperature=0.0,
        ),
    )
    try:
        from skiimo.uso_ia import registrar_uso
        registrar_uso(getattr(response,"usage_metadata",None), operacion="factura", modelo=GEMINI_MODEL)
    except Exception: pass
    parsed = response.parsed
    if isinstance(parsed, ComprobantePago):
        return parsed
    import json
    return ComprobantePago.model_validate(json.loads(response.text or "{}"))


def extract_factura_proveedor(
    data: bytes,
    mime_type: str = "application/pdf",
) -> FacturaProveedor:
    """data puede ser PDF o imagen (jpg, png, webp)."""
    client = get_client()
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            "Extrae los datos de esta factura de proveedor.",
            genai_types.Part.from_bytes(data=data, mime_type=mime_type),
        ],
        config=genai_types.GenerateContentConfig(
            system_instruction=SYSTEM_FACTURA,
            response_mime_type="application/json",
            response_schema=FacturaProveedor,
            temperature=0.0,
        ),
    )
    try:
        from skiimo.uso_ia import registrar_uso
        registrar_uso(getattr(response,"usage_metadata",None), operacion="factura", modelo=GEMINI_MODEL)
    except Exception: pass
    parsed = response.parsed
    if isinstance(parsed, FacturaProveedor):
        return parsed
    import json
    return FacturaProveedor.model_validate(json.loads(response.text or "{}"))


if __name__ == "__main__":
    # Test rapido de extraccion de pedido
    test_msg = "Hola, necesito 10 bolsas de chicle 6L y 5 sachets miami para la Tienda Don Pepe, entregar mañana en efectivo"
    print(f"Mensaje: {test_msg}\n")
    p = extract_pedido(test_msg)
    print("Pedido extraido:")
    print(p.model_dump_json(indent=2))
