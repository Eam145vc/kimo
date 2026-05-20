# Endpoints documentados — verificacion en cuenta real

Ejecutado: 2026-05-19 12:20:09

| Prueba | Capacidad | Resultado | Detalle |
|---|---|---|---|
| GET /v1/quotations | `list_quotations` | **OK** |  |
| GET /v1/payment-receipts | `list_payment_receipts` | **OK** |  |
| GET /v1/purchase-support-documents | `list_support_docs` | **OK** |  |
| GET /v1/debit-notes | `list_debit_notes` | **FAIL** | HTTP 404: { "statusCode": 404, "message": "Resource not found" } |
| GET /v1/invoices/{id}/pdf | `get_invoice_pdf` | **OK** |  |
| POST /v1/invoices/{id}/mail | `send_invoice_email` | **FAIL** | HTTP 400: {"Status":400,"Errors":[{"Code":"parameter_required","Message":"The field mail_to is required","Params":["mail_to"],"Detail":"Check the API documentation: https://developer.siigo.com/introdu... |
| POST /v1/invoices/{id}/annul | `annul_invoice` | **SKIP** | endpoint conocido por SDK oficial; no lo probamos en cuenta real para no generar mas movimientos contables |
| POST /v1/invoices/{id}/stamp | `stamp_dian` | **SKIP** | endpoint conocido por SDK oficial; aplica solo a documento electronico 27703 |
| POST /v1/quotations | `create_quotation` | **SKIP** | no se encontro document-type de cotizacion (CT/CO/COT/QUO) |
| POST /v1/webhooks (topic invoices.create) | `create_webhook_invoices` | **FAIL** | HTTP 400: {"status":400,"errors":[{"code":"invalid_reference","message":"The topic doesn't exist: public.siigoapi.invoices.create","params":["topic"],"detail":"Check the API documentation: invalid_ref... |
| POST /v1/webhooks (topic products.create) | `webhook_topic_products_create` | **OK** |  |
| POST /v1/webhooks (topic customers.create) | `webhook_topic_customers_create` | **FAIL** | HTTP 400: {"status":400,"errors":[{"code":"invalid_reference","message":"The topic doesn't exist: public.siigoapi.customers.create","params":["topic"],"detail":"Check the API documentation: invalid_re... |
| POST /v1/webhooks (topic purchases.create) | `webhook_topic_purchases_create` | **FAIL** | HTTP 400: {"status":400,"errors":[{"code":"invalid_reference","message":"The topic doesn't exist: public.siigoapi.purchases.create","params":["topic"],"detail":"Check the API documentation: invalid_re... |
| POST /v1/webhooks (topic invoices.update) | `webhook_topic_invoices_update` | **FAIL** | HTTP 400: {"status":400,"errors":[{"code":"invalid_reference","message":"The topic doesn't exist: public.siigoapi.invoices.update","params":["topic"],"detail":"Check the API documentation: invalid_ref... |
| POST /v1/webhooks (topic payments.create) | `webhook_topic_payments_create` | **FAIL** | HTTP 400: {"status":400,"errors":[{"code":"invalid_reference","message":"The topic doesn't exist: public.siigoapi.payments.create","params":["topic"],"detail":"Check the API documentation: invalid_ref... |