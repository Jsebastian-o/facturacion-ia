from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from datetime import date
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from . import agent
from .config import get_settings
from .repository import crear_repositorio
from .service import FacturacionService

log = logging.getLogger("facturacion")
STATIC = Path(__file__).parent / "static"

@asynccontextmanager
async def lifespan(_: FastAPI):
    # Carga inicial y recargas periódicas en segundo plano: las páginas se sirven siempre desde memoria.
    ttl = get_settings().cache_ttl_seconds
    get_service().iniciar_refresco(ttl)
    if get_service_hoy() is not get_service():
        get_service_hoy().iniciar_refresco(ttl)
    yield


app = FastAPI(title="Facturación Sidoc", docs_url=None, redoc_url=None, lifespan=lifespan)
_basic = HTTPBasic(auto_error=False)


@lru_cache
def get_service() -> FacturacionService:
    s = get_settings()
    return FacturacionService(crear_repositorio(s), s)


@lru_cache
def get_service_hoy() -> FacturacionService:
    """Servicio de la pestaña Hoy: usa su propia base si HOY_DB_* está definido; si no, el principal."""
    s = get_settings()
    if not s.hoy_separado:
        return get_service()
    sh = s.para_hoy()
    return FacturacionService(crear_repositorio(sh), sh)


def _servicio(en_curso: bool) -> FacturacionService:
    return get_service_hoy() if en_curso else get_service()


def requiere_acceso(cred: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    s = get_settings()
    if not s.app_user:  # sin usuario configurado, la app queda abierta (solo para pruebas locales)
        return
    ok = cred is not None \
        and secrets.compare_digest(cred.username.encode(), s.app_user.encode()) \
        and secrets.compare_digest(cred.password.encode(), s.app_password.encode())
    if not ok:
        raise HTTPException(401, "Credenciales inválidas", headers={"WWW-Authenticate": "Basic"})


def _corte(fecha_corte: str | None) -> date | None:
    if not fecha_corte:
        return None
    try:
        return date.fromisoformat(fecha_corte)
    except ValueError as exc:
        raise HTTPException(422, "fecha_corte debe tener formato AAAA-MM-DD") from exc


def _con_servidor(fn):
    try:
        return fn()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("Error consultando datos")
        raise HTTPException(502, f"No se pudo consultar el servidor de datos: {type(exc).__name__}") from exc


@app.get("/", dependencies=[Depends(requiere_acceso)], include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    out = {"ok": True, "fuente": get_settings().data_source, **get_service().estado()}
    if get_service_hoy() is not get_service():
        out["hoy"] = get_service_hoy().estado()
    return out


def _en_curso(vista: str) -> bool:
    if vista not in ("ayer", "hoy"):
        raise HTTPException(422, "vista debe ser 'ayer' o 'hoy'")
    return vista == "hoy"


@app.get("/api/resumen", dependencies=[Depends(requiere_acceso)])
def resumen(fecha_corte: str | None = None, vista: str = "ayer"):
    """vista='ayer' (cierre hasta el día anterior) o 'hoy' (incluye el día en curso, parcial).
    fecha_corte = 'hoy' a efectos del cálculo (opcional; por defecto la fecha actual)."""
    return _con_servidor(lambda: _servicio(_en_curso(vista)).resumen(_corte(fecha_corte), _en_curso(vista)))


@app.get("/api/tendencia", dependencies=[Depends(requiere_acceso)])
def tendencia(dias: int = 30, fecha_corte: str | None = None, vista: str = "ayer"):
    return _con_servidor(lambda: _servicio(_en_curso(vista)).tendencia(dias, _corte(fecha_corte), _en_curso(vista)))


@app.post("/api/refresh", dependencies=[Depends(requiere_acceso)])
def refresh():
    _con_servidor(get_service().refrescar)
    if get_service_hoy() is not get_service():
        _con_servidor(get_service_hoy().refrescar)
    return {"ok": True, **get_service().estado()}


class Mensaje(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(max_length=4000)


class ChatIn(BaseModel):
    mensajes: list[Mensaje] = Field(min_length=1, max_length=40)


@app.post("/api/chat", dependencies=[Depends(requiere_acceso)])
def chat(body: ChatIn):
    if body.mensajes[-1].role != "user":
        raise HTTPException(422, "El último mensaje debe ser del usuario")
    try:
        texto = agent.responder(get_service(), get_settings(), [m.model_dump() for m in body.mensajes])
    except agent.AgenteNoConfigurado as exc:
        raise HTTPException(503, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("Error en el agente")
        raise HTTPException(502, f"El agente no pudo responder: {type(exc).__name__}") from exc
    return {"respuesta": texto}
