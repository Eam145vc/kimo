# Reporte de hallazgos — Cuenta Siigo Skiimo Cocktails

Fecha: 2026-05-19
Cuenta: `esskimococktails@gmail.com`
Base URL: `https://api.siigo.com`
Partner-Id usado: `Skiimo`

## TL;DR

La API Siigo respondió **OK en todos los endpoints críticos**. La cuenta es de una fábrica de granizados real y activa (5.603 facturas de venta, 483 facturas de compra, 3.848 recibos de caja, 1.161 clientes, 450 productos). **Tenemos todo lo necesario para empezar a montar el bot sin bloqueos**.

---

## 1. Autenticación

- Endpoint `POST https://api.siigo.com/auth` con `{username, access_key}` devuelve `access_token` JWT.
- TTL del token: 24 h (asumido estándar Siigo, verificar `expires_in` en próxima corrida).
- Header obligatorio en cada request: `Authorization: Bearer <token>` + `Partner-Id: Skiimo`.

## 2. Volumen actual de la cuenta

| Entidad | Total |
|---|---|
| Facturas de venta | **5.603** |
| Facturas de compra | **483** |
| Recibos de caja (vouchers) | **3.848** |
| Notas crédito | (hay registros, no contado) |
| Clientes | **1.161** |
| Productos | **450** |
| Usuarios/vendedores | **3** |

Implicación: la sincronización inicial Siigo → Postgres no es trivial pero es perfectamente manejable (~10 k registros entre todo). Una primera carga completa toma minutos, no horas.

## 3. IDs críticos identificados (semana 1 — guardar en `.env`)

### Tipos de documento

| Tipo | Nombre | ID | Consecutivo actual |
|---|---|---|---|
| Factura de venta tradicional | Factura | **13214** | 5171 |
| **Factura electrónica de venta** | Factura electrónica de venta | **27703** | 681 (← más usado) |
| Factura de compra (materias primas) | MATERIAS PRIMAS | **13219** | 418 |
| Factura de compra (gasto admin) | GASTO ADMINISTRATIVO | **27394** | 75 |
| Recibo de caja | RC | (id 13213 visto en data) | 3858 |
| Nota crédito | varias (3 tipos) | — | — |

**Recomendación**: usar **27703 (factura electrónica)** como default para pedidos de vendedores, porque es lo que más se usa actualmente y dispara facturación DIAN automática.

### Impuestos

| Impuesto | ID |
|---|---|
| **IVA 19%** | **7108** ← usado en 100% de las facturas vistas |
| IVA 5% | 7109 |
| IVA 0% | 13999 |
| Impoconsumo 8% | 7123 (no veo usado en muestras, validar si aplica a granizados) |
| Retefuente / ReteICA / ReteIVA | varias (22 impuestos en total) |

### Formas de pago (9 disponibles)

| ID | Nombre | Notas |
|---|---|---|
| 3043 | Efectivo | |
| 3044 | Crédito | con `due_date` |
| 3045 | Tarjeta Débito | |
| 3046 | Tarjeta Crédito | |
| 8102 | NEQUI | |
| 8103 | DAVIPLATA | |
| 8104 | BANCO AHORROS | más usado |
| 10766 | Clientes Nacionales | con `due_date` |
| 10767 | Clientes Extranjero | con `due_date` |

### Vendedores (users)

| ID | Nombre | Email |
|---|---|---|
| **341** | Oscar Andres Gomez Montoya (admin) | esskimococktails@gmail.com |
| 716 | Frank Tabares | frank.tabares@hotmail.com |
| **1026** | MANUELA PATIÑO GIRALDO | oscargo12360@gmail.com |

**Recomendación**: cada vendedor de la fábrica debe estar registrado en la tabla `vendedores` de nuestro Postgres con su `telegram_chat_id` mapeado a uno de estos `id` de Siigo. Hoy solo 3 usuarios, así que el onboarding es trivial.

## 4. Estructura real de los documentos

### Factura de venta (FV electrónica) — campos clave para POST /v1/invoices

```json
{
  "document": {"id": 27703},
  "date": "2026-05-15",
  "customer": {"identification": "32160242", "branch_office": 0},
  "seller": 341,
  "items": [
    {
      "code": "P23",
      "quantity": 1.0,
      "price": 31512.605042,     // pre-IVA
      "description": "PERLAS EXPLOSIVAS MANGO BICHE 1200 GR",
      "taxes": [{"id": 7108}]    // IVA 19%
    }
  ],
  "payments": [{"id": 8104, "value": 157400.01}],
  "observations": ""
}
```

Respuesta incluye:
- `stamp`: estado DIAN + CUFE (firma electrónica).
- `public_url`: link al PDF visible para el cliente.
- `mail.status`: si el correo fue enviado.

### Factura de compra (FC) — campos clave para POST /v1/purchases

```json
{
  "document": {"id": 13219},
  "date": "2026-05-04",
  "supplier": {"identification": "811027326", "branch_office": 0},
  "provider_invoice": {"prefix": "EI", "number": "50650"},
  "items": [
    {
      "type": "Product",
      "code": "AC1",
      "quantity": 200000.0,
      "price": 4.5,
      "description": "ACIDO CITRICO",
      "taxes": [{"id": 7108}]
    }
  ],
  "payments": [{"id": 8104, "value": 2445450.0}]
}
```

**Importante**: el `provider_invoice` (prefijo + número de la factura del proveedor) es exactamente lo que el OCR Gemini debe extraer del PDF de la factura.

## 5. Productos y familias

13 familias de productos (`account_groups`), todas alineadas con el negocio de granizados:
- BOLSAS PARA GRANIZADORAS CON LICOR / SIN LICOR
- SACHETS 08 OZ
- PERLAS EXPLOSIVAS
- SIROPES, GELATINAS, CREMOSOS
- SALES PARA MICHELAR
- MAQUINA, REPUESTOS MAQUINAS
- Materias Primas, Servicios

**Implicación crítica para el bot**: cuando un vendedor dice *"30 bolsas chicle 6L"*, el matcher difuso debe:
1. Filtrar productos con `account_group ∈ {BOLSAS...}`.
2. Buscar similitud por `name` (ej. "BOLSA 6L CHICLE", code `A1AO`).
3. Si confianza alta → sugerir. Si baja → pedir al vendedor que elija de top 3.

## 6. Configuración faltante / sin datos

| Elemento | Estado | Acción |
|---|---|---|
| Cost centers (centros de costo) | **vacío** | No críticos — el `cost_center: false` en document_types lo confirma. Ignorar. |
| Warehouses (bodegas) | **vacío** | Los productos viven en "Sin asignar" (`id: -1`). OK por ahora; si se necesitan bodegas múltiples, configurar en Siigo web. |
| `/v1/accounting-periods` | **404** | No expuesto en la API o no aplica. Sin impacto en el bot. |
| Fixed assets | 14 items | No relevante para pedidos de venta o compras corrientes. |

## 7. Limitaciones detectadas / cosas a verificar

1. **Tax classification / Impoconsumo**: ninguna factura de muestra usa Impoconsumo. Confirmar con contador si granizados pagan IVA 19% solamente (lo que sugieren los datos) o si en algún caso aplica impoconsumo (típico en bebidas alcohólicas).
2. **Decimales en precios**: `price` viene con muchos decimales (ej. `31512.605042`). El bot debe respetar esta precisión — no redondear antes de enviar a Siigo.
3. **`tax_included`**: algunos productos están con `tax_included: true` (precio incluye IVA), otros con `false`. Hay que leer el flag del producto antes de calcular.
4. **Branch office**: siempre `0` en las muestras (sucursal principal). Asumir 0 salvo que el cliente tenga sucursales reales.
5. **Webhooks**: no exploramos endpoint de webhooks en esta sesión. Para fase 1 vamos a polling cada 15 min, lo cual sobra para 50 facturas/día.
6. **Rate limit**: no probamos el límite real. Documentación Siigo menciona ~100 req/min. Para el bot esto es holgado.

## 8. Próximos pasos sugeridos (Fase 1 — local)

1. ✅ Cliente Siigo mínimo (hecho — `siigo_client.py`).
2. ✅ Exploración de catálogos (hecho — `explorations/`).
3. **Crear `config.py` con todos los IDs descubiertos** y `.env` con defaults.
4. Crear un script `siigo_create_test_invoice.py` que cree una factura de prueba (modo borrador o usando un cliente "TEST"), valide la respuesta, y la **anule inmediatamente** para no manchar la contabilidad. Esto confirma que tenemos permisos de escritura.
5. Crear `sync_local.py` que baje todos los clientes, productos, facturas y compras a archivos JSON locales (Postgres viene después).
6. Recién entonces avanzar al bot Telegram + Gemini.

## 9. Bloqueos: NINGUNO

La cuenta está completa, la API funciona, los catálogos están poblados, hay historial abundante. Podemos seguir con confianza.
