"""Schemas Pydantic para structured output de Gemini."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PedidoItem(BaseModel):
    descripcion: str = Field(description="Descripcion del producto tal como lo dijo el vendedor")
    cantidad: float = Field(description="Cantidad pedida; si no se menciona, asumir 1")
    precio_unitario: float | None = Field(
        default=None,
        description="Precio unitario sin IVA si el vendedor lo dijo; null si no se menciono",
    )


class Pedido(BaseModel):
    """Pedido de venta extraido de un mensaje del vendedor."""
    cliente_nombre: str | None = Field(
        default=None,
        description="Nombre o razon social del cliente como lo dijo el vendedor",
    )
    cliente_nit: str | None = Field(
        default=None,
        description="NIT/cedula del cliente solo si el vendedor lo menciono explicitamente",
    )
    items: list[PedidoItem] = Field(description="Lista de items pedidos")
    fecha_entrega: str | None = Field(
        default=None,
        description="Fecha de entrega en formato YYYY-MM-DD si se menciono",
    )
    forma_pago: str | None = Field(
        default=None,
        description="Forma de pago si se menciono: efectivo, credito, nequi, daviplata, tarjeta",
    )
    descuento_pct: float | None = Field(
        default=None,
        description="Porcentaje de descuento PUNTUAL sobre el total (ej: cumpleanos, atencion, regalo). 5.0 = 5%. Null si no aplica.",
        ge=0.0,
        le=100.0,
    )
    descuento_motivo: str | None = Field(
        default=None,
        description="Motivo del descuento puntual. Ej: 'cumpleaños', 'cliente fiel', 'promo dia x'.",
    )
    observaciones: str | None = Field(
        default=None,
        description="Notas adicionales del vendedor (entregar antes de X hora, etc.)",
    )
    confidence: float = Field(
        description="Confianza 0-1 de que la extraccion es correcta y completa",
        ge=0.0,
        le=1.0,
    )


class ComprobantePago(BaseModel):
    """Comprobante de pago/transferencia extraido de una foto.
    Aplica para pantallazos de Nequi, Daviplata, Bancolombia, Davivienda, etc.
    """
    monto: float = Field(description="Monto del pago en pesos colombianos (sin signos, sin puntos miles)")
    metodo_pago: Literal[
        "nequi", "daviplata", "bancolombia", "davivienda", "banco_otro",
        "tarjeta_debito", "tarjeta_credito", "efectivo", "desconocido",
    ] = Field(description="App o banco que se ve en el comprobante")
    fecha_pago: str | None = Field(
        default=None,
        description="Fecha del pago en YYYY-MM-DD. Null si no es legible.",
    )
    numero_referencia: str | None = Field(
        default=None,
        description="Numero de referencia / transaccion / aprobacion si aparece",
    )
    titular_destino: str | None = Field(
        default=None,
        description="A quien se le transfirio (titular de la cuenta destino) si se ve",
    )
    titular_origen: str | None = Field(
        default=None,
        description="Quien envio el pago (titular de la cuenta origen) si se ve",
    )
    cuenta_destino: str | None = Field(
        default=None,
        description="Numero parcial de la cuenta destino si aparece",
    )
    es_comprobante_valido: bool = Field(
        description="True solo si la imagen es claramente un comprobante de pago/transferencia exitoso",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    observaciones: str | None = Field(
        default=None,
        description="Cualquier dato adicional relevante: 'aprobado', 'pendiente', 'rechazado', etc.",
    )


class FacturaProveedorItem(BaseModel):
    descripcion: str
    cantidad: float
    precio_unitario: float
    iva_pct: float | None = Field(default=None, description="Porcentaje IVA: 0, 5, 19")


class FacturaProveedor(BaseModel):
    """Factura de proveedor extraida de un PDF o foto."""
    proveedor_nombre: str | None = None
    proveedor_nit: str | None = None
    numero_factura: str | None = Field(default=None, description="Numero/folio de la factura del proveedor")
    prefijo_factura: str | None = Field(default=None, description="Prefijo si tiene (FE, EI, etc.)")
    fecha: str | None = Field(default=None, description="Fecha en YYYY-MM-DD")
    items: list[FacturaProveedorItem] = Field(default_factory=list)
    subtotal: float | None = None
    iva_total: float | None = None
    retenciones: float | None = None
    total: float | None = None
    categoria: Literal[
        "materias_primas",
        "gasto_administrativo",
        "documento_soporte",
        "otro",
    ] = "gasto_administrativo"
    forma_pago: str | None = None
    observaciones: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
