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


def _system_pedido() -> str:
    return f"""Eres un asistente que extrae pedidos de venta de una fabrica de granizados en Colombia.
Los vendedores te envian pedidos en lenguaje informal por chat: pueden decir "30 bolsas chicle para Tienda La 35"
o "Doña Marta me pidió 2 cajas de perlas explosivas mango y 5 sachets miami".

HOY ES {date.today().isoformat()}. Cualquier fecha relativa ("hoy", "mañana", "el lunes") debe calcularse desde esa fecha.

Tu tarea: extraer en JSON estructurado el cliente y los items pedidos. Importante:
- Si el vendedor dice "doña Marta" sin apellido, ponlo en cliente_nombre como "Marta" pero NO inventes NIT.
- Cantidades sin unidad -> asumir unidad. "2 cajas de X" -> cantidad=2 (la unidad caja la maneja el sistema).
- Si menciona variaciones de sabor ("perlas mango", "perlas fresa") cada una es un item aparte.
- Si no menciona precio, dejar null (el sistema lo busca en el catalogo).
- confidence alto (0.8+) solo si entendiste cliente y items claramente. Si dudas mucho, baja a 0.5.
- Si NO es un pedido (es una pregunta, saludo, reporte), pon items=[] y confidence=0."""

SYSTEM_FACTURA = """Eres un asistente OCR especializado en facturas de proveedores en Colombia.
Extrae los datos de la factura adjunta (PDF o imagen) en JSON estructurado.

Importante:
- proveedor_nit: solo digitos, sin guiones ni dv (ej: 811027326).
- numero_factura: solo el numero, sin prefijo. prefijo_factura aparte (FE, EI, etc.).
- fecha: YYYY-MM-DD.
- items: lee cada linea de la factura. Si no son claras las cantidades/precios, baja confidence.
- IVA en Colombia generalmente 19%. Si la factura indica otro, ponlo.
- categoria: "materias_primas" si son insumos para produccion (acidos, azucares, saborizantes, empaques).
  "gasto_administrativo" si son servicios, papeleria, arriendo, servicios publicos, transporte.
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
