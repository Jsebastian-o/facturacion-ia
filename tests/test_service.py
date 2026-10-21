from datetime import date, datetime

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.repository import COLUMNAS, SqlServerRepository
from app.service import FacturacionService

HOY = date(2026, 10, 20)
CSV = "data/Muestra_vista.csv"


def _settings(**kw) -> Settings:
    base = dict(data_source="csv", csv_path=None, db_server="", db_name="", db_user="", db_password="",
                db_driver="x", db_trusted_connection=False, view_name="dbo.v", estados_validos=["Aprobado"],
                cache_ttl_seconds=300, categorias_peso=["ACERO SIDOC", "GR_CEMENTO"], fecha_hoy=HOY,
                anthropic_api_key="", anthropic_model="m", app_user="", app_password="")
    base.update(kw)
    return Settings(**base)


class RepoFalso:
    """Datos sintéticos para probar los límites de día / mes / año."""
    def __init__(self, filas):
        self.filas, self.llamadas = filas, 0

    def cargar(self, desde, hasta):
        self.llamadas += 1
        df = pd.DataFrame(self.filas, columns=COLUMNAS)
        df["Fecha"] = pd.to_datetime(df["Fecha"])
        df = df[(df.Fecha >= pd.Timestamp(desde)) & (df.Fecha < pd.Timestamp(hasta))]
        return df, datetime(2026, 10, 20, 3, 0)


FILAS = [
    ("2026-01-02", "ACERO SIDOC", "T1", 100.0, 1000.0),   # primer día hábil del año (1-ene es festivo)
    ("2026-09-30", "ACERO SIDOC", "T1", 200.0, 2000.0),   # mes anterior: solo cuenta en el año
    ("2026-10-01", "ACERO SIDOC", "T1", 300.0, 3000.0),   # primer día del mes
    ("2026-10-19", "ACERO SIDOC", "T2", 400.0, 4000.0),   # ayer
    ("2026-10-19", "GR_CEMENTOS", "T2", 50.0, 5000.0),    # ayer, cemento (plural en la vista)
    ("2026-10-19", "OTROS", "T2", 7.0, 70.0),
    ("2026-10-20", "ACERO SIDOC", "T1", 9999.0, 99999.0), # hoy: NO debe contar
    ("2025-12-31", "ACERO SIDOC", "T1", 888.0, 8000.0),   # año anterior: no cuenta en 2026
]


def _svc(filas=FILAS, hoy=HOY, **kw):
    return FacturacionService(RepoFalso(filas), _settings(fecha_hoy=hoy, **kw))


def test_limites_de_periodos():
    r = _svc().resumen()
    assert r["total"]["dia"] == {"valor": 457.0, "peso_ton": 9.07}
    assert r["total"]["mes"]["valor"] == 300 + 400 + 50 + 7          # excluye 30-sep y hoy
    assert r["total"]["anio"]["valor"] == 100 + 200 + 300 + 400 + 50 + 7
    assert r["periodos"]["mes"] == {"desde": "2026-10-01", "hasta": "2026-10-19"}
    assert r["periodos"]["anio"]["desde"] == "2026-01-01"
    assert r["dia_incluye_no_habiles"] is False


def test_peso_foco_en_toneladas_y_prefijo_cemento():
    r = _svc().resumen()
    foco = {f["categoria"]: f for f in r["peso_foco"]}
    assert set(foco) == {"ACERO SIDOC", "GR_CEMENTOS"}                # "GR_CEMENTO" casa con "GR_CEMENTOS"
    assert foco["ACERO SIDOC"]["dia"] == 4.0
    assert foco["ACERO SIDOC"]["mes"] == 7.0
    assert foco["ACERO SIDOC"]["anio"] == 10.0
    assert foco["GR_CEMENTOS"]["dia"] == 5.0


def test_dia_sin_facturacion():
    r = _svc([f for f in FILAS if f[0] != "2026-10-19"]).resumen()
    assert r["sin_facturacion_dia"] is True
    assert r["total"]["mes"]["valor"] == 300


def test_cache_y_refresco():
    s = _svc()
    s.resumen(); s.resumen(); s.tendencia()
    assert s.repo.llamadas == 1
    s.refrescar()
    assert s.repo.llamadas == 2


def test_servidor_caido_usa_ultimos_datos():
    s = _svc()
    s.resumen()
    s.repo.cargar = lambda d, h: (_ for _ in ()).throw(ConnectionError("sin red"))
    s.refrescar()
    r = s.resumen()
    assert r["total"]["dia"]["valor"] == 457.0
    assert "No se pudo actualizar" in r["aviso"]


def test_consultar_ajusta_fechas_y_filtra():
    s = _svc()
    r = s.consultar(date(2026, 10, 1), date(2026, 10, 31), "categoria", categoria="cemento")
    assert r["hasta"] == "2026-10-19" and r["notas"]
    assert r["total"] == {"valor": 50.0, "peso_ton": 5.0}
    r = s.consultar(date(2026, 1, 1), date(2026, 10, 19), "mes")
    assert [f["grupo"] for f in r["filas"]] == ["2026-01", "2026-09", "2026-10"]
    with pytest.raises(ValueError):
        s.consultar(date(2026, 10, 25), date(2026, 10, 26))


def test_tendencia_solo_dias_habiles():
    t = _svc().tendencia(dias=5)  # 15-oct (jue) a 19-oct (lun); sábado y domingo no aparecen
    assert [x["fecha"] for x in t] == ["2026-10-15", "2026-10-16", "2026-10-19"]
    assert t[0]["valor"] == 0 and t[2]["valor"] == 457.0
    assert t[2]["peso_foco_ton"] == 9.0


def test_vista_invalida_se_rechaza():
    with pytest.raises(ValueError):
        SqlServerRepository(_settings(view_name="dbo.v; DROP TABLE x"))


# ---------------------------------------------------------- con la muestra real
def _esperado_muestra():
    """Cálculo independiente directo sobre el CSV, sin usar el código de la app."""
    df = pd.read_csv(CSV, sep=";", encoding="utf-8-sig", dtype=str)
    df = df[df["Estado"].str.strip() == "Aprobado"]
    df = df[df["Fecha"] == "2026-10-19"]  # ayer
    v = df["ValorSubtotalLocal"].astype(float)
    p = df["Peso"].astype(float)
    cat = df["Categoria"].str.strip()
    return v.sum(), p.sum() / 1000, (v[cat == "ACERO SIDOC"].sum(), p[cat == "ACERO SIDOC"].sum() / 1000,
                                     p[cat == "GR_CEMENTOS"].sum() / 1000)


def test_muestra_coincide_con_calculo_independiente(monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "csv"); monkeypatch.setenv("FECHA_HOY", "2026-10-20")
    get_settings.cache_clear()
    from app.repository import crear_repositorio
    s = get_settings()
    r = FacturacionService(crear_repositorio(s), s).resumen()
    valor, ton, (acero_v, acero_t, cem_t) = _esperado_muestra()
    assert r["total"]["dia"]["valor"] == pytest.approx(valor, abs=0.01)
    assert r["total"]["dia"]["peso_ton"] == pytest.approx(ton, abs=0.001)
    cats = {c["categoria"]: c for c in r["categorias"]}
    assert cats["ACERO SIDOC"]["dia"]["valor"] == pytest.approx(acero_v, abs=0.01)
    foco = {f["categoria"]: f for f in r["peso_foco"]}
    assert foco["ACERO SIDOC"]["dia"] == pytest.approx(acero_t, abs=0.001)
    assert foco["GR_CEMENTOS"]["dia"] == pytest.approx(cem_t, abs=0.001)
    get_settings.cache_clear()


def test_api_y_acceso(monkeypatch):
    monkeypatch.setenv("DATA_SOURCE", "csv"); monkeypatch.setenv("FECHA_HOY", "2026-10-20")
    monkeypatch.setenv("APP_USER", "sidoc"); monkeypatch.setenv("APP_PASSWORD", "clave")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    from app.main import app, get_service
    get_service.cache_clear()
    c = TestClient(app)
    assert c.get("/api/health").json()["ok"] is True
    assert c.get("/api/resumen").status_code == 401
    assert c.get("/").status_code == 401
    ok = c.get("/api/resumen", auth=("sidoc", "clave"))
    assert ok.status_code == 200 and ok.json()["total"]["dia"]["valor"] > 0
    assert c.get("/api/tendencia?dias=7", auth=("sidoc", "clave")).status_code == 200
    assert c.get("/api/resumen?fecha_corte=mal", auth=("sidoc", "clave")).status_code == 422
    assert c.get("/", auth=("sidoc", "clave")).status_code == 200
    chat = c.post("/api/chat", auth=("sidoc", "clave"), json={"mensajes": [{"role": "user", "content": "hola"}]})
    assert chat.status_code == 503
    get_settings.cache_clear(); get_service.cache_clear()


# ------------------------------------------------ fines de semana y festivos
def _f(fecha, valor):
    return (fecha, "ACERO SIDOC", "T1", float(valor), float(valor) * 10)


def test_lunes_suma_viernes_sabado_y_domingo():
    filas = [_f("2026-10-15", 1000), _f("2026-10-16", 100), _f("2026-10-17", 10), _f("2026-10-18", 1)]
    r = _svc(filas, hoy=date(2026, 10, 19)).resumen()           # hoy lunes: ayer fue domingo
    assert r["total"]["dia"]["valor"] == 111                      # vie + sáb + dom
    assert r["periodos"]["dia"] == {"desde": "2026-10-16", "hasta": "2026-10-18"}
    assert r["dia_incluye_no_habiles"] is True
    assert r["total"]["mes"]["valor"] == 1111
    assert r["peso_foco"][0]["dia"] == 1.11


def test_lunes_festivo_suma_sabado_domingo_y_lunes_al_viernes():
    filas = [_f("2026-10-08", 100), _f("2026-10-09", 5), _f("2026-10-10", 1), _f("2026-10-11", 1),
             _f("2026-10-12", 1), _f("2026-10-13", 20)]          # 12-oct-2026 (lunes) es festivo
    r = _svc(filas, hoy=date(2026, 10, 13)).resumen()            # hoy martes: ayer fue el lunes festivo
    assert r["total"]["dia"]["valor"] == 8
    assert r["periodos"]["dia"] == {"desde": "2026-10-09", "hasta": "2026-10-12"}
    r = _svc(filas, hoy=date(2026, 10, 14)).resumen()            # hoy miércoles: ayer fue martes hábil
    assert r["total"]["dia"]["valor"] == 20 and r["dia_incluye_no_habiles"] is False
    assert r["total"]["mes"]["valor"] == 100 + 8 + 20


def test_festivo_entre_semana_se_suma_al_dia_anterior():
    filas = [_f("2026-12-23", 100), _f("2026-12-24", 10), _f("2026-12-25", 5)]   # 25-dic-2026 es viernes
    r = _svc(filas, hoy=date(2026, 12, 26)).resumen()
    assert r["total"]["dia"]["valor"] == 15
    assert r["periodos"]["dia"] == {"desde": "2026-12-24", "hasta": "2026-12-25"}


def test_fin_de_semana_pertenece_al_mes_del_dia_habil_anterior():
    filas = [_f("2026-10-01", 100), _f("2026-10-30", 10), _f("2026-10-31", 2), _f("2026-11-01", 3),
             _f("2026-11-02", 4), _f("2026-11-03", 50)]          # 2-nov-2026 festivo; 31-oct sábado, 1-nov domingo
    r = _svc(filas, hoy=date(2026, 11, 3)).resumen()
    assert r["total"]["dia"]["valor"] == 19                      # viernes 30 + sáb + dom + lunes festivo
    assert r["periodos"]["mes"]["desde"] == "2026-10-01"         # sigue siendo octubre
    assert r["total"]["mes"]["valor"] == 119
    r = _svc(filas, hoy=date(2026, 11, 4)).resumen()             # ya martes hábil: noviembre arranca el 3
    assert r["total"]["dia"]["valor"] == 50 and r["total"]["mes"]["valor"] == 50
    assert r["total"]["anio"]["valor"] == 100 + 19 + 50


def test_consultar_por_dia_agrupa_por_dia_habil():
    filas = [_f("2026-10-15", 1000), _f("2026-10-16", 100), _f("2026-10-17", 10), _f("2026-10-18", 1)]
    r = _svc(filas, hoy=date(2026, 10, 19)).consultar(date(2026, 10, 15), date(2026, 10, 18), "dia")
    assert [(f["grupo"], f["valor"]) for f in r["filas"]] == [("2026-10-15", 1000.0), ("2026-10-16", 111.0)]
    assert any("no es día hábil" in n for n in r["notas"])


def test_refresco_en_segundo_plano_no_bloquea_ni_recarga_por_peticion():
    s = _svc(cache_ttl_seconds=0)                                # TTL 0: sin hilo, cada consulta recargaría
    s.iniciar_refresco(3600)
    s.resumen(); s.resumen(); s.tendencia()
    assert s.repo.llamadas == 1                                  # solo la carga inicial del hilo
    assert s.estado()["refresco_en_segundo_plano"] is True
