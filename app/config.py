from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import date
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _lista(raw: str) -> list[str]:
    return [x.strip() for x in raw.split(",") if x.strip()]


def _bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "si", "sí"}


@dataclass(frozen=True)
class Settings:
    data_source: str
    csv_path: Path
    db_server: str
    db_name: str
    db_user: str
    db_password: str
    db_driver: str
    db_trusted_connection: bool
    view_name: str
    estados_validos: list[str]
    cache_ttl_seconds: int
    categorias_peso: list[str]
    fecha_hoy: date | None
    anthropic_api_key: str
    anthropic_model: str
    app_user: str
    app_password: str
    categorias_valor: tuple[str, ...] = ("PRODUCTOS NO SIDOC",)
    canales: tuple[str, ...] = ("MAYORISTA", "MINORISTA", "CONSTRUCTORES")
    # Fuente propia de la pestaña "Hoy" (vacío = usa la misma que el resto del tablero)
    hoy_db_server: str = ""
    hoy_db_name: str = ""
    hoy_db_user: str = ""
    hoy_db_password: str = ""
    hoy_view_name: str = ""
    hoy_db_trusted_connection: bool | None = None

    @property
    def hoy_separado(self) -> bool:
        return self.data_source == "sql" and bool(self.hoy_db_server or self.hoy_db_name)

    def para_hoy(self) -> "Settings":
        """Configuración de la fuente de la pestaña Hoy: lo que no se defina se hereda de la principal."""
        if not self.hoy_separado:
            return self
        trusted = self.db_trusted_connection if self.hoy_db_trusted_connection is None else self.hoy_db_trusted_connection
        return replace(
            self,
            db_server=self.hoy_db_server or self.db_server,
            db_name=self.hoy_db_name or self.db_name,
            db_user=self.hoy_db_user or self.db_user,
            db_password=self.hoy_db_password or self.db_password,
            view_name=self.hoy_view_name or self.view_name,
            db_trusted_connection=trusted,
        )


@lru_cache
def get_settings() -> Settings:
    e = os.getenv
    csv_path = Path(e("CSV_PATH", "data/Muestra_vista.csv"))
    if not csv_path.is_absolute():
        csv_path = BASE_DIR / csv_path
    fecha_hoy = e("FECHA_HOY", "").strip()
    return Settings(
        data_source=e("DATA_SOURCE", "sql").strip().lower(),
        csv_path=csv_path,
        db_server=e("DB_SERVER", ""),
        db_name=e("DB_NAME", ""),
        db_user=e("DB_USER", ""),
        db_password=e("DB_PASSWORD", ""),
        db_driver=e("DB_DRIVER", "ODBC Driver 18 for SQL Server"),
        db_trusted_connection=_bool(e("DB_TRUSTED_CONNECTION", "false")),
        view_name=e("VIEW_NAME", "dbo.NOMBRE_DE_LA_VISTA").strip(),
        estados_validos=_lista(e("ESTADOS_VALIDOS", "Aprobado")),
        cache_ttl_seconds=int(e("CACHE_TTL_SECONDS", "300")),
        categorias_peso=[c.upper() for c in _lista(e("CATEGORIAS_PESO", "ACERO SIDOC,GR_CEMENTO"))],
        fecha_hoy=date.fromisoformat(fecha_hoy) if fecha_hoy else None,
        anthropic_api_key=e("ANTHROPIC_API_KEY", ""),
        anthropic_model=e("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
        app_user=e("APP_USER", ""),
        app_password=e("APP_PASSWORD", ""),
        categorias_valor=tuple(c.upper() for c in _lista(e("CATEGORIAS_VALOR", "PRODUCTOS NO SIDOC"))),
        canales=tuple(c.upper() for c in _lista(e("CANALES", "MAYORISTA,MINORISTA,CONSTRUCTORES"))),
        hoy_db_server=e("HOY_DB_SERVER", "").strip(),
        hoy_db_name=e("HOY_DB_NAME", "").strip(),
        hoy_db_user=e("HOY_DB_USER", "").strip(),
        hoy_db_password=e("HOY_DB_PASSWORD", ""),
        hoy_view_name=e("HOY_VIEW_NAME", "").strip(),
        hoy_db_trusted_connection=_bool(e("HOY_DB_TRUSTED_CONNECTION", "")) if e("HOY_DB_TRUSTED_CONNECTION", "").strip() else None,
    )
