# Pruebas extra de capacidades

| Prueba | Capacidad | Resultado |
|---|---|---|
| GET /v1/id-types (tipos de identificacion) | `GET /v1/id-types (tipos de identificacion)` | **FAIL** |
| GET /v1/cities (lista de ciudades Colombia) | `GET /v1/cities (lista de ciudades Colombia)` | **FAIL** |
| GET /v1/cities sin params | `GET /v1/cities sin params` | **FAIL** |
| GET /v1/invoices filtro por NIT cliente | `filter_invoices_by_customer_nit` | **OK** |
| GET /v1/invoices filtro por vendedor | `filter_invoices_by_seller` | **OK** |
| GET /v1/reports (existe?) | `reports_endpoint` | **FAIL** |
| GET /v1/cash-flow (existe?) | `cashflow_endpoint` | **FAIL** |
| GET /v1/webhooks (todos) | `GET /v1/webhooks (todos)` | **OK** |
| GET /v1/applications | `GET /v1/applications` | **FAIL** |
| GET /v1/customers?type=Supplier | `GET /v1/customers?type=Supplier` | **OK** |
| GET /v1/suppliers (endpoint dedicado?) | `GET /v1/suppliers (endpoint dedicado?)` | **FAIL** |
| GET /v1/credit-notes (listar) | `GET /v1/credit-notes (listar)` | **OK** |
| GET /v1/tax-classifications | `GET /v1/tax-classifications` | **FAIL** |
| POST /v1/customers MINIMO | `customer_minimal_fields` | **FAIL** |