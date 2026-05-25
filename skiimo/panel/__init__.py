"""Panel web admin para Esskimo Cocktails.

FastAPI app que sirve:
  - /login                login (user + password)
  - /                     dashboard (KPIs + chat embebido)
  - /api/kpis             JSON con los KPIs del dia/mes
  - /api/chat             POST mensaje, devuelve respuesta del agente
  - /static/...           CSS / JS / assets
"""
