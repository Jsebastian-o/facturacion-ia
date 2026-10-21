# Facturación Sidoc — dashboard + agente

Plataforma web (FastAPI) que muestra la facturación del **día anterior**, el **acumulado del mes** y el
**acumulado del año** por categoría, y el **peso en toneladas** de ACERO SIDOC y GR_CEMENTOS. Incluye un
chat (agente con Claude) que responde preguntas usando las mismas cifras del tablero.

## Estructura

```
app/
  config.py      variables de entorno (.env)
  repository.py  acceso a datos: SQL Server (un SELECT agregado y parametrizado) o CSV de muestra
  service.py     reglas de negocio, caché con TTL, resumen / tendencia / consulta libre
  agent.py       agente con tool use (lee del mismo servicio que el tablero)
  main.py        API + autenticación básica
  static/index.html  dashboard con el agente en un panel lateral (sin dependencias externas)
tests/           pytest (límites de fechas, cálculo contra la muestra, API, agente)
data/Muestra_vista.csv
```

## Puesta en marcha (Windows)

```powershell
python -m venv .venv ; .venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env        # completar DB_*, VIEW_NAME y ANTHROPIC_API_KEY (ya viene hecho en este proyecto)
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Abrir `http://localhost:8000`. Para probar sin base de datos: `DATA_SOURCE=csv` en el `.env`.
Requiere el driver **ODBC Driver 18 for SQL Server** instalado en el equipo.

## Reglas de cálculo

- Facturación = `SUM(ValorSubtotalLocal)` de documentos con `Estado` en `ESTADOS_VALIDOS` (por defecto `Aprobado`).
  Las notas crédito ya vienen en negativo en la vista, así que el resultado es neto.
- Peso = `SUM(Peso)` en kg, mostrado en toneladas.
- **Fines de semana y festivos:** se suman al día hábil anterior (calendario de Colombia, con los festivos
  trasladados a lunes). Viernes + sábado + domingo = "viernes"; si el lunes es festivo, también el lunes.
  Un festivo entre semana se suma al día anterior.
- "Día anterior" = el día hábil al que pertenece ayer (hora de Bogotá). Un lunes muestra el viernes con el fin de semana
  incluido, y el tablero lo indica en la etiqueta. El día en curso nunca se incluye.
- Mes = del día 1 hasta ayer; año = del 1 de enero hasta ayer. Se cuentan por día hábil: las ventas de un sábado 31 o un
  domingo 1 pertenecen al mes del viernes al que se suman.
- Las categorías de peso se configuran con `CATEGORIAS_PESO` y coinciden por prefijo
  (`GR_CEMENTO` encuentra `GR_CEMENTOS`).
- **% del año:** en la tabla por categoría, el valor del acumulado del año de cada categoría dividido entre el total del año
  (en pesos). Las categorías suman 100 %. Si el total del año no es positivo, se muestra "—". En la pestaña Hoy el año incluye hoy.
- **Tipo de cliente (canal):** tabla con la facturación por la columna `TipoCliente` de la vista. Se muestran siempre
  MAYORISTA, MINORISTA y CONSTRUCTORES (`CANALES`), aunque no tengan ventas; la vista trae "CONSTRUCTOR" en singular y se
  reconoce igual. Cualquier otro valor aparece como fila adicional ("SIN TIPO" si viene vacío), así que el total siempre
  coincide con el de la tabla por categoría. Incluye "% del año".
- **Pestaña "Hoy":** muestra la misma información (tarjetas, tablas y gráfica) pero con el día en curso, que es parcial.
  Sirve para seguir el día; el cierre oficial es la pestaña "Resumen", que llega hasta el día anterior.
  El acumulado del mes y del año de esa pestaña incluye lo facturado hoy. Aplica la misma regla de fines de semana y
  festivos. Qué tan al día esté depende de cada cuánto se cargue la vista en SQL Server; el tablero muestra la hora de
  la última carga (`FechaCarga`).
- **Segunda tabla (peso y valor):** ACERO SIDOC y GR_CEMENTO van en toneladas y PRODUCTOS NO SIDOC en pesos
  (`CATEGORIAS_VALOR`). Como las unidades son distintas, esa tabla no tiene fila de totales.
  La primera tabla (todo en COP) sí la conserva.

## Cómo se mantiene actualizada

Al arrancar, un hilo en segundo plano consulta SQL Server con un solo SELECT agregado por (día, categoría, tienda, tipo de cliente) desde
el 1 de enero del año anterior y repite la consulta cada `CACHE_TTL_SECONDS` (60 s por defecto). Las páginas y el chat se
sirven siempre desde memoria, así que responden al instante sin esperar a la base de datos. El tablero se vuelve a
pedir solo cada 60 segundos y tiene el botón "Actualizar ahora". Si el servidor no responde, se siguen mostrando los
últimos datos con un aviso en pantalla.

## API

| Ruta | Descripción |
|---|---|
| `GET /api/resumen?vista=ayer\|hoy` | Totales y por categoría (día / mes / año), peso foco; `hoy` incluye el día en curso |
| `GET /api/tendencia?dias=30&vista=ayer\|hoy` | Serie por día hábil |
| `POST /api/refresh` | Fuerza recarga desde el servidor |
| `POST /api/chat` | Agente (`{"mensajes":[{"role":"user","content":"..."}]}`) |
| `GET /api/health` | Estado (sin autenticación) |

## Pruebas

```powershell
python -m pytest -q
```

## Seguridad

- Usar un usuario de SQL Server de **solo lectura** sobre la vista.
- Acceso web: por defecto sin control (`APP_USER` vacío), pensado para un solo usuario en la red interna. Si algún día se
  expone más allá, definir `APP_USER` / `APP_PASSWORD` y ponerla detrás de HTTPS.
- El archivo `.env` contiene credenciales: no se versiona (`.gitignore`) ni se comparte.
- La clave de Anthropic vive solo en el servidor (`.env`), nunca en el navegador. El agente únicamente puede
  invocar las dos herramientas de `agent.py`; no tiene acceso directo a la base de datos.
