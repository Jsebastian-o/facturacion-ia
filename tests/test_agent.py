import json
from types import SimpleNamespace as NS

from app import agent
from tests.test_service import _settings, _svc


class ClienteFalso:
    """Simula a Claude: primero pide una herramienta y luego responde con su resultado."""
    def __init__(self):
        self.llamadas = []
        self.messages = self

    def create(self, **kw):
        self.llamadas.append(kw)
        if len(self.llamadas) == 1:
            bloque = NS(type="tool_use", id="t1", name="consultar_facturacion",
                        input={"desde": "2026-10-19", "hasta": "2026-10-19", "agrupar_por": "ninguno"})
            return NS(stop_reason="tool_use", content=[bloque])
        resultado = json.loads(kw["messages"][-1]["content"][0]["content"])
        return NS(stop_reason="end_turn", content=[NS(type="text", text=f"Ayer: {resultado['total']['valor']}")])


def test_agente_llama_herramienta_y_responde(monkeypatch):
    falso = ClienteFalso()
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: falso)
    s = _settings(anthropic_api_key="k")
    out = agent.responder(_svc(), s, [{"role": "user", "content": "¿cuánto se facturó ayer?"}])
    assert out == "Ayer: 457.0"
    assert "2026-10-20" in falso.llamadas[0]["system"]


def test_herramienta_con_error_devuelve_json_de_error():
    res = json.loads(agent._ejecutar(_svc(), "consultar_facturacion",
                                     {"desde": "2026-10-25", "hasta": "2026-10-26"}))
    assert "error" in res
    res = json.loads(agent._ejecutar(_svc(), "consultar_facturacion", {"desde": "mal", "hasta": "x"}))
    assert "error" in res
