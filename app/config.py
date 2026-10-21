from __future__ import annotations

import os
from dataclasses import dataclass
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
    )
