# Matriz de capacidades — Siigo API (Skiimo)

Ejecutado: 2026-05-19T12:14:32

Leyenda: OK = funciona; FAIL = no funciona o endpoint no existe; SKIP = no probado por dependencia.

| # | Prueba | Capacidad | Resultado | Detalle |
|---|---|---|---|---|
| 1 | 1.1 invoices filtrado por created_start (ultimos 30 dias) | `filter_invoices_by_date` | **OK** |  |
| 2 | 1.2 invoices filtrado por date_start | `filter_invoices_by_doc_date` | **OK** |  |
| 3 | 1.3 customers filtrado por identification | `search_customer_by_id` | **OK** |  |
| 4 | 1.4 products filtrado por code | `search_product_by_code` | **OK** |  |
| 5 | 1.5 invoice detalle by id (71bb29ce...) | `get_invoice_by_id` | **OK** |  |
| 6 | 1.6 invoice PDF (71bb29ce...) | `get_invoice_pdf` | **FAIL** | HTTP 500:  |
| 7 | 1.7 customers page_size=100 | `pagination_100` | **OK** |  |
| 8 | 1.8 customers con modified_start | `filter_by_modified_start` | **OK** |  |
| 9 | 1.9 GET /v1/webhooks | `list_webhooks` | **OK** |  |
| 10 | 1.10 purchase detalle by id (d8a46654...) | `get_purchase_by_id` | **OK** |  |
| 11 | 2.1 POST /v1/customers (crear) | `create_customer` | **OK** |  |
| 12 | 2.2 GET /v1/customers/{id} | `get_customer_by_id` | **OK** |  |
| 13 | 2.3 PUT /v1/customers/{id} (actualizar) | `update_customer` | **FAIL** | HTTP 400: {"Status":400,"Errors":[{"Code":"parameter_required","Message":"The field name is required","Params":["name"],... |
| 14 | 3.1 POST /v1/products (crear) | `create_product` | **OK** |  |
| 15 | 3.2 GET /v1/products/{id} | `get_product_by_id` | **OK** |  |
| 16 | 4.1 POST /v1/invoices (factura tradicional) | `create_invoice_fv_tradicional` | **OK** |  |
| 17 | 4b POST /v1/credit-notes | `create_credit_note` | **OK** |  |
| 18 | 5.1 POST /v1/purchases (factura compra GASTO) | `create_purchase_gasto` | **OK** |  |
| 19 | 6.1 GET /v1/document-types?type=RC | `list_rc_doc_types` | **OK** |  |
| 20 | 6.2 GET /v1/webhooks (capabilidad ya verificada en bloque 1.9) | `webhooks_supported` | **OK** |  |
| 21 | 6.3 POST /v1/webhooks (test suscripcion) | `create_webhook` | **FAIL** | HTTP 400: {"status":400,"errors":[{"code":"parameter_required","message":"The field application_id is required","params"... |

## Resumen por bloque

- OK: 18
- FAIL: 3
- SKIP: 0
- Total: 21