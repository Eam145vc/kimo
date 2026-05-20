"""Agente conversacional unificado.

Decide en cada turno que hacer:
  - Si el mensaje es un pedido -> Gemini llama tool `registrar_pedido` con args estructurados.
  - Si el mensaje es pregunta/consulta -> Gemini llama tools de reporte.
  - Si es saludo / off-topic -> responde texto.

Mantiene historial corto por chat para coherencia conversacional.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from google import genai
from google.genai import types as genai_types

from skiimo.config import GEMINI_API_KEY, GEMINI_MODEL
from skiimo.llm.schemas import Pedido, PedidoItem
from skiimo.llm.tools import TOOL_DECLARATIONS, TOOLS_MAP


_client: genai.Client | None = None


def get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


@dataclass(slots=True)
class AgentReply:
    """Lo que devuelve el agente al handler de Telegram."""
    kind: str                      # 'pedido' | 'texto' | 'error'
    texto: str | None = None       # respuesta para mostrar al usuario
    pedido: Pedido | None = None   # si kind=='pedido'
    tools_used: list[str] = field(default_factory=list)
    last_tool_result: dict | None = None  # resultado de la ultima tool ejecutada


def _system_instruction(user_role: str = "vendedor") -> str:
    today = date.today().isoformat()
    return f"""Eres Kimo, el asistente de gestion de la fabrica de granizados Skiimo Cocktails.
Hoy es {today}. Hablas en espanol colombiano informal.

El usuario actual tiene rol: {user_role}.

TU MISION: en cada mensaje, decidir el camino:

1) **PEDIDO DE VENTA**: si el usuario describe items para vender a un cliente
   (ej: "10 bolsas chicle para Tienda La 35", "Doña Marta pidio 5 perlas mango",
   "necesito 1 P23 a $100 en efectivo").
   Para esto LLAMA a la funcion `registrar_pedido`.
   Si el vendedor menciona un descuento puntual (cumpleanos, atencion, promo)
   capturalo en descuento_pct y descuento_motivo. Ej: "5 bolsas para Hugo con 10%
   por cumpleanos" -> descuento_pct=10, descuento_motivo="cumpleanos".

2) **REPORTE O CONSULTA**: si el usuario pregunta por ventas, gastos, clientes,
   productos, facturas, balance, top, comparativos. LLAMA las funciones de consulta
   (consultar_ventas, consultar_gastos, top_clientes, top_productos, ultima_venta,
   resumen_dia, buscar_cliente, buscar_producto, facturas_pendientes_cobro).
   Despues responde en lenguaje natural y formato chat. Usa $ y comas en montos.

3) **CONVERSACION GENERAL**: saludo, agradecimiento, off-topic. Responde breve.

REGLAS:
- Si dice "ultima venta" / "ultima factura" -> ultima_venta()
- Si dice "como vamos hoy" / "resumen" -> resumen_dia()
- Si dice "cuanto vendi esta semana" -> consultar_ventas(periodo='esta_semana')
- Si dice "quien me debe" / "pendientes" -> facturas_pendientes_cobro()
- Si dice "cuanto cuesta X" / "precio de X" -> consultar_precio()
- Si dice "cambiar precio de X" / "X ahora cuesta Y" / "subir precio X a Y" -> cambiar_precio()
  (Solo admin. Si el rol no es admin, decir 'solo el admin puede cambiar precios').
- Si dice "X es mayorista/distribuidor" / "X pasa a Y" -> cambiar_categoria_cliente() (solo admin).
- Si dice "que tengo por pagar" -> facturas_proveedor_pendientes()
- Si dice "que esta vencido" -> vencimientos_proximos()
- Si dice "repite pedido de X" / "lo de siempre a X" -> repetir_pedido_cliente()
  Despues muestra los items y pregunta si confirma para llamar registrar_pedido.
- Si dice "estado de cuenta de X" / "cuanto me debe X" / "cartera de Y" -> estado_cuenta_cliente()
- Si dice "stock de X" / "cuantas X tengo" / "inventario de Y" -> consultar_stock()
- Si dice "Hugo me pago X" / "recibi tanto de la factura Y" -> analizar_pago_factura()
- Si dice "pague a Arqui" / "le transferi a proveedor X" / "salio pago para Y" -> analizar_pago_a_proveedor()
- Si dice "anula la factura X" / "cancela la factura Y" / "borra el pedido Z" -> proponer_anular_factura()
  (Solo admin. NO ejecuta nada directamente — devuelve botones de confirmacion).
- NUNCA inventes datos.
- Responde SIEMPRE en espanol colombiano informal."""


# Declaracion de registrar_pedido para Gemini
_REGISTRAR_PEDIDO_DECL = {
    "name": "registrar_pedido",
    "description": (
        "Registra un pedido de venta extraido del mensaje del vendedor. "
        "Llamar SOLO cuando el usuario describe items para vender a un cliente. "
        "El sistema externo procesa el pedido y muestra confirmacion al vendedor."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "cliente_nombre": {
                "type": "string",
                "description": "Nombre del cliente como lo dijo el vendedor. Null si no se menciono.",
            },
            "cliente_nit": {
                "type": "string",
                "description": "NIT/cedula solo si se menciono explicitamente.",
            },
            "items": {
                "type": "array",
                "description": "Lista de items pedidos.",
                "items": {
                    "type": "object",
                    "properties": {
                        "descripcion": {"type": "string", "description": "Producto tal como lo dijo el vendedor"},
                        "cantidad": {"type": "number", "description": "Cantidad. Default 1."},
                        "precio_unitario": {"type": "number", "description": "Precio sin IVA si se menciono."},
                    },
                    "required": ["descripcion", "cantidad"],
                },
            },
            "fecha_entrega": {
                "type": "string",
                "description": "Fecha entrega YYYY-MM-DD si se menciono.",
            },
            "forma_pago": {
                "type": "string",
                "description": "efectivo, credito, nequi, daviplata, tarjeta",
            },
            "descuento_pct": {
                "type": "number",
                "description": (
                    "Porcentaje de descuento PUNTUAL si el vendedor lo menciona "
                    "(ej: 'con 10% por cumpleanos', 'le doy 5%', 'descuento del 8%'). "
                    "Es ADICIONAL al precio de su categoria. Null si no se menciona. "
                    "Maximo 100."
                ),
            },
            "descuento_motivo": {
                "type": "string",
                "description": (
                    "Motivo del descuento puntual mencionado por el vendedor. "
                    "Ej: 'cumpleanos', 'cliente fiel', 'promocion', 'regalo'."
                ),
            },
            "observaciones": {
                "type": "string",
                "description": "Notas adicionales del vendedor.",
            },
            "confidence": {
                "type": "number",
                "description": "Confianza 0-1 de que la extraccion es correcta.",
            },
        },
        "required": ["items", "confidence"],
    },
}


def _to_genai_tools() -> list[genai_types.Tool]:
    """Construye Tool de Gemini con todas las declaraciones."""
    fn_decls = [
        genai_types.FunctionDeclaration(
            name=d["name"], description=d["description"], parameters=d["parameters"],
        )
        for d in [_REGISTRAR_PEDIDO_DECL] + TOOL_DECLARATIONS
    ]
    return [genai_types.Tool(function_declarations=fn_decls)]


# Historial por chat
_history: dict[int, list[genai_types.Content]] = {}
MAX_HISTORY = 12


def _push_history(chat_id: int, content: genai_types.Content) -> None:
    hist = _history.setdefault(chat_id, [])
    hist.append(content)
    if len(hist) > MAX_HISTORY:
        _history[chat_id] = hist[-MAX_HISTORY:]


def reset_history(chat_id: int) -> None:
    _history.pop(chat_id, None)


def _pedido_from_args(args: dict) -> Pedido | None:
    """Convierte args de tool registrar_pedido en Pedido pydantic."""
    try:
        items_raw = args.get("items") or []
        items = [
            PedidoItem(
                descripcion=str(it.get("descripcion", "")),
                cantidad=float(it.get("cantidad", 1)),
                precio_unitario=(
                    float(it["precio_unitario"]) if it.get("precio_unitario") is not None else None
                ),
            )
            for it in items_raw
        ]
        dto = args.get("descuento_pct")
        return Pedido(
            cliente_nombre=args.get("cliente_nombre"),
            cliente_nit=args.get("cliente_nit"),
            items=items,
            fecha_entrega=args.get("fecha_entrega"),
            forma_pago=args.get("forma_pago"),
            descuento_pct=float(dto) if dto is not None else None,
            descuento_motivo=args.get("descuento_motivo"),
            observaciones=args.get("observaciones"),
            confidence=float(args.get("confidence", 0.5)),
        )
    except Exception:
        return None


def process_message(
    chat_id: int,
    text: str,
    *,
    user_role: str = "vendedor",
    media_bytes: bytes | None = None,
    media_mime: str | None = None,
) -> AgentReply:
    """Procesa un mensaje del usuario y devuelve la respuesta."""
    client = get_client()
    tools = _to_genai_tools()

    user_parts: list[genai_types.Part] = []
    if media_bytes:
        user_parts.append(genai_types.Part.from_bytes(data=media_bytes, mime_type=media_mime or "audio/ogg"))
    if text:
        user_parts.append(genai_types.Part.from_text(text=text))
    user_content = genai_types.Content(role="user", parts=user_parts)

    history = _history.get(chat_id, [])
    contents = list(history) + [user_content]

    config = genai_types.GenerateContentConfig(
        system_instruction=_system_instruction(user_role),
        tools=tools,
        temperature=0.2,
        automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True),
    )

    tools_used: list[str] = []
    last_tool_result: dict | None = None

    for _step in range(6):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL, contents=contents, config=config,
            )
        except Exception as e:
            return AgentReply(kind="error", texto=f"Error LLM: {e}")

        if not response.candidates:
            return AgentReply(kind="error", texto="Sin respuesta del modelo")

        cand = response.candidates[0]
        parts = (cand.content.parts if cand.content else []) or []
        fcs = [p.function_call for p in parts if getattr(p, "function_call", None)]

        # Interceptar registrar_pedido (no se ejecuta como tool, se devuelve al caller)
        for fc in fcs:
            if fc.name == "registrar_pedido":
                pedido = _pedido_from_args(dict(fc.args) if fc.args else {})
                if pedido:
                    _push_history(chat_id, user_content)
                    _push_history(chat_id, cand.content)
                    tools_used.append("registrar_pedido")
                    return AgentReply(kind="pedido", pedido=pedido, tools_used=tools_used)

        if fcs:
            # Otras tools -> ejecutar y continuar
            contents.append(cand.content)
            response_parts: list[genai_types.Part] = []
            for fc in fcs:
                tools_used.append(fc.name)
                fn = TOOLS_MAP.get(fc.name)
                if not fn:
                    out = {"error": f"tool desconocida: {fc.name}"}
                else:
                    try:
                        args = dict(fc.args) if fc.args else {}
                        out = fn(**args)
                    except Exception as e:
                        out = {"error": str(e)}
                last_tool_result = out if isinstance(out, dict) else {"value": out}
                response_parts.append(genai_types.Part.from_function_response(
                    name=fc.name, response={"result": out},
                ))
            contents.append(genai_types.Content(role="user", parts=response_parts))
            continue

        # Texto final
        text_out = ""
        for p in parts:
            if getattr(p, "text", None):
                text_out += p.text
        _push_history(chat_id, user_content)
        _push_history(chat_id, cand.content)
        return AgentReply(
            kind="texto",
            texto=text_out.strip() or "(sin respuesta)",
            tools_used=tools_used,
            last_tool_result=last_tool_result,
        )

    return AgentReply(kind="error", texto="El modelo se atasco en function calls")


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    test_chat = 999
    test_msgs = [
        "hola",
        "cual fue mi ultima venta?",
        "cuanto vendi este mes?",
        "10 P23 a $100 para entregar hoy en efectivo",
        "y los gastos del mes?",
        "top 3 productos mas vendidos",
        "buscame doña martinez",
    ]
    for m in test_msgs:
        print(f"\n>>> USER: {m}")
        r = process_message(test_chat, m, user_role="admin")
        print(f"<<< KIND: {r.kind}  TOOLS: {r.tools_used}")
        if r.kind == "pedido":
            print("    PEDIDO:")
            print("   ", r.pedido.model_dump_json(indent=2))
        else:
            print(f"    TEXTO: {r.texto}")
