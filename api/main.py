"""
Servidor de Appkinson — implementa el contrato de API v1 (docs/API_CONTRATO.md del
repositorio de la app, branch API).

Persistencia:
    PostgreSQL (servicio "db" del docker-compose): catálogos, pacientes, medicamentos,
        configuración e índice de grabaciones. Esquema en schema.sql.
    Disco (volumen /datos): las grabaciones en
        /datos/archivos/<numero_documento>/Sesion_<n>/<modalidad>/<archivo>

La app envía las grabaciones con el paciente_uuid (sin datos personales). El servidor sabe
quién es porque recibe el perfil antes que los archivos, y con eso nombra las carpetas.
"""
import hashlib
import json
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, Header, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from starlette.concurrency import run_in_threadpool

TOKEN = os.environ["APPKINSON_TOKEN"]
TAMANO_MAX_MB = int(os.environ.get("APPKINSON_TAMANO_MAX_MB", "200"))
DATABASE_URL = os.environ["DATABASE_URL"]
# Prefijo público cuando la API está detrás de un proxy en una sub-ruta (p. ej. /appkinson).
# El proxy quita el prefijo; esto solo sirve para que /docs genere bien sus enlaces.
RUTA_BASE = os.environ.get("APPKINSON_RUTA_BASE", "")

RAIZ = Path(os.environ.get("APPKINSON_DATOS", "/datos"))
ARCHIVOS = RAIZ / "archivos"
TEMPORALES = RAIZ / "tmp"
TAREAS_SEMILLA = Path(os.environ.get("APPKINSON_TAREAS", "/app/semillas/Actividades.txt"))
ESQUEMA = Path(__file__).parent / "schema.sql"

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=10, open=False,
                      kwargs={"row_factory": dict_row})


# ── Arranque ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def ciclo_de_vida(_: FastAPI):
    pool.open(wait=True, timeout=60)
    with pool.connection() as con:
        con.execute(ESQUEMA.read_text(encoding="utf-8"))
        sembrar_catalogos(con)
    ARCHIVOS.mkdir(parents=True, exist_ok=True)
    TEMPORALES.mkdir(parents=True, exist_ok=True)
    yield
    pool.close()


app = FastAPI(title="Appkinson — servidor", version="1", lifespan=ciclo_de_vida,
              root_path=RUTA_BASE)


def sembrar_catalogos(con) -> None:
    """Llena los catálogos con los valores que trae la app, SOLO si están vacíos."""
    if con.execute("SELECT 1 FROM tareas LIMIT 1").fetchone() is None and TAREAS_SEMILLA.exists():
        for linea in TAREAS_SEMILLA.read_text(encoding="utf-8").splitlines():
            linea = linea.strip()
            if not linea or linea.startswith("#"):
                continue
            p = linea.split("-")
            if len(p) >= 6:
                con.execute(
                    "INSERT INTO tareas (codigo, modalidad, descripcion, duracion, instruccion) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (p[1], p[0], p[2], int(p[3]) if p[3].isdigit() else 0, p[4]))
    if con.execute("SELECT 1 FROM medicamentos_catalogo LIMIT 1").fetchone() is None:
        meds = {"Amantadina": ["50", "otra"], "Mirapex": ["0.5", "otra"], "Rasagilina": ["1", "otra"],
                "Safinamida": ["100", "otra"], "Rotigotina": ["6", "16", "otra"],
                "Quetiapina": ["25", "otra"], "Escitalopram": ["20", "otra"],
                "Amitriptilina": ["25", "otra"], "Levotiroxina": ["75", "otra"],
                "Rosuvastatina": ["40", "otra"], "Pregabalina": ["0.5", "otra"],
                "Parken": ["0"], "Carbidopa-Levodopa": ["0"]}
        for nombre, dosis in meds.items():
            con.execute("INSERT INTO medicamentos_catalogo (nombre, dosis) VALUES (%s, %s)",
                        (nombre, dosis))
    if con.execute("SELECT 1 FROM configuracion_global LIMIT 1").fetchone() is None:
        con.execute("INSERT INTO configuracion_global VALUES "
                    "('Audio', 4), ('Video', 3), ('Movimiento', 2)")
        # La siembra no cuenta como cambio de la nube: versión 0, igual que la app recién instalada.
        con.execute("UPDATE configuracion_global_version SET version = 0")


# ── Utilidades ───────────────────────────────────────────────────────────────

class ErrorApi(Exception):
    def __init__(self, estado: int, codigo: str, mensaje: str):
        self.estado, self.codigo, self.mensaje = estado, codigo, mensaje


@app.exception_handler(ErrorApi)
async def _error_api(_: Request, e: ErrorApi):
    # Formato de error común del contrato (sección 1.3)
    return JSONResponse(status_code=e.estado,
                        content={"error": {"codigo": e.codigo, "mensaje": e.mensaje}})


def ahora() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def iso(fecha: datetime | None) -> str:
    return fecha.astimezone().isoformat(timespec="seconds") if fecha else ""


def autenticar(authorization: str | None) -> None:
    if authorization != f"Bearer {TOKEN}":
        raise ErrorApi(401, "TOKEN_INVALIDO", "Token ausente, inválido o revocado")


def responder_catalogo(contenido: dict, if_none_match: str | None) -> Response:
    """GET de catálogo con ETag (sección 2.1): 304 si el contenido no cambió."""
    cuerpo = json.dumps(contenido, ensure_ascii=False, sort_keys=True).encode("utf-8")
    etag = '"' + hashlib.sha256(cuerpo).hexdigest()[:16] + '"'
    if if_none_match == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(content=cuerpo, media_type="application/json", headers={"ETag": etag})


def _paciente_existe(con, paciente_uuid: str) -> None:
    if con.execute("SELECT 1 FROM pacientes WHERE paciente_uuid = %s",
                   (paciente_uuid,)).fetchone() is None:
        raise ErrorApi(404, "PACIENTE_NO_EXISTE", "Envíe primero el perfil del paciente")


def carpeta_paciente(con, paciente_uuid: str) -> str:
    """Nombre de carpeta legible: el número de documento (o el UUID si el perfil no llegó)."""
    fila = con.execute("SELECT numero_documento FROM pacientes WHERE paciente_uuid = %s",
                       (paciente_uuid,)).fetchone()
    documento = "".join(c for c in str((fila or {}).get("numero_documento") or "")
                        if c.isalnum() or c in "-_")
    return documento or paciente_uuid


def _texto(valor) -> str | None:
    return None if valor is None else str(valor)


# ── Salud (fuera del contrato) ───────────────────────────────────────────────

@app.get("/salud", include_in_schema=False)
def salud():
    """Healthcheck de Docker: sin token ni datos. Falla si la base de datos no responde."""
    with pool.connection() as con:
        con.execute("SELECT 1")
    return {"ok": True}


# ── 3.1 Estado ───────────────────────────────────────────────────────────────

@app.get("/api/v1/estado")
def estado(authorization: str | None = Header(None)):
    autenticar(authorization)
    return {"servidor": "appkinson", "version_api": "1",
            "hora_servidor": ahora(), "tamano_max_archivo_mb": TAMANO_MAX_MB}


# ── 3.1.x Catálogos (nube → teléfono) ────────────────────────────────────────

@app.get("/api/v1/catalogo/tareas")
def catalogo_tareas(authorization: str | None = Header(None),
                    if_none_match: str | None = Header(None)):
    autenticar(authorization)
    with pool.connection() as con:
        filas = con.execute("SELECT * FROM tareas ORDER BY modalidad, codigo").fetchall()
    tareas = [{"codigo": f["codigo"], "modalidad": f["modalidad"], "descripcion": f["descripcion"],
               "duracion": f["duracion"], "instruccion": f["instruccion"],
               "vigente": f["vigente"], "actualizado_en": iso(f["actualizado_en"])} for f in filas]
    return responder_catalogo({"tareas": tareas}, if_none_match)


@app.get("/api/v1/catalogo/medicamentos")
def catalogo_medicamentos(authorization: str | None = Header(None),
                          if_none_match: str | None = Header(None)):
    autenticar(authorization)
    with pool.connection() as con:
        filas = con.execute("SELECT * FROM medicamentos_catalogo ORDER BY nombre").fetchall()
    medicamentos = [{"nombre": f["nombre"], "dosis": list(f["dosis"]), "vigente": f["vigente"]}
                    for f in filas]
    return responder_catalogo({"medicamentos": medicamentos}, if_none_match)


@app.get("/api/v1/configuracion-global")
def configuracion_global(authorization: str | None = Header(None),
                         if_none_match: str | None = Header(None)):
    autenticar(authorization)
    with pool.connection() as con:
        version = con.execute("SELECT version FROM configuracion_global_version").fetchone()
        filas = con.execute("SELECT * FROM configuracion_global ORDER BY modalidad").fetchall()
    return responder_catalogo({
        "version": version["version"],
        "modalidades": [{"modalidad": f["modalidad"], "cantidad": f["cantidad"]} for f in filas],
    }, if_none_match)


# ── 3.2 / 3.3 Datos del paciente (teléfono → nube) ───────────────────────────

@app.put("/api/v1/pacientes/{paciente_uuid}")
def perfil(paciente_uuid: str, cuerpo: dict = Body(...),
           authorization: str | None = Header(None)):
    autenticar(authorization)
    if cuerpo.get("paciente_uuid") != paciente_uuid:
        raise ErrorApi(400, "UUID_NO_COINCIDE", "paciente_uuid del cuerpo distinto al de la ruta")
    with pool.connection() as con:
        existia = con.execute("SELECT 1 FROM pacientes WHERE paciente_uuid = %s",
                              (paciente_uuid,)).fetchone() is not None
        con.execute("""
            INSERT INTO pacientes (paciente_uuid, tipo_documento, numero_documento, nombre,
                apellido, fecha_nacimiento, genero, telefono, correo, mano_dominante,
                escolaridad, medico, sesion_actual, registrado_en, actualizado_en, recibido_en)
            VALUES (%(paciente_uuid)s, %(tipo_documento)s, %(numero_documento)s, %(nombre)s,
                %(apellido)s, %(fecha_nacimiento)s, %(genero)s, %(telefono)s, %(correo)s,
                %(mano_dominante)s, %(escolaridad)s, %(medico)s, %(sesion_actual)s,
                %(registrado_en)s, %(actualizado_en)s, now())
            ON CONFLICT (paciente_uuid) DO UPDATE SET
                tipo_documento = EXCLUDED.tipo_documento,
                numero_documento = EXCLUDED.numero_documento, nombre = EXCLUDED.nombre,
                apellido = EXCLUDED.apellido, fecha_nacimiento = EXCLUDED.fecha_nacimiento,
                genero = EXCLUDED.genero, telefono = EXCLUDED.telefono, correo = EXCLUDED.correo,
                mano_dominante = EXCLUDED.mano_dominante, escolaridad = EXCLUDED.escolaridad,
                medico = EXCLUDED.medico, sesion_actual = EXCLUDED.sesion_actual,
                registrado_en = EXCLUDED.registrado_en, actualizado_en = EXCLUDED.actualizado_en,
                recibido_en = now()
        """, {
            "paciente_uuid": paciente_uuid,
            **{c: _texto(cuerpo.get(c)) for c in (
                "tipo_documento", "numero_documento", "nombre", "apellido", "fecha_nacimiento",
                "genero", "telefono", "correo", "mano_dominante", "escolaridad")},
            "medico": Jsonb(cuerpo.get("medico") or {}),
            "sesion_actual": cuerpo.get("sesion_actual"),
            "registrado_en": cuerpo.get("registrado_en") or None,
            "actualizado_en": cuerpo.get("actualizado_en") or None,
        })
    return JSONResponse(status_code=200 if existia else 201, content={"ok": True})


@app.put("/api/v1/pacientes/{paciente_uuid}/medicamentos")
def medicamentos(paciente_uuid: str, cuerpo: dict = Body(...),
                 authorization: str | None = Header(None)):
    autenticar(authorization)
    with pool.connection() as con:
        _paciente_existe(con, paciente_uuid)
        # Reemplazo completo (la app manda siempre la lista entera, con historial).
        con.execute("DELETE FROM medicamentos_paciente WHERE paciente_uuid = %s", (paciente_uuid,))
        for m in cuerpo.get("medicamentos", []):
            con.execute("""
                INSERT INTO medicamentos_paciente (paciente_uuid, medicamento_id, nombre, dosis,
                    frecuencia, metodo, horas, informacion_adicional, activo, registrado_en)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (paciente_uuid, m["medicamento_id"], _texto(m.get("nombre")),
                  _texto(m.get("dosis")), _texto(m.get("frecuencia")), _texto(m.get("metodo")),
                  [str(h) for h in m.get("horas", [])], _texto(m.get("informacion_adicional")),
                  bool(m.get("activo", True)), m.get("registrado_en") or None))
    return {"ok": True}


@app.put("/api/v1/pacientes/{paciente_uuid}/configuracion")
def configuracion(paciente_uuid: str, cuerpo: dict = Body(...),
                  authorization: str | None = Header(None)):
    autenticar(authorization)
    with pool.connection() as con:
        _paciente_existe(con, paciente_uuid)
        for m in cuerpo.get("modalidades", []):
            con.execute("""
                INSERT INTO configuracion_paciente
                    (paciente_uuid, modalidad, cantidad, version_global_aplicada, actualizado_en)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (paciente_uuid, modalidad) DO UPDATE SET
                    cantidad = EXCLUDED.cantidad,
                    version_global_aplicada = EXCLUDED.version_global_aplicada,
                    actualizado_en = EXCLUDED.actualizado_en
            """, (paciente_uuid, m["modalidad"], m["cantidad"],
                  cuerpo.get("version_global_aplicada", 0), cuerpo.get("actualizado_en") or None))
    return {"ok": True}


# ── 3.4 / 3.5 Archivos ───────────────────────────────────────────────────────

@app.head("/api/v1/archivos/{sha256}")
def existe_archivo(sha256: str, authorization: str | None = Header(None)):
    autenticar(authorization)
    with pool.connection() as con:
        existe = con.execute("SELECT 1 FROM archivos WHERE sha256 = %s",
                             (sha256.lower(),)).fetchone() is not None
    return Response(status_code=200 if existe else 404)


def _registrar_archivo(ruta_tmp: Path, calculada: str, campos: dict) -> JSONResponse | dict:
    """Parte con base de datos y disco de la subida (se ejecuta fuera del event loop)."""
    with pool.connection() as con:
        previo = con.execute("SELECT sha256, recibido_en FROM archivos WHERE archivo_id = %s",
                             (campos["archivo_id"],)).fetchone()
        if previo:
            ruta_tmp.unlink(missing_ok=True)
            if previo["sha256"] != calculada:
                raise ErrorApi(409, "CONFLICTO", "Mismo archivo_id con otra huella")
            return JSONResponse(status_code=200, content={
                "archivo_id": campos["archivo_id"], "recibido": True, "sha256_verificado": True,
                "duplicado": True, "recibido_en": iso(previo["recibido_en"])})

        destino = (ARCHIVOS / carpeta_paciente(con, campos["paciente_uuid"])
                   / f"Sesion_{campos['sesion']}" / campos["modalidad"]
                   / Path(campos["nombre_original"]).name)
        destino.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(ruta_tmp), destino)

        fila = con.execute("""
            INSERT INTO archivos (archivo_id, paciente_uuid, sesion, modalidad, tarea, libre,
                grabado_en, sha256, tamano_bytes, nombre_original, ruta, version_app)
            VALUES (%(archivo_id)s, %(paciente_uuid)s, %(sesion)s, %(modalidad)s, %(tarea)s,
                %(libre)s, %(grabado_en)s, %(sha256)s, %(tamano_bytes)s, %(nombre_original)s,
                %(ruta)s, %(version_app)s)
            RETURNING recibido_en
        """, {**campos, "sha256": calculada, "ruta": str(destino.relative_to(RAIZ))}).fetchone()

    print(f"[ARCHIVO] {campos['modalidad']}/{campos['tarea']} sesión {campos['sesion']} — "
          f"{destino.name} ({campos['tamano_bytes']} B)", flush=True)
    return {"archivo_id": campos["archivo_id"], "recibido": True, "sha256_verificado": True,
            "duplicado": False, "recibido_en": iso(fila["recibido_en"])}


@app.post("/api/v1/archivos", status_code=201)
async def subir_archivo(
    authorization: str | None = Header(None),
    archivo: UploadFile = File(...),
    archivo_id: str = Form(...), paciente_uuid: str = Form(...), sesion: int = Form(...),
    modalidad: str = Form(...), tarea: str = Form(...), libre: bool = Form(...),
    grabado_en: str = Form(...), sha256: str = Form(...), tamano_bytes: int = Form(...),
    nombre_original: str = Form(...), version_app: str = Form(...),
):
    autenticar(authorization)
    if modalidad not in ("Audio", "Video", "Movimiento"):
        raise ErrorApi(400, "MODALIDAD_INVALIDA", f"Modalidad desconocida: {modalidad}")
    if tamano_bytes > TAMANO_MAX_MB * 1024 * 1024:
        raise ErrorApi(413, "ARCHIVO_DEMASIADO_GRANDE", f"Máximo {TAMANO_MAX_MB} MB")

    # Se guarda en un temporal calculando la huella SOBRE LOS BYTES RECIBIDOS.
    digest = hashlib.sha256()
    with tempfile.NamedTemporaryFile(delete=False, dir=TEMPORALES) as tmp:
        while bloque := await archivo.read(1024 * 1024):
            digest.update(bloque)
            tmp.write(bloque)
        ruta_tmp = Path(tmp.name)
    calculada = digest.hexdigest()

    if calculada != sha256.lower():
        ruta_tmp.unlink(missing_ok=True)
        raise ErrorApi(422, "SHA256_NO_COINCIDE", "La huella calculada no coincide con la enviada")

    return await run_in_threadpool(_registrar_archivo, ruta_tmp, calculada, {
        "archivo_id": archivo_id, "paciente_uuid": paciente_uuid, "sesion": sesion,
        "modalidad": modalidad, "tarea": tarea, "libre": libre, "grabado_en": grabado_en,
        "tamano_bytes": tamano_bytes, "nombre_original": nombre_original,
        "version_app": version_app})
