-- Esquema de la base de datos del servidor de Appkinson (PostgreSQL).
-- Se ejecuta en cada arranque del servicio "api": todo es IF NOT EXISTS / OR REPLACE,
-- así que es seguro repetirlo y no borra datos.

-- ═══ Catálogos (nube → teléfono). Se editan aquí (p. ej. desde Adminer). ═══════════

CREATE TABLE IF NOT EXISTS tareas (
    codigo          TEXT PRIMARY KEY,
    modalidad       TEXT NOT NULL CHECK (modalidad IN ('Audio', 'Video', 'Movimiento')),
    descripcion     TEXT NOT NULL DEFAULT '',
    duracion        INTEGER NOT NULL DEFAULT 0,
    instruccion     TEXT NOT NULL DEFAULT '',
    vigente         BOOLEAN NOT NULL DEFAULT TRUE,     -- false = retirada (no borrar filas)
    actualizado_en  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS medicamentos_catalogo (
    nombre          TEXT PRIMARY KEY,
    dosis           TEXT[] NOT NULL DEFAULT '{}',      -- p. ej. {50,otra}
    vigente         BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE IF NOT EXISTS configuracion_global (
    modalidad       TEXT PRIMARY KEY CHECK (modalidad IN ('Audio', 'Video', 'Movimiento')),
    cantidad        INTEGER NOT NULL CHECK (cantidad >= 1)
);

-- Versión de la configuración global (fila única). Sube SOLA con cada cambio en
-- configuracion_global (trigger de abajo): así la app sabe que la nube tiene prioridad.
CREATE TABLE IF NOT EXISTS configuracion_global_version (
    id              INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    version         INTEGER NOT NULL DEFAULT 0,
    actualizado_en  TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO configuracion_global_version (id) VALUES (1) ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION subir_version_configuracion() RETURNS trigger AS $$
BEGIN
    UPDATE configuracion_global_version SET version = version + 1, actualizado_en = now();
    RETURN NULL;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tr_configuracion_global ON configuracion_global;
CREATE TRIGGER tr_configuracion_global
    AFTER INSERT OR UPDATE OR DELETE ON configuracion_global
    FOR EACH STATEMENT EXECUTE FUNCTION subir_version_configuracion();

CREATE OR REPLACE FUNCTION tocar_actualizado_en() RETURNS trigger AS $$
BEGIN
    NEW.actualizado_en = now();
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tr_tareas_actualizado ON tareas;
CREATE TRIGGER tr_tareas_actualizado
    BEFORE UPDATE ON tareas FOR EACH ROW EXECUTE FUNCTION tocar_actualizado_en();

-- ═══ Datos del paciente (teléfono → nube). Solo lectura: la app los reenvía. ════════

CREATE TABLE IF NOT EXISTS pacientes (
    paciente_uuid       UUID PRIMARY KEY,
    tipo_documento      TEXT,
    numero_documento    TEXT,
    nombre              TEXT,
    apellido            TEXT,
    fecha_nacimiento    TEXT,
    genero              TEXT,
    telefono            TEXT,
    correo              TEXT,
    mano_dominante      TEXT,
    escolaridad         TEXT,
    medico              JSONB NOT NULL DEFAULT '{}',
    sesion_actual       INTEGER,
    registrado_en       TIMESTAMPTZ,
    actualizado_en      TIMESTAMPTZ,
    recibido_en         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_pacientes_documento ON pacientes (numero_documento);

CREATE TABLE IF NOT EXISTS medicamentos_paciente (
    paciente_uuid           UUID NOT NULL REFERENCES pacientes ON DELETE CASCADE,
    medicamento_id          INTEGER NOT NULL,
    nombre                  TEXT,
    dosis                   TEXT,
    frecuencia              TEXT,
    metodo                  TEXT,
    horas                   TEXT[] NOT NULL DEFAULT '{}',
    informacion_adicional   TEXT,
    activo                  BOOLEAN NOT NULL,           -- false = eliminado en la app (historial)
    registrado_en           TIMESTAMPTZ,
    recibido_en             TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (paciente_uuid, medicamento_id)
);

CREATE TABLE IF NOT EXISTS configuracion_paciente (
    paciente_uuid           UUID NOT NULL REFERENCES pacientes ON DELETE CASCADE,
    modalidad               TEXT NOT NULL,
    cantidad                INTEGER NOT NULL,
    version_global_aplicada INTEGER NOT NULL DEFAULT 0,
    actualizado_en          TIMESTAMPTZ,
    PRIMARY KEY (paciente_uuid, modalidad)
);

-- Grabaciones recibidas. El archivo está en disco (volumen) en la ruta indicada.
CREATE TABLE IF NOT EXISTS archivos (
    archivo_id          UUID PRIMARY KEY,
    paciente_uuid       UUID NOT NULL,
    sesion              INTEGER NOT NULL,
    modalidad           TEXT NOT NULL,
    tarea               TEXT NOT NULL,
    libre               BOOLEAN NOT NULL,
    grabado_en          TEXT,
    sha256              CHAR(64) NOT NULL,
    tamano_bytes        BIGINT NOT NULL,
    nombre_original     TEXT NOT NULL,
    ruta                TEXT NOT NULL,                  -- relativa a /datos
    version_app         TEXT,
    recibido_en         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_archivos_sha256 ON archivos (sha256);
CREATE INDEX IF NOT EXISTS ix_archivos_paciente ON archivos (paciente_uuid, sesion);

-- ═══ Vistas para consultar rápido (Adminer → "Vistas") ══════════════════════════════

CREATE OR REPLACE VIEW vista_pacientes AS
SELECT p.numero_documento, p.nombre, p.apellido, p.sesion_actual,
       (SELECT count(*) FROM medicamentos_paciente m
         WHERE m.paciente_uuid = p.paciente_uuid AND m.activo)          AS medicamentos_activos,
       (SELECT count(*) FROM archivos a WHERE a.paciente_uuid = p.paciente_uuid) AS archivos,
       (SELECT max(a.recibido_en) FROM archivos a
         WHERE a.paciente_uuid = p.paciente_uuid)                       AS ultimo_archivo,
       p.actualizado_en, p.paciente_uuid
FROM pacientes p
ORDER BY p.numero_documento;

CREATE OR REPLACE VIEW vista_medicamentos AS
SELECT p.numero_documento, p.nombre AS paciente, m.nombre AS medicamento, m.dosis,
       m.frecuencia, m.metodo, array_to_string(m.horas, ', ') AS horas,
       m.informacion_adicional, m.activo, m.registrado_en
FROM medicamentos_paciente m JOIN pacientes p USING (paciente_uuid)
ORDER BY p.numero_documento, m.activo DESC, m.nombre;

CREATE OR REPLACE VIEW vista_archivos AS
SELECT p.numero_documento, a.sesion, a.modalidad, a.tarea, a.libre, a.nombre_original,
       round(a.tamano_bytes / 1048576.0, 2) AS tamano_mb, a.grabado_en, a.recibido_en, a.ruta
FROM archivos a LEFT JOIN pacientes p USING (paciente_uuid)
ORDER BY a.recibido_en DESC;
