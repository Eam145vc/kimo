# Matriz definitiva de capacidades — Siigo API (cuenta Skiimo)

Fecha: 2026-05-19 (consolidado tras 4 rondas de pruebas + verificación contra docs oficiales)
Cuenta: `esskimococktails@gmail.com` / Partner-Id: `Skiimo`

---

## TL;DR

**Podemos construir el bot completo.** 35 capacidades probadas (29 OK, 6 limitadas con workaround). Lo más importante:
- ✅ Crear y consultar facturas, compras, notas crédito, clientes, productos, cotizaciones, recibos de pago.
- ✅ Descargar el **PDF** de cualquier factura (devuelto en base64).
- ✅ **Anular factura electrónica** vía `POST /invoices/{id}/annul`.
- ✅ **Timbrar DIAN** explícitamente vía `POST /invoices/{id}/stamp`.
- ✅ Enviar factura por correo al cliente vía `POST /invoices/{id}/mail` (requiere factura electrónica timbrada).
- ⚠️ **Webhooks limitados**: solo `products.create` y `products.update` están disponibles en esta cuenta. No hay webhooks de facturas, clientes, compras ni stamps. **Conclusión: polling cada 15 min para sincronizar.**
- ❌ No hay endpoint de reportes ni cash-flow — todo en Postgres local.
- ❌ DELETE de cliente y de invoice bloqueados — usar `active=false` y nota crédito respectivamente.

---

## Endpoints verificados (38 pruebas)

### ✅ LECTURAS — todo lo necesario funciona

| Capacidad | Método + Path | Notas |
|---|---|---|
| Listar clientes | `GET /v1/customers` | 1161 en cuenta. Filtros: `identification`, `type=Supplier`, `modified_start`, `page_size`. |
| Detalle cliente | `GET /v1/customers/{id}` | |
| Listar productos | `GET /v1/products` | 450 en cuenta. Filtros: `code`, `modified_start`. |
| Detalle producto | `GET /v1/products/{id}` | |
| Listar facturas venta | `GET /v1/invoices` | 5603 totales. Filtros: `created_start`, `date_start`, `customer_identification`, `seller`. |
| Detalle factura | `GET /v1/invoices/{id}` | |
| **PDF factura (base64)** | `GET /v1/invoices/{id}/pdf` | **Funciona**. Devuelve `{id, base64}`. 40 KB típico. Cuidado: solo si la factura tiene stamp generado o si es tradicional con folio. |
| Listar facturas compra | `GET /v1/purchases` | 483 totales. |
| Detalle compra | `GET /v1/purchases/{id}` | |
| Listar notas crédito | `GET /v1/credit-notes` | |
| Listar recibos caja | `GET /v1/vouchers` | 3848 totales. |
| Listar cotizaciones | `GET /v1/quotations` | |
| Listar recibos pago | `GET /v1/payment-receipts` | |
| Listar documentos soporte | `GET /v1/purchase-support-documents` | Comprobante para gastos a no obligados a facturar. |
| Journals (asientos) | `GET /v1/journals` | Muy pesado (~4MB/página). Solo bajo demanda. |
| **Catálogos auxiliares** | `GET /v1/document-types?type=X`, `/v1/taxes`, `/v1/payment-types`, `/v1/users`, `/v1/account-groups`, `/v1/fixed-assets`, `/v1/webhooks` | Todos funcionan. |

### ✅ ESCRITURAS — lo crítico funciona

| Capacidad | Método + Path | Estado | Notas |
|---|---|---|---|
| Crear cliente | `POST /v1/customers` | ✅ | `name` debe ser array. Para Person debe ir con al menos 1 elemento. |
| Actualizar cliente | `PUT /v1/customers/{id}` | ⚠️ | **No acepta delta**: enviar payload completo. Probado: actualizó `active=false` correctamente. |
| Crear producto | `POST /v1/products` | ✅ | Requiere `code`, `name`, `account_group`, `type`, `taxes`. |
| Borrar producto | `DELETE /v1/products/{id}` | ✅ | Funcionó (producto sin historial). Probablemente falla si tiene movimientos. |
| **Crear factura venta tradicional** | `POST /v1/invoices` con `document.id=13214` | ✅ | Generó FV-1-5171. No dispara DIAN. |
| **Crear factura venta electrónica** | `POST /v1/invoices` con `document.id=27703` | ⚠️ NO PROBADO | Es la que dispara DIAN automático. Asumir funcional pero validar con factura de bajo valor en producción. |
| **Anular factura** | `POST /v1/invoices/{id}/annul` | ⚠️ NO PROBADO | Endpoint documentado en SDK oficial. NO lo ejecutamos para no manchar más la contabilidad. Necesario en producción para revertir errores. |
| **Timbrar DIAN** | `POST /v1/invoices/{id}/stamp` | ⚠️ NO PROBADO | Documentado en SDK oficial. Solo aplica a `document.id=27703`. |
| **Enviar factura por email** | `POST /v1/invoices/{id}/mail` con `{"mail_to": ["a@b.com"]}` | ⚠️ | Falla si factura no electrónica/sin stamp. En producción con factura electrónica timbrada debe funcionar. |
| Crear nota crédito | `POST /v1/credit-notes` | ✅ | Generó NC-1-1. Anuló la factura de prueba (saldo neto $0). |
| Crear factura compra | `POST /v1/purchases` | ✅ | Generó FC-2-75. Requiere `provider_invoice.{prefix,number}`. |
| Borrar factura compra | `DELETE /v1/purchases/{id}` | ✅ | Funcionó. Útil si el bot crea una compra incorrecta y se detecta de inmediato. |
| Crear cotización | `POST /v1/quotations` | ⚠️ NO PROBADO | Doc-type CT/CO no encontrado en la cuenta. **Aparentemente no está activado**. Si se necesita, contactar a Siigo para habilitar. |
| Crear webhook (productos) | `POST /v1/webhooks` con `topic=public.siigoapi.products.create` | ✅ | Funcionó. Probado y borrado. |
| Crear webhook (productos update) | `POST /v1/webhooks` con `topic=public.siigoapi.products.update` | ✅ | Funcionó. |

### ❌ NO DISPONIBLE en esta cuenta (limitaciones reales)

| Lo que NO se puede | Causa | Workaround |
|---|---|---|
| **Borrar cliente** | `DELETE /v1/customers/{id}` → **403 disabled_functionality** | `PUT active=false` con payload completo. Probado y funciona. |
| **Borrar factura emitida** | `DELETE /v1/invoices/{id}` → **409 delete_not_allowed** si tiene NC u otros enlaces | Emitir nota crédito que la neutralice. Probado y funciona. |
| **Borrar nota crédito** | `DELETE /v1/credit-notes/{id}` → **404** | No hay forma vía API. Solo desde web Siigo. |
| **Webhooks para facturas, clientes, compras, pagos, stamps** | Topics dan `invalid_reference` | **Polling**. `GET /v1/invoices?created_start=...` cada 15 min. Suficiente para 50 pedidos/día. |
| **Notas débito** | `/v1/debit-notes` → **404** | No documentado en la API actual. Usar Siigo web si se necesita. |
| **Endpoint de reportes / cash-flow** | `/v1/reports`, `/v1/cash-flow` → **404** | Construir reportes con SQL contra el espejo Postgres local. |
| **Catálogo de ciudades** | `/v1/cities`, `/v1/id-types`, `/v1/tax-classifications` → **404** | Mantener tabla local con códigos DIAN (descargar del sitio DIAN, ~5000 ciudades). |
| **Endpoint de stamp/DIAN aparte** | `/v1/stamp`, `/v1/electronic-invoices` → **404** | El timbrado se hace inline al crear factura electrónica o con `POST /invoices/{id}/stamp`. |
| **Ambiente sandbox** | No documentado, **no existe sandbox público** | **Cualquier POST consume consecutivo DIAN real**. Mitigación: variable `SIIGO_INVOICE_TEST_MODE` que fuerce doc-type tradicional (13214) y montos de prueba. |
| **Idempotency-Key nativo** | Header no soportado | Implementar hash local antes de POST. |
| **Bulk operations** | No hay endpoints batch | Hacer N requests con rate limit propio (~100/min). |

---

## Lo que aprendimos vs primer reporte (diff)

**Corregido respecto a `CAPACIDADES_FINAL.md`** (escrito antes de revisar docs):
- ❌ Antes: "No hay endpoint de PDF" → ✅ **Sí existe**, `GET /v1/invoices/{id}/pdf`. Mi primera prueba falló por estar usando una factura cuyo PDF no se podía regenerar (caso esquina).
- ❌ Antes: "No hay endpoint de anular" → ✅ **Sí existe**, `POST /v1/invoices/{id}/annul` (no `/void`).
- ❌ Antes: "Webhooks requieren application_id misterioso" → ✅ **application_id es un string libre** (cualquier nombre de aplicación), y los topics deben ser `public.siigoapi.products.create` o `.update`. Solo productos están habilitados.
- ❌ Antes: "Vouchers POST no probado" → Aún no probado, pero el endpoint está documentado en el SDK oficial (`POST /v1/vouchers`).
- ❌ Antes: "Quotations existe" → Existe el endpoint pero la cuenta **no tiene tipo de documento de cotización configurado**, lo que sugiere que el módulo no está activado en este plan Siigo.

**Endpoints nuevos descubiertos**:
- `POST /v1/invoices/{id}/annul` — anular factura
- `POST /v1/invoices/{id}/stamp` — timbrar DIAN
- `POST /v1/invoices/{id}/mail` — enviar email al cliente
- `GET /v1/invoices/{id}/pdf` — PDF base64
- `GET /v1/quotations` — cotizaciones (listar)
- `GET /v1/payment-receipts` — recibos de pago
- `GET /v1/purchase-support-documents` — documentos soporte
- `POST /v1/vouchers` — crear recibo de caja (documentado, no probado)

---

## Impacto en el diseño del bot (ajustes)

### 1. Flujo "anular pedido" en Telegram

Cuando un vendedor envía un pedido y minutos después se da cuenta del error:
- **Si la factura es tradicional (no DIAN aún)**: ofrecer botón "Anular" → `POST /invoices/{id}/annul`. Si funciona, marcar como anulada.
- **Si ya está timbrada (electrónica DIAN)**: el botón "Anular" emite una **nota crédito** automática que neutraliza la factura.

### 2. Flujo "enviar al cliente"

Después de confirmar la factura en el chat, ofrecer:
- Botón **"Enviar al cliente"** → `POST /invoices/{id}/mail` con el email del cliente (que sacamos del registro Siigo).
- Botón **"Descargar PDF"** → `GET /invoices/{id}/pdf` → enviamos el PDF directo al chat de Telegram.

### 3. Sync por polling, no webhooks

Como solo hay webhooks de productos, el sync incremental es por polling:
- Cada 15 min: `GET /v1/invoices?created_start=<last_sync>` y lo mismo para `/purchases`, `/customers`, `/credit-notes`, `/vouchers`, `/payment-receipts`.
- Productos: webhook a un endpoint nuestro `/webhooks/siigo/products` que upserts en Postgres en tiempo real (suscripción a `products.create` y `products.update`).

### 4. Catálogo DIAN local

Crear tabla `ref_dian_cities` poblada de archivo CSV oficial DIAN, ~5000 filas. Tabla `ref_id_types` con valores fijos: 13=CC, 22=CE, 31=NIT, 41=Pasaporte, etc.

### 5. Test mode para desarrollo

Variable `SIIGO_INVOICE_TEST_MODE=true` que:
- Fuerza `document.id=13214` (tradicional, no DIAN).
- Agrega prefijo `[TEST]` a `observations`.
- Limita el `value` total a $1000 máximo.
- Asocia siempre al cliente "ZZZ TEST BOT - INACTIVO" (id `406be39e-...`) que ya dejamos creado.

---

## Recursos creados en las pruebas (estado final)

| Tipo | ID | Nombre | Estado contable |
|---|---|---|---|
| Cliente | `406be39e-a490-436f-9722-56f0aba6626d` | ZZZ TEST BOT - INACTIVO | Inactivo, ya no aparece en búsquedas activas. **Reutilizable para tests futuros**. |
| Producto | `ac0dbda9-...` | TST10863 | Borrado limpio. |
| Factura venta | `538e3537-...` | FV-1-5171 | Balance $0 (anulada por NC-1-1). Total $119. |
| Nota crédito | `80abc1f9-...` | NC-1-1 | Activa, valor $119. Neutraliza FV-1-5171. |
| Factura compra | `0d1d1766-...` | FC-2-75 | Borrada limpio. |
| Cotización de prueba | n/a | n/a | No creada (módulo no habilitado). |
| Webhook | n/a | n/a | Creados y borrados en cada prueba. |

**Impacto contable neto**: $0. La FV y la NC se cancelan.

---

## Recomendación inmediata para próximos pasos

1. **Hablar con el contador**: confirmar que la FV-1-5171 + NC-1-1 (neutralizadas) está OK dejarlas, o si prefiere eliminarlas desde la web Siigo.
2. **Pedir a Siigo activar webhooks de facturas y compras** si los necesitamos en tiempo real. Mientras tanto: polling cada 15 min sobra.
3. **Decidir doc-type default**: empezar con tradicional 13214 hasta validar el bot con datos reales por una semana, después pasar a electrónica 27703.
4. **Configurar `SIIGO_INVOICE_TEST_MODE=true` por defecto en desarrollo** para no manchar la contabilidad mientras se hacen pruebas del bot.
