"""Acceso a datos.

Ambos repositorios devuelven la facturación ya agregada a nivel
(Fecha, Categoria, Tienda) con las columnas Valor y Peso, más la fecha de la
última carga de la vista. Así no se traen las líneas de factura al servicio web.

El acceso a SQL Server es un único SELECT parametrizado (no requiere permisos
de escritura ni procedimientos almacenados).
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

import pandas as pd

from .config import Settings

COLUMNAS = ["Fecha", "Categoria", "Tienda", "Valor", "Peso"]
_VISTA_VALIDA = re.compile(r"^[A-Za-z0-9_\.\[\]]+$")


class Repository(Protocol):
    def cargar(self, desde: date, hasta: date) -> tuple[pd.DataFrame, datetime | None]:
        """Facturación con desde <= Fecha < hasta."""


def _normalizar(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["Fecha"] = pd.to_datetime(df["Fecha"]).dt.normalize()
    for col in ("Categoria", "Tienda"):
        df[col] = df[col].fillna("").astype(str).str.strip()
    df.loc[df["Categoria"] == "", "Categoria"] = "SIN CATEGORIA"
    df.loc[df["Tienda"] == "", "Tienda"] = "SIN TIENDA"
    df["Valor"] = pd.to_numeric(df["Valor"], errors="coerce").fillna(0.0).astype(float)
    df["Peso"] = pd.to_numeric(df["Peso"], errors="coerce").fillna(0.0).astype(float)
    return df[COLUMNAS].reset_index(drop=True)


class SqlServerRepository:
    def __init__(self, settings: Settings):
        if not _VISTA_VALIDA.match(settings.view_name):
            raise ValueError(f"VIEW_NAME no es un identificador válido: {settings.view_name!r}")
        self.s = settings

    def _conn_str(self) -> str:
        s = self.s
        partes = [f"DRIVER={{{s.db_driver}}}", f"SERVER={s.db_server}", f"DATABASE={s.db_name}",
                  "TrustServerCertificate=yes", "Application Name=facturacion_web"]
        if s.db_trusted_connection:
            partes.append("Trusted_Connection=yes")
        else:
            partes += [f"UID={s.db_user}", f"PWD={s.db_password}"]
        return ";".join(partes)

    def cargar(self, desde: date, hasta: date) -> tuple[pd.DataFrame, datetime | None]:
        import pyodbc  # import perezoso: el modo CSV no necesita ODBC

        estados = self.s.estados_validos
        marcas = ",".join("?" for _ in estados)
        sql = f"""
            SELECT CAST(Fecha AS date)                              AS Fecha,
                   LTRIM(RTRIM(Categoria))                          AS Categoria,
                   LTRIM(RTRIM(Tienda))                             AS Tienda,
                   SUM(ValorSubtotalLocal)                          AS Valor,
                   SUM(Peso)                                        AS Peso,
                   MAX(FechaCarga)                                  AS FechaCarga
            FROM {self.s.view_name}
            WHERE Fecha >= ? AND Fecha < ? AND LTRIM(RTRIM(Estado)) IN ({marcas})
            GROUP BY CAST(Fecha AS date), LTRIM(RTRIM(Categoria)), LTRIM(RTRIM(Tienda))
        """
        params = [desde, hasta, *estados]
        with pyodbc.connect(self._conn_str(), timeout=30) as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            filas = cur.fetchall()
            nombres = [c[0] for c in cur.description]
        df = pd.DataFrame([tuple(f) for f in filas], columns=nombres)
        if df.empty:
            return pd.DataFrame(columns=COLUMNAS), None
        fecha_carga = pd.to_datetime(df["FechaCarga"]).max()
        return _normalizar(df), fecha_carga.to_pydatetime()


class CsvRepository:
    """Lee la muestra exportada de la vista (separador ';'). Solo para desarrollo."""

    def __init__(self, settings: Settings):
        self.path: Path = settings.csv_path
        self.estados = [e.lower() for e in settings.estados_validos]

    def cargar(self, desde: date, hasta: date) -> tuple[pd.DataFrame, datetime | None]:
        raw = pd.read_csv(self.path, sep=";", encoding="utf-8-sig", dtype=str)
        raw["Fecha"] = pd.to_datetime(raw["Fecha"].str.strip()).dt.normalize()
        raw["Estado"] = raw["Estado"].fillna("").str.strip().str.lower()
        for col in ("Categoria", "Tienda"):
            raw[col] = raw[col].fillna("").str.strip()
        raw["Valor"] = pd.to_numeric(raw["ValorSubtotalLocal"], errors="coerce").fillna(0.0)
        raw["Peso"] = pd.to_numeric(raw["Peso"], errors="coerce").fillna(0.0)
        mask = (raw["Fecha"] >= pd.Timestamp(desde)) & (raw["Fecha"] < pd.Timestamp(hasta)) \
            & raw["Estado"].isin(self.estados)
        sel = raw.loc[mask]
        if sel.empty:
            return pd.DataFrame(columns=COLUMNAS), None
        fecha_carga = pd.to_datetime(sel["FechaCarga"]).max().to_pydatetime()
        agg = sel.groupby(["Fecha", "Categoria", "Tienda"], as_index=False)[["Valor", "Peso"]].sum()
        return _normalizar(agg), fecha_carga


def crear_repositorio(settings: Settings) -> Repository:
    if settings.data_source == "csv":
        return CsvRepository(settings)
    if settings.data_source == "sql":
        return SqlServerRepository(settings)
    raise ValueError(f"DATA_SOURCE debe ser 'sql' o 'csv', no {settings.data_source!r}")
