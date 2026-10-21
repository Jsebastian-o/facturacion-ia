"""Agente de consulta en lenguaje natural sobre la facturación.

Claude no ve la base de datos: solo puede llamar a las herramientas de abajo,
que leen del mismo servicio (y la misma caché) que usa el dashboard. Así las
cifras del chat y del tablero siempre coinciden.
"""
from __future__ import annotations

import json
from datetime import date

from .config import Settings
from .service import FacturacionService

MAX_VUELTAS = 6

TOOLS = [
    {
        "name": "resumen_facturacion",
        "description": (
            "Resumen oficial del tablero: facturación (COP) y peso (toneladas) del día anterior, "
            "acumulado del mes y acumulado del año, total y por categoría, más el peso de ACERO SIDOC "
            "y GR_CEMENTOS. Úsala para preguntas generales como '¿cuánto se facturó ayer?'."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "consultar_facturacion",
        "description": (
            "Consulta libre de facturación (COP) y peso (toneladas) en un rango de fechas, agrupada por "
            "categoria, tienda, tipo_cliente, dia, mes o ninguno (solo total). Permite filtrar por categoría, tienda o "
            "tipo de cliente (MAYORISTA, MINORISTA, CONSTRUCTORES; coincidencia parcial, sin importar mayúsculas). Solo hay datos hasta el día anterior. "
            "Los fines de semana y festivos ya vienen sumados al día hábil anterior. No existe detalle por cliente, vendedor ni producto."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "desde": {"type": "string", "description": "Fecha inicial AAAA-MM-DD (inclusive)."},
                "hasta": {"type": "string", "description": "Fecha final AAAA-MM-DD (inclusive)."},
                "agrupar_por": {"type": "string", "enum": ["categoria", "tienda", "tipo_cliente", "dia", "mes", "ninguno"]},
                "categoria": {"type": "string", "description": "Filtro opcional, p. ej. 'CEMENTO'."},
                "tienda": {"type": "string", "description": "Filtro opcional, p. ej. 'PEREIRA'."},
                "tipo_cliente": {"type": "string", "description": "Filtro opcional por canal: 'MAYORISTA', 'MINORISTA' o 'CONSTRUCTORES'."},
            },
            "required": ["desde", "hasta"],
        },
    },
]


def _system(hoy: date, peso_cats: list[str]) -> str:
    return f"""Eres el asistente de facturación de Sidoc (Siderúrgica del Occidente). Hoy es {hoy.isoformat()}.
Responde siempre en español, de forma breve y directa, en texto plano (sin markdown ni tablas).

Reglas del dato:
- Facturación = subtotal de documentos aprobados, en pesos colombianos (COP), neto de notas crédito.
- Solo hay datos hasta el día anterior ({hoy.isoformat()} no está incluido). Si preguntan por hoy, explícalo.
- Los sábados, domingos y festivos de Colombia se suman al día hábil anterior (viernes + sábado + domingo = viernes;
  con lunes festivo, también el lunes). Por eso el "día anterior" de un lunes o de un día tras festivo es ese
  día hábil con el fin de semana incluido, y las consultas por día agrupan por día hábil.
- El peso está en toneladas. Las categorías con peso destacado son: {', '.join(peso_cats)}. PRODUCTOS NO SIDOC se reporta en dinero (COP), no en peso.
- Dimensiones disponibles: fecha, categoría, tienda y tipo de cliente (canal: MAYORISTA, MINORISTA, CONSTRUCTORES). No tienes cliente, vendedor ni producto; si lo piden, dilo.
- Usa SIEMPRE las herramientas para obtener cifras; nunca las inventes ni las estimes de memoria.
- Formatea los pesos con separador de miles (p. ej. $1.234.567.890) y las toneladas con un decimal.
- Si la consulta devuelve notas (ajustes de fechas), menciónalas si cambian la respuesta."""


def _ejecutar(servicio: FacturacionService, nombre: str, args: dict) -> str:
    try:
        if nombre == "resumen_facturacion":
            return json.dumps(servicio.resumen(), ensure_ascii=False)
        if nombre == "consultar_facturacion":
            res = servicio.consultar(
                desde=date.fromisoformat(args["desde"]),
                hasta=date.fromisoformat(args["hasta"]),
                agrupar_por=args.get("agrupar_por", "categoria"),
                categoria=args.get("categoria"),
                tienda=args.get("tienda"),
                tipo_cliente=args.get("tipo_cliente"),
            )
            return json.dumps(res, ensure_ascii=False)
        return json.dumps({"error": f"Herramienta desconocida: {nombre}"})
    except (ValueError, KeyError) as exc:
        return json.dumps({"error": str(exc)})


class AgenteNoConfigurado(RuntimeError):
    pass


def responder(servicio: FacturacionService, settings: Settings, historial: list[dict]) -> str:
    """historial: [{'role': 'user'|'assistant', 'content': str}, ...] terminando en un mensaje del usuario."""
    if not settings.anthropic_api_key:
        raise AgenteNoConfigurado("Falta ANTHROPIC_API_KEY en el archivo .env.")
    import anthropic

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    system = _system(servicio.hoy(), servicio._categorias_peso(servicio._datos(servicio.hoy()).df))
    mensajes = [{"role": m["role"], "content": m["content"]} for m in historial[-12:]]

    for _ in range(MAX_VUELTAS):
        resp = client.messages.create(
            model=settings.anthropic_model, max_tokens=1500,
            system=system, tools=TOOLS, messages=mensajes,
        )
        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text").strip()
        mensajes.append({"role": "assistant", "content": resp.content})
        resultados = [
            {"type": "tool_result", "tool_use_id": b.id, "content": _ejecutar(servicio, b.name, b.input)}
            for b in resp.content if b.type == "tool_use"
        ]
        mensajes.append({"role": "user", "content": resultados})
    return "No pude completar la consulta; intenta con una pregunta más específica."
