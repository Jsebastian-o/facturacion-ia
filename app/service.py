"""Cálculo de métricas de facturación.

Reglas:
- Facturación = suma de ValorSubtotalLocal de documentos en estado válido.
  Las notas crédito ya vienen en negativo en la vista, así que la suma es neta.
- Los sábados, domingos y festivos (calendario de Colombia) se suman al día
  hábil anterior. Viernes + sábado + domingo = "viernes"; si el lunes es festivo,
  viernes + sábado + domingo + lunes = "viernes".
- Vista "Hoy": igual que lo anterior pero terminando en hoy (el día en curso, parcial), con el acumulado
  del mes y del año incluyéndolo. Es solo para seguir el día; el cierre oficial es "día anterior".
- "Día anterior" = el día hábil al que pertenece ayer (hora de Bogotá). Nunca se
  incluye hoy, porque el día en curso está incompleto.
- Acumulado del mes = del día 1 del mes de ese día hábil hasta ayer.
- Acumulado del año = del 1 de enero hasta ayer.
- Peso: la vista trae kg; se reporta en toneladas.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import holidays
import pandas as pd

from .config import Settings
from .repository import Repository

log = logging.getLogger("facturacion")
TZ = ZoneInfo("America/Bogota")
KG_POR_TON = 1000.0


class CalendarioHabil:
    """Lunes a viernes que no sean festivo en Colombia (incluye los festivos trasladados a lunes)."""

    def __init__(self):
        self._festivos = holidays.country_holidays("CO")  # se expande sola por año

    def es_habil(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self._festivos

    def habil_anterior(self, d: date) -> date:
        """d si es día hábil; si no, el día hábil inmediatamente anterior."""
        while not self.es_habil(d):
            d -= timedelta(days=1)
        return d


@dataclass
class _Cache:
    df: pd.DataFrame
    hoy: date
    cargado_en: datetime
    fecha_carga: datetime | None
    expira: float
    aviso: str | None = None


def _vp(valor: float, peso_kg: float) -> dict:
    return {"valor": round(float(valor), 2), "peso_ton": round(float(peso_kg) / KG_POR_TON, 3)}


class FacturacionService:
    def __init__(self, repo: Repository, settings: Settings):
        self.repo = repo
        self.settings = settings
        self.cal = CalendarioHabil()
        self._cache: _Cache | None = None
        self._carga = threading.Lock()   # serializa las cargas; las lecturas con caché vigente no esperan
        self._segundo_plano = False
        self._hilo: threading.Thread | None = None

    # ------------------------------------------------------------------ datos
    def hoy(self) -> date:
        return self.settings.fecha_hoy or datetime.now(TZ).date()

    def _a_dias_habiles(self, df: pd.DataFrame) -> pd.DataFrame:
        """Fecha pasa a ser el día hábil al que pertenece la venta; FechaReal conserva la fecha del documento."""
        df = df.copy()
        df["Fecha"] = pd.to_datetime(df["Fecha"])
        df["FechaReal"] = df["Fecha"]
        if df.empty:
            return df
        mapa = {f: pd.Timestamp(self.cal.habil_anterior(f.date()))
                for f in pd.to_datetime(df["Fecha"].unique())}
        df["Fecha"] = df["Fecha"].map(mapa)
        return df

    def _vigente(self, c: _Cache | None, hoy: date) -> bool:
        # Con el refresco en segundo plano la caché nunca "vence" para quien consulta.
        return c is not None and c.hoy == hoy and (self._segundo_plano or time.monotonic() < c.expira)

    def _datos(self, hoy: date, forzar: bool = False) -> _Cache:
        c = self._cache
        if not forzar and self._vigente(c, hoy):
            return c  # type: ignore[return-value]
        with self._carga:
            c = self._cache
            if not forzar and self._vigente(c, hoy):
                return c  # type: ignore[return-value]
            # Se carga desde el 1 de enero del año anterior para poder comparar años.
            desde = date(hoy.year - 1, 1, 1)
            try:
                # hasta es exclusivo: se incluye hoy para la vista "Hoy"; los cálculos de
                # "día anterior", mes y año lo excluyen porque filtran por fecha real <= ayer.
                df, fecha_carga = self.repo.cargar(desde, hoy + timedelta(days=1))
                self._cache = _Cache(self._a_dias_habiles(df), hoy, datetime.now(TZ), fecha_carga,
                                     time.monotonic() + self.settings.cache_ttl_seconds)
            except Exception as exc:  # noqa: BLE001
                if c is None:
                    raise
                # Servidor caído: se siguen mostrando los últimos datos buenos y se reintenta en 60 s.
                c.aviso = (f"No se pudo actualizar desde el servidor; se muestran los datos "
                           f"cargados a las {c.cargado_en:%H:%M} ({type(exc).__name__}).")
                c.expira = time.monotonic() + 60
            return self._cache  # type: ignore[return-value]

    def refrescar(self) -> None:
        self._datos(self.hoy(), forzar=True)

    def iniciar_refresco(self, intervalo: int) -> None:
        """Recarga la vista cada `intervalo` segundos en un hilo aparte: las páginas se sirven desde memoria."""
        if self._hilo is not None:
            return
        self._segundo_plano = True

        def bucle():
            while True:
                try:
                    self.refrescar()
                except Exception:  # noqa: BLE001
                    log.exception("Falló la recarga en segundo plano; se reintenta")
                time.sleep(max(intervalo, 5))

        self._hilo = threading.Thread(target=bucle, name="refresco-facturacion", daemon=True)
        self._hilo.start()

    # --------------------------------------------------------------- utilidades
    def periodos(self, corte: date) -> dict[str, tuple[date, date]]:
        """(desde, hasta) de cada período. `hasta` es siempre ayer; `desde` del día es el día hábil
        al que pertenece ayer (si es anterior a ayer, el período incluye fin de semana o festivos)."""
        ayer = corte - timedelta(days=1)
        r = self.cal.habil_anterior(ayer)
        return {
            "dia": (r, ayer),
            "mes": (r.replace(day=1), ayer),
            "anio": (date(r.year, 1, 1), ayer),
        }

    @staticmethod
    def _rango(df: pd.DataFrame, desde: date, hasta: date) -> pd.DataFrame:
        """Ventas cuyo día hábil es >= desde y cuya fecha real es <= hasta."""
        return df[(df["Fecha"] >= pd.Timestamp(desde)) & (df["FechaReal"] <= pd.Timestamp(hasta))]

    @staticmethod
    def _por_prefijo(df: pd.DataFrame, patrones) -> list[str]:
        """Categorías reales de la vista que coinciden (por prefijo, sin importar mayúsculas) con los patrones."""
        existentes = sorted(df["Categoria"].unique())
        elegidas: list[str] = []
        for patron in patrones:
            hallazgos = [c for c in existentes if c.upper().startswith(patron)]
            elegidas += hallazgos if hallazgos else [patron]
        return list(dict.fromkeys(elegidas))

    def _categorias_peso(self, df: pd.DataFrame) -> list[str]:
        return self._por_prefijo(df, self.settings.categorias_peso)

    def _categorias_valor(self, df: pd.DataFrame) -> list[str]:
        return self._por_prefijo(df, self.settings.categorias_valor)

    # ------------------------------------------------------------------ resumen
    def resumen(self, corte: date | None = None, en_curso: bool = False) -> dict:
        """en_curso=True: la misma información pero con el día de hoy (parcial) como "día" del período,
        y el acumulado del mes y del año incluyéndolo."""
        corte = corte or self.hoy()
        if en_curso:
            corte += timedelta(days=1)
        cache = self._datos(self.hoy())
        df = cache.df
        per = self.periodos(corte)

        por_cat: dict[str, dict] = {}
        totales: dict[str, dict] = {}
        for nombre, (desde, hasta) in per.items():
            sub = self._rango(df, desde, hasta)
            totales[nombre] = _vp(sub["Valor"].sum(), sub["Peso"].sum())
            for cat, g in sub.groupby("Categoria"):
                por_cat.setdefault(cat, {})[nombre] = _vp(g["Valor"].sum(), g["Peso"].sum())

        vacio = _vp(0, 0)
        categorias = [
            {"categoria": cat, **{p: datos.get(p, vacio) for p in per}}
            for cat, datos in por_cat.items()
        ]
        categorias.sort(key=lambda x: x["anio"]["valor"], reverse=True)

        peso_foco = []
        for cat in self._categorias_peso(df):
            fila = {"categoria": cat}
            for nombre in per:
                fila[nombre] = por_cat.get(cat, {}).get(nombre, vacio)["peso_ton"]
            peso_foco.append(fila)

        # Categorías que se muestran en dinero (COP): no se suman con las de peso.
        valor_foco = []
        for cat in self._categorias_valor(df):
            fila = {"categoria": cat}
            for nombre in per:
                fila[nombre] = por_cat.get(cat, {}).get(nombre, vacio)["valor"]
            valor_foco.append(fila)

        dia_desde, dia_hasta = per["dia"]
        return {
            "fecha_corte": corte.isoformat(),
            "periodos": {k: {"desde": d.isoformat(), "hasta": h.isoformat()} for k, (d, h) in per.items()},
            "en_curso": en_curso,
            "dia_incluye_no_habiles": dia_desde != dia_hasta,
            "total": totales,
            "categorias": categorias,
            "peso_foco": peso_foco,
            "valor_foco": valor_foco,
            "sin_facturacion_dia": totales["dia"]["valor"] == 0 and totales["dia"]["peso_ton"] == 0,
            "datos_cargados_en": cache.cargado_en.isoformat(timespec="seconds"),
            "fecha_carga_vista": cache.fecha_carga.isoformat(timespec="seconds") if cache.fecha_carga else None,
            "aviso": cache.aviso,
        }

    def tendencia(self, dias: int = 30, corte: date | None = None, en_curso: bool = False) -> list[dict]:
        """Serie por día hábil (los fines de semana y festivos ya están sumados al día hábil anterior).
        en_curso=True termina en hoy (parcial) en lugar de ayer."""
        corte = corte or self.hoy()
        if en_curso:
            corte += timedelta(days=1)
        dias = max(1, min(int(dias), 366))
        cache = self._datos(self.hoy())
        ayer = corte - timedelta(days=1)
        desde = ayer - timedelta(days=dias - 1)
        sub = self._rango(cache.df, desde, ayer)
        foco = self._categorias_peso(cache.df)
        total = sub.groupby("Fecha")["Valor"].sum()
        peso_foco = sub[sub["Categoria"].isin(foco)].groupby("Fecha")["Peso"].sum()
        salida = []
        for d in pd.date_range(desde, ayer):
            if not self.cal.es_habil(d.date()):
                continue
            salida.append({
                "fecha": d.date().isoformat(),
                "valor": round(float(total.get(d, 0.0)), 2),
                "peso_foco_ton": round(float(peso_foco.get(d, 0.0)) / KG_POR_TON, 3),
            })
        return salida

    # --------------------------------------------------- consulta libre (agente)
    def consultar(self, desde: date, hasta: date, agrupar_por: str = "categoria",
                  categoria: str | None = None, tienda: str | None = None,
                  limite: int = 50) -> dict:
        cache = self._datos(self.hoy())
        df = cache.df
        ayer = self.hoy() - timedelta(days=1)
        notas: list[str] = []

        if hasta > ayer:
            notas.append(f"hasta se ajustó a {ayer} (no hay datos del día en curso).")
            hasta = ayer
        inicio = date(self.hoy().year - 1, 1, 1)
        if desde < inicio:
            notas.append(f"desde se ajustó a {inicio} (inicio del historial cargado).")
            desde = inicio
        if desde > hasta:
            raise ValueError("El rango de fechas es inválido o queda fuera de los datos disponibles.")
        if not self.cal.es_habil(desde):
            notas.append(f"{desde} no es día hábil: su facturación se suma al día hábil anterior "
                         f"({self.cal.habil_anterior(desde)}), que queda fuera del rango.")
        if not self.cal.es_habil(hasta):
            notas.append(f"{hasta} no es día hábil: su facturación está sumada al día hábil anterior "
                         f"({self.cal.habil_anterior(hasta)}).")

        sub = self._rango(df, desde, hasta)
        if categoria:
            sub = sub[sub["Categoria"].str.upper().str.contains(categoria.strip().upper(), regex=False)]
        if tienda:
            sub = sub[sub["Tienda"].str.upper().str.contains(tienda.strip().upper(), regex=False)]

        claves = {"categoria": sub["Categoria"], "tienda": sub["Tienda"],
                  "dia": sub["Fecha"].dt.strftime("%Y-%m-%d"), "mes": sub["Fecha"].dt.strftime("%Y-%m")}
        if agrupar_por not in claves and agrupar_por != "ninguno":
            raise ValueError("agrupar_por debe ser: categoria, tienda, dia, mes o ninguno.")

        filas = []
        if agrupar_por != "ninguno":
            g = sub.groupby(claves[agrupar_por])[["Valor", "Peso"]].sum()
            g = g.sort_index() if agrupar_por in ("dia", "mes") else g.sort_values("Valor", ascending=False)
            if len(g) > limite:
                notas.append(f"se muestran {limite} de {len(g)} grupos.")
                g = g.head(limite)
            filas = [{"grupo": k, **_vp(r.Valor, r.Peso)} for k, r in g.iterrows()]

        return {
            "desde": desde.isoformat(), "hasta": hasta.isoformat(), "agrupar_por": agrupar_por,
            "filtros": {"categoria": categoria, "tienda": tienda},
            "filas": filas,
            "total": _vp(sub["Valor"].sum(), sub["Peso"].sum()),
            "notas": notas,
        }

    def estado(self) -> dict:
        c = self._cache
        return {
            "datos_cargados_en": c.cargado_en.isoformat(timespec="seconds") if c else None,
            "filas_en_cache": int(len(c.df)) if c else 0,
            "refresco_en_segundo_plano": self._segundo_plano,
        }
