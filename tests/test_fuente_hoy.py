from dataclasses import replace

from app.config import Settings, get_settings
from app.repository import SqlServerRepository


def _base() -> Settings:
    return replace(get_settings(), data_source="sql", db_server="DISENO1", db_name="Gestion_Comercial",
                   db_user="u", db_password="p", view_name="dbo.vw_VentasCompleto", db_trusted_connection=False,
                   hoy_db_server="", hoy_db_name="", hoy_db_user="", hoy_db_password="",
                   hoy_view_name="", hoy_db_trusted_connection=None)


def test_sin_config_hoy_usa_la_misma_fuente():
    s = _base()
    assert not s.hoy_separado
    assert s.para_hoy() is s


def test_hoy_solo_cambia_base_y_hereda_el_resto():
    s = replace(_base(), hoy_db_name="bd_desarrollo")
    h = s.para_hoy()
    assert s.hoy_separado
    assert (h.db_server, h.db_name, h.db_user, h.db_password, h.view_name) == \
        ("DISENO1", "bd_desarrollo", "u", "p", "dbo.vw_VentasCompleto")
    assert s.db_name == "Gestion_Comercial"  # la principal no cambia


def test_hoy_con_otro_servidor_y_credenciales():
    s = replace(_base(), hoy_db_server="OTRO", hoy_db_name="bd_desarrollo", hoy_db_user="x", hoy_db_password="y",
                hoy_view_name="dbo.FactVentas")
    h = s.para_hoy()
    assert (h.db_server, h.db_name, h.db_user, h.db_password, h.view_name) == \
        ("OTRO", "bd_desarrollo", "x", "y", "dbo.FactVentas")
    cs = SqlServerRepository(h)._conn_str()
    assert "SERVER=OTRO" in cs and "DATABASE=bd_desarrollo" in cs and "UID=x" in cs


def test_en_modo_csv_hoy_no_se_separa():
    s = replace(_base(), data_source="csv", hoy_db_name="bd_desarrollo")
    assert not s.hoy_separado and s.para_hoy() is s


def test_api_hoy_usa_su_servicio(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main

    llamadas = []

    class Falso:
        def __init__(self, nombre): self.nombre = nombre
        def resumen(self, corte=None, en_curso=False):
            llamadas.append((self.nombre, en_curso)); return {"ok": self.nombre}
        def tendencia(self, dias=30, corte=None, en_curso=False):
            llamadas.append((self.nombre, en_curso)); return []
        def estado(self): return {}
        def refrescar(self): llamadas.append((self.nombre, "refresh"))
        def iniciar_refresco(self, _): pass

    principal, hoy = Falso("principal"), Falso("hoy")
    monkeypatch.setattr(main, "get_service", lambda: principal)
    monkeypatch.setattr(main, "get_service_hoy", lambda: hoy)
    c = TestClient(main.app)
    assert c.get("/api/resumen?vista=ayer").json() == {"ok": "principal"}
    assert c.get("/api/resumen?vista=hoy").json() == {"ok": "hoy"}
    c.get("/api/tendencia?vista=hoy"); c.post("/api/refresh")
    assert ("hoy", True) in llamadas and ("principal", "refresh") in llamadas and ("hoy", "refresh") in llamadas
