"""Agente conversacional unificado.

Decide en cada turno que hacer:
  - Si el mensaje es un pedido -> Gemini llama tool `registrar_pedido` con args estructurados.
  - Si el mensaje es pregunta/consulta -> Gemini llama tools de reporte.
  - Si es saludo / off-topic -> responde texto.

Mantiene historial corto por chat para coherencia conversacional.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from google import genai
from google.genai import types as genai_types

from skiimo.config import GEMINI_API_KEY, GEMINI_MODEL
from skiimo.llm.schemas import Pedido, PedidoItem
from skiimo.llm.tools import TOOL_DECLARATIONS, TOOLS_MAP


log = logging.getLogger("skiimo.llm.agent")
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
    # Importar lazy para evitar ciclos
    from skiimo.llm.gemini import _catalogo_para_prompt
    catalogo = _catalogo_para_prompt()
    return f"""Eres Kimo, el asistente de gestion de la fabrica de granizados Esskimo Cocktails.
Hoy es {today}. Hablas en espanol colombiano informal.

El usuario actual tiene rol: {user_role}.

TU MISION: en cada mensaje, decidir el camino:

1) **PEDIDO DE VENTA**: si el usuario describe items para vender a un cliente
   (ej: "10 bolsas chicle para Tienda La 35", "Doña Marta pidio 5 perlas mango",
   "necesito 1 P23 a $100 en efectivo").
   Para esto LLAMA a la funcion `registrar_pedido`.

   REGLAS DE NEGOCIO PARA ASIGNAR EL CODIGO DE PRODUCTO (campo items[].codigo):
   * POR DEFECTO ES BOLSA 6L CON LICOR. Solo cambia si dicen explicitamente:
     - "sachet" / "sachets" -> SACHETS 08 OZ
     - "perlas" -> PERLAS EXPLOSIVAS
     - "gelatina" -> GELATINAS
     - "sin licor" -> version sin licor del mismo sabor
   * Para PERLAS sin tamaño explicito -> DEFAULT 1200 GR (el tamaño medio, el mas vendido).
     - "perlas coco" -> P2 (Coco 1200 GR)
     - "perlas cereza grandes" -> P12 (Cereza 3400 GR)
     - "perlas chicle pequeñas" -> P? con 350 GR
   * "bombon" solo significa bombon regular. NO asumir "bombon manzana verde" salvo que lo digan literal.
   * "coco" sin contexto -> A1O (Bolsa 6L Coco Loco con licor). "coco sin licor" -> A2X. "sachet coco" -> A3M.
   * Si el sabor mencionado NO existe en el catalogo, dejar codigo=null.
   * Usar el CATALOGO de abajo para buscar el codigo exacto.

   {catalogo}

   Si el vendedor menciona un descuento puntual (cumpleanos, atencion, promo)
   capturalo en descuento_pct y descuento_motivo. Ej: "5 bolsas para Hugo con 10%
   por cumpleanos" -> descuento_pct=10, descuento_motivo="cumpleanos".

2) **REPORTE O CONSULTA**: si el usuario pregunta por ventas, gastos, clientes,
   productos, facturas, balance, top, comparativos. LLAMA las funciones de consulta
   (consultar_ventas, consultar_gastos, top_clientes, top_productos, ultima_venta,
   resumen_dia, buscar_cliente, buscar_producto, facturas_pendientes_cobro).
   Despues responde en lenguaje natural y formato chat. Usa $ y comas en montos.

3) **CONVERSACION GENERAL**: saludo, agradecimiento, off-topic. Responde breve.

REGLA CRITICA: si el usuario ya te dio toda la info necesaria, LLAMA LA TOOL en lugar de
preguntarle de nuevo. Si en turnos anteriores faltaba un dato y el usuario lo aporta ahora,
USA EL CONTEXTO para completar la tool y llamarla. NO devuelvas mensajes vacios.

REGLAS:
- Si dice "ultima venta" / "ultima factura" -> ultima_venta()
- Si dice "como vamos hoy" / "resumen" -> resumen_dia()
- Si dice "cuanto vendi esta semana" -> consultar_ventas(periodo='esta_semana')
- Si dice "quien me debe" / "pendientes" -> facturas_pendientes_cobro()
- Si dice "cuanto cuesta X" / "precio de X" -> consultar_precio()
- Si dice "cambiar precio de X" / "X ahora cuesta Y" / "subir precio X a Y" -> cambiar_precio()
  (Solo admin. Si el rol no es admin, decir 'solo el admin puede cambiar precios').
- Si dice "X es mayorista/distribuidor" / "X pasa a Y" -> cambiar_categoria_cliente() (solo admin).
- Si dice "Hugo paga en 8 dias y le doy 10%" / "configura pronto pago de X" /
  "a Zuniga descuento 10% si paga antes de 8 dias" -> configurar_pronto_pago() (solo admin).
  Para quitar pronto pago: 'sacale el pronto pago a X' -> configurar_pronto_pago(X, 0, 0).
- Si dice "que tengo por pagar" -> facturas_proveedor_pendientes()
- Si dice "que esta vencido" -> vencimientos_proximos()
- Si dice "repite pedido de X" / "lo de siempre a X" -> repetir_pedido_cliente()
  Despues muestra los items y pregunta si confirma para llamar registrar_pedido.
- Si dice "estado de cuenta de X" / "cuanto me debe X" / "cartera de Y" -> estado_cuenta_cliente()
- Si dice "stock de X" / "cuantas X tengo" / "inventario de Y" -> consultar_stock()
- Si dice "Hugo me pago X" / "recibi tanto de la factura Y" -> analizar_pago_factura()
- Si dice "pague a Arqui" / "le transferi a proveedor X" / "salio pago para Y" -> analizar_pago_a_proveedor()
- Si dice "anula la factura FV-1-XXXX" / "cancela la factura Y por nombre exacto" -> proponer_anular_factura()
- Si dice "anula la ultima de Hugo" / "cancela la penultima de Diego" / "tira el ultimo pedido de X"
  -> proponer_anular_ultima_factura_cliente(cliente_query=X, n=1|2|3)
  (Solo admin. NO ejecuta nada directamente — devuelve botones de confirmacion).
- Si dice "agrega como admin/vendedor al chat X" / "registra a Y" / "da de alta a Z" -> agregar_usuario()
- Si dice "lista de usuarios" / "quien tiene acceso" -> listar_usuarios()
- Si dice "sacale acceso a X" / "desactiva al chat Y" -> desactivar_usuario()
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
                        "codigo": {
                            "type": "string",
                            "description": (
                                "Codigo del producto del catalogo (ej: A1AO, A3U, P2). "
                                "Aplicar reglas: default bolsa 6L con licor. "
                                "Si el sabor no existe en el catalogo, omitir o null."
                            ),
                        },
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


def _fallback_text_from_tool(tool_name: str, result: dict) -> str:
    """Cuando Gemini ejecuta una tool pero no genera texto al final, armar uno basico
    con la informacion del resultado. Mejor un texto plano que '(sin respuesta)'.
    """
    if not isinstance(result, dict):
        return ""

    # Tools de gestion de usuarios
    if tool_name == "agregar_usuario":
        if result.get("error"):
            return f"❌ {result['error']}"
        if result.get("ok"):
            accion = result.get("accion", "registrado")
            return (
                f"✅ Usuario {accion}:\n"
                f"chat_id: {result.get('chat_id')}\n"
                f"nombre: {result.get('nombre')}\n"
                f"rol: {result.get('rol')}\n"
                f"siigo_seller_id: {result.get('siigo_seller_id')}"
            )

    if tool_name == "listar_usuarios":
        usuarios = result.get("usuarios", [])
        if not usuarios:
            return "No hay usuarios registrados."
        lines = ["Usuarios registrados:"]
        for u in usuarios:
            estado = "✅" if u.get("activo") else "❌"
            lines.append(f"{estado} {u.get('nombre')} - chat {u.get('chat_id')} - {u.get('rol')}")
        return "\n".join(lines)

    if tool_name == "desactivar_usuario":
        if result.get("error"):
            return f"❌ {result['error']}"
        return f"✅ Usuario {result.get('nombre')} (chat {result.get('chat_id')}) desactivado."

    # Tool de ultima venta
    if tool_name == "ultima_venta":
        if not result.get("encontrado"):
            return "No encontre ventas recientes."
        return (
            f"Ultima venta: {result.get('factura')}\n"
            f"Cliente: {result.get('cliente_nombre')}\n"
            f"Total: ${float(result.get('total', 0)):,.0f}\n"
            f"Fecha: {result.get('fecha')}"
        )

    # Generico para reportes
    if tool_name == "consultar_ventas":
        return (
            f"Ventas {result.get('periodo', '')}:\n"
            f"Total: ${float(result.get('total_ventas', 0)):,.0f}\n"
            f"Cantidad: {result.get('cantidad_facturas', 0)} facturas"
        )

    if tool_name == "consultar_gastos":
        return (
            f"Gastos {result.get('periodo', '')}:\n"
            f"Total: ${float(result.get('total_gastos', 0)):,.0f}\n"
            f"Cantidad: {result.get('cantidad_compras', 0)} compras"
        )

    # Fallback super generico
    if result.get("error"):
        return f"⚠️ {result['error']}"

    return ""


def _pedido_from_args(args: dict) -> Pedido | None:
    """Convierte args de tool registrar_pedido en Pedido pydantic."""
    try:
        items_raw = args.get("items") or []
        items = [
            PedidoItem(
                descripcion=str(it.get("descripcion", "")),
                codigo=(str(it["codigo"]).strip() or None) if it.get("codigo") else None,
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
    intentos_vacio = 0  # cuantas veces Gemini devolvio vacio (sin texto ni tool)

    for _step in range(8):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL, contents=contents, config=config,
            )
        except Exception as e:
            log.exception("Error LLM en step %d", _step)
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
        text_out = text_out.strip()

        # RETRY: si Gemini no genero texto NI llamo tools y es el primer intento, reintentar
        if not text_out and not tools_used and intentos_vacio < 2:
            intentos_vacio += 1
            log.warning("Gemini devolvio vacio sin tool, reintento #%d", intentos_vacio)
            # No agregamos el cand vacio al history, repetimos
            continue

        # FALLBACK: si el modelo no genero texto pero ejecuto tools, armar respuesta
        # con los datos del ultimo tool result (Gemini a veces deja vacio el final)
        if not text_out and last_tool_result and tools_used:
            text_out = _fallback_text_from_tool(tools_used[-1], last_tool_result)

        # FALLBACK 2: ultimo intento con un mensaje del sistema pidiendo resumen
        if not text_out and tools_used:
            log.warning("Gemini devolvio vacio despues de %s. Reintentando con prompt directo.",
                        tools_used)
            try:
                followup = client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=contents + [genai_types.Content(
                        role="user",
                        parts=[genai_types.Part.from_text(
                            text="Resumime en lenguaje natural el resultado anterior para el usuario.")],
                    )],
                    config=genai_types.GenerateContentConfig(
                        system_instruction=_system_instruction(user_role),
                        temperature=0.2,
                    ),
                )
                if followup.candidates and followup.candidates[0].content:
                    for p in (followup.candidates[0].content.parts or []):
                        if getattr(p, "text", None):
                            text_out += p.text
                    text_out = text_out.strip()
            except Exception:
                log.exception("Followup fallo tambien")

        _push_history(chat_id, user_content)
        _push_history(chat_id, cand.content)
        return AgentReply(
            kind="texto",
            texto=text_out or "(sin respuesta)",
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
