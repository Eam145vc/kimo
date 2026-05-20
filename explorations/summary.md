# Exploracion cuenta Siigo — 2026-05-19T12:08:39

- Base URL: `https://api.siigo.com`
- Usuario: `esskimococktails@gmail.com`
- Partner-Id: `Skiimo`

## Conteos por endpoint

| Endpoint | Resultado |
|---|---|
| `catalogs` | dict keys=['document_types_FV', 'document_types_FC', 'document_types_RC', 'document_types_DS', 'document_types_NC'] |
| `customers` | 50 en pagina (total 1161) |
| `products` | 50 en pagina (total 450) |
| `invoices` | 50 en pagina (total 5603) |
| `purchases` | 50 en pagina (total 483) |
| `credit_notes` | 17 en pagina (total 17) |
| `vouchers` | 20 en pagina (total 3848) |
| `journals` | 10 en pagina (total 147) |

## Muestra del primer item de cada coleccion

### catalogs
_vacio o error_

### customers
```json
{
  "id": "6572d33c-4ad0-4077-b3ef-5fff84ba18db",
  "type": "Customer",
  "person_type": "Company",
  "id_type": {
    "code": "31",
    "name": "NIT"
  },
  "identification": "32160242",
  "branch_office": 0,
  "check_digit": "8",
  "name": [
    "MARTINEZ CARDENAS DORA LUZ"
  ],
  "active": true,
  "vat_responsible": false,
  "fiscal_responsibilities": [
    {
      "code": "R-99-PN",
      "name": "No aplica - Otros"
    }
  ],
  "address": {
    "address": "No aplica",
    "city": {
      "country_code": "Co",
      "country_name": "Colombia",
      "state_code": "05",
      "state_name": "Antioquia",
      "city_code": "05001",
      "city_name": "Medellín"
    }
  },
  "phones": [
    {
      "indicative": "604",
      "number": "3152293329"
    }
  ],
  "contacts": [
    {
      "first_name": "MARTINEZ CARDENAS DORA LUZ",
      "last_name": "",
      "email": "martinezdora460@gmail.com",
      "phone": {
        "indicative": "000",
        "number": "0000000"
      }
    }
  ],
  "metadata": {
    "created": "2026-05-15T21:40:42.147"
  }
}
```

### products
```json
{
  "id": "6ed25078-cc5a-4fad-b03f-148e8c7ec531",
  "code": "SD10",
  "name": "Mandarina",
  "account_group": {
    "id": 1755,
    "name": "Materias Primas"
  },
  "type": "Product",
  "stock_control": true,
  "active": true,
  "tax_classification": "Taxed",
  "tax_included": false,
  "tax_consumption_value": 0,
  "taxes": [
    {
      "id": 7108,
      "name": "IVA 19%",
      "type": "IVA",
      "percentage": 19.0
    }
  ],
  "unit": {
    "code": "94",
    "name": "unidad"
  },
  "unit_label": "unidad",
  "reference": "",
  "description": "",
  "additional_fields": {
    "barcode": "",
    "brand": "",
    "tariff": "",
    "model": ""
  },
  "available_quantity": 2500.0,
  "warehouses": [
    {
      "id": -1,
      "name": "Sin asignar",
      "quantity": 2500.0
    }
  ],
  "metadata": {
    "created": "2026-04-27T11:43:34.45Z"
  }
}
```

### invoices
```json
{
  "id": "71bb29ce-3827-43c2-90bd-3f376e8bd8f4",
  "document": {
    "id": 27703
  },
  "prefix": "FE",
  "number": 680,
  "name": "FV-2-680",
  "date": "2026-05-15",
  "customer": {
    "id": "6572d33c-4ad0-4077-b3ef-5fff84ba18db",
    "identification": "32160242",
    "branch_office": 0
  },
  "seller": 341,
  "total": 157400.01,
  "balance": 0.0,
  "observations": "",
  "items": [
    {
      "id": "bdc54123-0523-4f46-a8fd-340a01d3be43",
      "code": "P23",
      "quantity": 1.0,
      "price": 31512.605042,
      "description": "PERLAS EXPLOSIVAS MANGO BICHE 1200 GR",
      "taxes": [
        {
          "id": 7108,
          "name": "IVA 19%",
          "type": "IVA",
          "percentage": 19.0,
          "value": 5987.39
        }
      ],
      "total": 37500.0
    },
    {
      "id": "694753e1-1685-476d-803c-130719bd1269",
      "code": "P4",
      "quantity": 1.0,
      "price": 31512.61,
      "description": "PERLAS EXPLOSIVAS CEREZA 1200 GR",
      "taxes": [
        {
          "id": 7108,
          "name": "IVA 19%",
          "type": "IVA",
          "percentage": 19.0,
          "value": 5987.4
        }
      ],
      "total": 37500.01
    },
    {
      "id": "6871662b-7a22-466c-92ce-1e4f0b668a56",
      "code": "PE1",
      "quantity": 1.0,
      "price": 31512.605042,
      "description": "PERLAS EXPLOSIVAS MARACUYA 1200 GR",
      "taxes": [
        {
          "id": 7108,
          "name": "IVA 19%",
          "type": "IVA",
          "percentage": 19.0,
          "value": 5987.39
        }
      ],
      "total": 37500.0
    },
    {
      "id": "c2c7052f-39f2-4b20-869f-e9c11b360a28",
      "code": "AZ10",
      "quantity": 1.0,
      "price": 15882.352941,
      "description": "AZUCAR MANGO BICHE 500 GR",
      "taxes": [
        {
          "id": 7108,
          "name": "IVA 19%",
          "type": "IVA",
          "percentage": 19.0,
          "value": 3017.65
        }
      ],
      "total": 18900.0
    },
    {
      "id": "a91eed23-d4d3-4579-9a44-655f6b39cca2",
      "code": "S5",
      "quantity": 1.0,
      "price": 10924.369748,
      "description": "SAL LIMON 250 GR",
      "taxes": [
        {
          "id": 7108,
          "name": "IVA 19%",
          "type": "IVA",
          "percentage": 19.0,
          "value": 2075.63
        }
      ],
      "total": 13000.0
    },
    {
      "id": "a91eed23-d4d3-4579-9a44-655f6b39cca2",
      "code": "S5",
      "quantity": 1.0,
      "price": 10924.369748,
      "description
... [truncado]
```

### purchases
```json
{
  "id": "d8a46654-69a3-4d11-8c93-a86c7b8bcce9",
  "document": {
    "id": 13219
  },
  "number": 417,
  "name": "FC-1-417",
  "date": "2026-05-04",
  "supplier": {
    "id": "a23892d5-31e7-4386-b9ae-53f31ef6fb94",
    "identification": "811027326",
    "branch_office": 0
  },
  "total": 2445450.0,
  "balance": 0.0,
  "provider_invoice": {
    "prefix": "EI",
    "number": "50650"
  },
  "supplier_by_item": false,
  "discount_type": "Value",
  "items": [
    {
      "id": "e3b06180-6a72-4ab0-b95b-470bc95b69d6",
      "type": "Product",
      "code": "AC5",
      "quantity": 25000.0,
      "price": 12.4,
      "discount": 0.0,
      "description": "SORBATO DE POTASIO",
      "total": 368900.0,
      "taxes": [
        {
          "id": 7108,
          "name": "IVA 19%",
          "type": "IVA",
          "percentage": 19.0,
          "value": 58900.0
        }
      ]
    },
    {
      "id": "447c2df3-36ee-4dbe-837c-5003c154d265",
      "type": "Product",
      "code": "AC1",
      "quantity": 200000.0,
      "price": 4.5,
      "discount": 0.0,
      "description": "ACIDO CITRICO",
      "total": 1071000.0,
      "taxes": [
        {
          "id": 7108,
          "name": "IVA 19%",
          "type": "IVA",
          "percentage": 19.0,
          "value": 171000.0
        }
      ]
    },
    {
      "id": "80910a8d-2a47-474e-ba64-104d614c0c8f",
      "type": "Product",
      "code": "AC2",
      "quantity": 100000.0,
      "price": 8.45,
      "discount": 0.0,
      "description": "ACIDO MALICO",
      "total": 1005550.0,
      "taxes": [
        {
          "id": 7108,
          "name": "IVA 19%",
          "type": "IVA",
          "percentage": 19.0,
          "value": 160550.0
        }
      ]
    }
  ],
  "retentions": [],
  "payments": [
    {
      "id": 8104,
      "name": "BANCO AHORROS",
      "value": 2445450.0
    }
  ],
  "metadata": {
    "created": "2026-05-19T09:22:15.833"
  }
}
```

### credit_notes
```json
{
  "id": "deb4d048-76e3-4388-9a34-728685812a99",
  "document": {
    "id": 27704
  },
  "number": 17,
  "name": "NC-2-17",
  "date": "2026-05-05",
  "invoice": {
    "id": "961e1954-e978-483d-b8f9-5af14acdfcc5",
    "name": "FV-2-669"
  },
  "invoice_data": {
    "cufe": "9d8e63010bc8f67bcc8bb27338ef4f1e3b3b46b583736c591349ba5df7f6b67e41bbd468b891272aa81da0f5cd8de68e",
    "number": 669,
    "date": "2026-05-05",
    "prefix": "FE"
  },
  "reason": 4,
  "customer": {
    "id": "97a62d45-3459-42d2-870b-97866bf83e1a",
    "identification": "1006743901",
    "branch_office": 0
  },
  "seller": 341,
  "total": 9282000.0,
  "observations": "",
  "items": [
    {
      "id": "a4b8c253-4896-4d76-a6f3-8b1e76feb872",
      "code": "A8",
      "quantity": 1.0,
      "price": 6470588.235294,
      "seller": 341,
      "description": "MAQUINA GRANIZADORA 3 TANQUES",
      "taxes": [
        {
          "id": 7108,
          "name": "IVA 19%",
          "type": "IVA",
          "percentage": 19.0,
          "value": 1229411.76
        }
      ],
      "total": 7700000.0
    },
    {
      "id": "21033b64-0dc6-4bd6-b2fa-7abb479a85be",
      "code": "A1B",
      "quantity": 8.0,
      "price": 21848.739496,
      "seller": 341,
      "description": "BOLSA 6L MORA AZUL",
      "taxes": [
        {
          "id": 7108,
          "name": "IVA 19%",
          "type": "IVA",
          "percentage": 19.0,
          "value": 33210.08
        }
      ],
      "total": 208000.0
    },
    {
      "id": "74faa7be-270d-4d7e-9592-cdbfee21af7c",
      "code": "A2V",
      "quantity": 8.0,
      "price": 20168.067227,
      "seller": 341,
      "description": "BOLSA 6L MARACUMANGO SIN LICOR",
      "taxes": [
        {
          "id": 7108,
          "name": "IVA 19%",
          "type": "IVA",
          "percentage": 19.0,
          "value": 30655.46
        }
      ],
      "total": 192000.0
    },
    {
      "id": "52869dd4-890a-4857-a2f5-c8a470a3bae6",
      "code": "A1K",
      "quantity": 8.0,
      "price": 21848.739496,
      "seller": 341,
      "description": "BOLSA 6L FOURLOKO",
      "taxes": [
        {
          "id": 7108,
          "name": "IVA 19%",
          "type": "IVA",
          "percentage": 19.0,
          "value": 33210.08
        }
      ],
      "total": 208000.0
    },
    {
      "id": "8db916f1-7c63-45d8-840d-69892ac17942",
      "code": "A1L",
      "quantity": 6.0,
      "price": 21848.739496,
      "seller": 341,
      "description": "BOLSA 6L KRIPTO
... [truncado]
```

### vouchers
```json
{
  "id": "289a1cb9-d0d2-44dd-a1f9-3a4bcc0c173d",
  "document": {
    "id": 13213
  },
  "number": 3857,
  "name": "RC-1-3857",
  "date": "2026-05-18",
  "type": "DebtPayment",
  "customer": {
    "id": "5cd843a2-b08d-477d-83fe-44a2d97b9421",
    "identification": "1000000054",
    "branch_office": 0
  },
  "items": [
    {
      "due": {
        "prefix": "FV-2",
        "consecutive": 655,
        "quote": 1,
        "date": "2026-04-29"
      },
      "value": 1050000.0
    }
  ],
  "payment": {
    "id": 8104,
    "name": "BANCO AHORROS",
    "value": 1050000.0
  },
  "metadata": {
    "created": "2026-05-18T19:24:59.56"
  }
}
```

### journals
```json
{
  "id": "f4e1e946-8f6a-407e-8ab2-ded55e1a1879",
  "document": {
    "id": 13235
  },
  "number": 114,
  "name": "CC-1-114",
  "date": "2026-05-19",
  "items": [
    {
      "account": {
        "code": "53959598",
        "movement": "Debit"
      },
      "customer": {
        "id": "cba77c9d-372c-4f1e-9642-cbcaf39ef57f",
        "identification": "890903938",
        "branch_office": 0
      },
      "description": "COMPRA APLLE",
      "value": 0.0
    },
    {
      "account": {
        "code": "53152001",
        "movement": "Debit"
      },
      "customer": {
        "id": "cba77c9d-372c-4f1e-9642-cbcaf39ef57f",
        "identification": "890903938",
        "branch_office": 0
      },
      "description": "Gravamen al movimiento financiero 4xmil",
      "value": 141653.0
    },
    {
      "account": {
        "code": "11200502",
        "movement": "Credit"
      },
      "customer": {
        "id": "cba77c9d-372c-4f1e-9642-cbcaf39ef57f",
        "identification": "890903938",
        "branch_office": 0
      },
      "description": "BANCOLOMBIA 29800004279",
      "value": 141653.0
    }
  ],
  "metadata": {
    "created": "2026-05-19T09:36:38.983"
  }
}
```
