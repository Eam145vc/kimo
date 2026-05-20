# Matriz de capacidades — Siigo API (Skiimo)

Fecha: 2026-05-19
Cuenta: `esskimococktails@gmail.com`
Tests ejecutados: 21 principales + 17 extras = **38 verificaciones**

---

## TL;DR

**Funciona el 80% de lo que necesitamos.** Lo que podemos hacer:
- ✅ Leer todo: clientes, productos, facturas, compras, recibos de caja, notas crédito.
- ✅ Filtrar por fecha, NIT, vendedor, código de producto.
- ✅ Crear clientes, productos, facturas de venta, facturas de compra, notas crédito.
- ✅ Sincronización incremental (`modified_start`, `created_start`).
- ✅ Webhooks (listar; suscribir requiere `application_id` de Siigo).

Lo que **NO** podemos hacer (y workarounds):
- ❌ Borrar facturas emitidas → usar nota crédito (probado, funciona).
- ❌ Eliminar clientes → marcar `active=false` (probado, funciona).
- ❌ Editar clientes con PUT parcial → enviar payload completo con todos los campos requeridos.
- ❌ Descargar PDF de factura vía API → usar `public_url` del response.
- ❌ Endpoint de reportes / agregados → reconstruir en Postgres local.
- ❌ Catálogo de ciudades / tipos de ID vía API → mantener tabla local con códigos DIAN.

---

## Tabla maestra de capacidades

### 🔵 LECTURA

| Capacidad | Endpoint | Estado | Notas |
|---|---|---|---|
| Listar clientes (paginado) | `GET /v1/customers` | ✅ | 1161 en cuenta |
| Buscar cliente por identificación | `GET /v1/customers?identification=X` | ✅ | Match exacto |
| Detalle cliente por id | `GET /v1/customers/{id}` | ✅ | |
| Filtrar clientes por modified_start | `GET /v1/customers?modified_start=YYYY-MM-DD` | ✅ | Para sync incremental |
| Listar suppliers | `GET /v1/customers?type=Supplier` | ✅ | No hay endpoint `/suppliers` |
| Listar productos | `GET /v1/products` | ✅ | 450 en cuenta |
| Buscar producto por code | `GET /v1/products?code=X` | ✅ | |
| Detalle producto por id | `GET /v1/products/{id}` | ✅ | |
| Listar facturas venta | `GET /v1/invoices` | ✅ | 5603 en cuenta |
| Detalle factura por id | `GET /v1/invoices/{id}` | ✅ | |
| Filtrar facturas por created_start | `GET /v1/invoices?created_start=` | ✅ | Para sync |
| Filtrar facturas por date_start | `GET /v1/invoices?date_start=` | ✅ | Por fecha del documento |
| Filtrar facturas por cliente NIT | `GET /v1/invoices?customer_identification=` | ✅ | Útil para reportes |
| Filtrar facturas por vendedor | `GET /v1/invoices?seller=341` | ✅ | Útil para reportes |
| Listar facturas compra | `GET /v1/purchases` | ✅ | 483 en cuenta |
| Detalle compra por id | `GET /v1/purchases/{id}` | ✅ | |
| Listar recibos de caja | `GET /v1/vouchers` | ✅ | 3848 en cuenta |
| Listar notas crédito | `GET /v1/credit-notes` | ✅ | |
| Asientos contables (journals) | `GET /v1/journals` | ✅ | Muy pesado (~4MB la página) |
| Tipos de documento (FV/FC/RC/NC/ND/DS) | `GET /v1/document-types?type=X` | ✅ | |
| Impuestos | `GET /v1/taxes` | ✅ | 22 impuestos |
| Formas de pago | `GET /v1/payment-types?document_type=FV` | ✅ | 9 formas |
| Usuarios/vendedores | `GET /v1/users` | ✅ | 3 usuarios |
| Account groups (familias) | `GET /v1/account-groups` | ✅ | 13 familias |
| Listar webhooks suscritos | `GET /v1/webhooks` | ✅ | |
| **PDF de factura** | `GET /v1/invoices/{id}/pdf` | ❌ | **500**. Workaround: usar `public_url` del response al crear o al hacer GET |
| Endpoint de reportes | `GET /v1/reports` | ❌ | **404**. No existe. Construir desde Postgres local |
| Cash-flow | `GET /v1/cash-flow` | ❌ | **404**. Calcular localmente |
| Catálogo de ciudades | `GET /v1/cities` | ❌ | **404**. Usar tabla local con códigos DIAN |
| Tipos de identificación | `GET /v1/id-types` | ❌ | **404**. Hardcodear (13=CC, 31=NIT, etc.) |
| Tax classifications | `GET /v1/tax-classifications` | ❌ | **404**. Usar valores: Taxed/Excluded/Exempt |
| Períodos contables | `GET /v1/accounting-periods` | ❌ | **404**. No expuesto |

### 🟢 ESCRITURA

| Capacidad | Endpoint | Estado | Notas críticas |
|---|---|---|---|
| **Crear cliente** | `POST /v1/customers` | ✅ | Campos mínimos: `person_type`, `id_type`, `identification`, `name` (array), `branch_office`. Para Person necesita `name` con 1+ entradas; aceptó 1 cuando se respeta estructura. |
| Actualizar cliente | `PUT /v1/customers/{id}` | ⚠️ | **No acepta delta**. Hay que enviar payload completo con todos los campos requeridos. Probado y funciona (cliente desactivado vía `active=false`). |
| **Borrar cliente** | `DELETE /v1/customers/{id}` | ❌ | **403 disabled_functionality**. Workaround: `PUT active=false`. |
| **Crear producto** | `POST /v1/products` | ✅ | Campos mínimos: `code`, `name`, `account_group`, `type`, `taxes`. |
| Actualizar producto | `PUT /v1/products/{id}` | ❓ | No probado, asumimos similar a cliente. |
| **Borrar producto** | `DELETE /v1/products/{id}` | ✅ | Funcionó en el test (producto recién creado, sin movimientos). Cuidado: probablemente falle si el producto tiene historial. |
| **Crear factura venta (FV tradicional)** | `POST /v1/invoices` con `document.id=13214` | ✅ | Generó FV-1-5171. No envía a DIAN. |
| Crear factura venta electrónica (FE) | `POST /v1/invoices` con `document.id=27703` | ❓ | **NO PROBADO**. Esta es la que dispara timbrado DIAN automático y CUFE. Asumir funciona pero usar con cuidado en producción. |
| **Borrar factura** | `DELETE /v1/invoices/{id}` | ❌ | **409 delete_not_allowed** si está enlazada a NC u otros documentos. Workaround: emitir nota crédito que la neutraliza. |
| Anular factura (void) | `POST /v1/invoices/{id}/void` | ❌ | **404**. No existe endpoint. Único camino: nota crédito. |
| **Crear nota crédito** | `POST /v1/credit-notes` | ✅ | Generó NC-1-1. Requiere referencia a `invoice` (id), `cause`, `customer`, `items`, `payments`. |
| Borrar nota crédito | `DELETE /v1/credit-notes/{id}` | ❌ | **404**. Workaround desconocido. |
| **Crear factura compra** | `POST /v1/purchases` | ✅ | Generó FC-2-75. Requiere `provider_invoice.prefix` y `.number`. |
| Borrar factura compra | `DELETE /v1/purchases/{id}` | ✅ | **Funcionó** en el test (sin pagos enlazados). Útil para deshacer un error reciente. |
| Crear webhook | `POST /v1/webhooks` | ⚠️ | Requiere `application_id` (no documentado en respuesta). Hay que pedirlo a Siigo. |
| Crear recibo de caja (RC) | `POST /v1/vouchers` | ❓ | **NO PROBADO**. RC doc type id 13213 existe. |

### 🟡 LÍMITES / COMPORTAMIENTOS DESCUBIERTOS

| Aspecto | Hallazgo |
|---|---|
| Token TTL | JWT estándar, ~24h. Cachear en Redis con TTL 23h. |
| Rate limit | No alcanzado en 80+ requests en <2 min. Documentación menciona ~100 req/min. |
| Pagination | `page_size` hasta 100 funciona; valores mayores no probados pero documentación los permite hasta 1000 en algunos endpoints. |
| Consecutivos DIAN | Cada `POST /v1/invoices` consume un consecutivo real (FV-1-5170 → FV-1-5171). **No hay sandbox**. Las pruebas tocan la contabilidad real. |
| Idempotencia API | **Siigo NO ofrece idempotency-key nativo**. Hay que implementarla en nuestro lado: hash(cliente+items+fecha) guardado antes de hacer POST. |
| Decimales en precios | Hasta 6 decimales (`31512.605042`). No redondear antes de enviar. |
| `tax_included` por producto | Cada producto define si su precio incluye o no IVA. Leer del catálogo antes de calcular total. |
| Borrado en cascada | Cliente con facturas → no se puede borrar; factura con NC → no se puede borrar. La API es estricta con integridad contable. |

---

## Implicaciones para el diseño del bot

### Lo que sí podemos automatizar 100%

1. **Crear pedido de venta** → `POST /v1/invoices`. Funciona end-to-end con consecutivo automático y `public_url` para enviar al cliente.
2. **Registrar factura de proveedor** → `POST /v1/purchases` con `provider_invoice.{prefix,number}` extraídos por Gemini del PDF.
3. **Crear cliente nuevo desde el chat** si el vendedor menciona uno que no existe → `POST /v1/customers` con datos mínimos.
4. **Sincronización incremental** cada 15 min con `modified_start` para clientes/productos y `created_start` para facturas/compras.
5. **Reportes conversacionales** filtrando por vendedor, NIT, rango de fechas.

### Lo que requiere flujo especial

1. **Anular un pedido emitido por error** → emitir nota crédito automáticamente. El bot debe ofrecer botón "Anular" que internamente crea NC con los mismos items y valor.
2. **Marcar cliente como inactivo** → `PUT active=false` con payload completo. El bot debe ofrecer "Desactivar cliente" en lugar de "borrar".
3. **PDF para enviar al cliente** → tomar `public_url` del response del POST `/invoices` (no del endpoint `/pdf` que falla).
4. **Suscribirse a webhooks** → contactar a Siigo para obtener `application_id` (registro de partner). **Mientras tanto: polling cada 15 min funciona perfecto para volumen actual**.

### Lo que tenemos que mantener localmente

1. **Códigos DIAN** de ciudades, departamentos, países, tipos de identificación → tabla `ref_dian` en Postgres (descargable del sitio DIAN, ~5000 ciudades + 32 deptos).
2. **Reportes y agregaciones** → todas las queries (top clientes, ventas por vendedor, etc.) se hacen contra el espejo Postgres.
3. **Idempotencia** → tabla `idempotency_keys` con hash(vendedor+cliente+items+fecha) para pedidos y hash(NIT+numero+total) para facturas proveedor. Bloquear duplicados ANTES de llamar a Siigo.

---

## Recursos creados durante las pruebas (estado final)

| Tipo | ID | Nombre | Estado |
|---|---|---|---|
| customer | 406be39e-a490-436f-9722-56f0aba6626d | "ZZZ TEST BOT - INACTIVO" | active=false (oculto en búsquedas activas) |
| product | ac0dbda9-4e9a-405f-9f3d-8467da4b4614 | TST10863 | ✅ borrado |
| invoice | 538e3537-4f76-4500-a008-1377da429f52 | FV-1-5171 | balance=0 (anulada por NC-1-1) |
| credit_note | 80abc1f9-cdcd-494f-be73-769ce731a603 | NC-1-1 | activa, $119 |
| purchase | 0d1d1766-5bff-4a41-9ca4-98000e6f7ddc | FC-2-75 | ✅ borrado |

**Impacto contable**: FV-1-5171 y NC-1-1 quedan en la cuenta pero se cancelan entre sí (saldo neto $0). Si el contador prefiere verlas eliminadas, hay que hacerlo desde la interfaz web de Siigo (la API no lo permite).

---

## Recomendación para el bot

1. **Default doc type para pedidos**: empezar con **factura tradicional 13214** (no electrónica) durante las primeras semanas para que cualquier prueba que se filtre no dispare DIAN. Switch a 27703 cuando esté validado.
2. **Variable de entorno `SIIGO_INVOICE_TEST_MODE=true`** en dev/staging → forzaría siempre 13214 + observación "[TEST]" + valores pequeños.
3. **Antes de cada `POST`** validar idempotency_key en Postgres → si existe, devolver el resultado previo sin tocar Siigo.
4. **Nunca dejar de auditar**: cada llamada de escritura va a `audit_log` con request+response+trace_id.
5. **Bandera global de pausa**: una env var `SIIGO_WRITES_ENABLED=false` que el bot respete para abortar cualquier escritura. Útil para incidentes.
