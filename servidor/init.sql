-- Esquema del monitor de examen. Postgres lo ejecuta SOLO la primera vez
-- que arranca con el volumen vacío.

-- Un PC físico. Se da de alta solo la primera vez que aparece.
CREATE TABLE equipos (
    id           TEXT PRIMARY KEY,                 -- MachineGuid del PC
    nombre       TEXT,                             -- nombre de red, p.ej. AULA2-PC07
    primera_vez  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ultima_vez   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Una captura concreta (un examen en un PC).
CREATE TABLE sesiones (
    id                TEXT PRIMARY KEY,             -- UUID generado al iniciar
    equipo_id         TEXT NOT NULL REFERENCES equipos(id),
    inicio            TIMESTAMPTZ NOT NULL DEFAULT now(),
    ultima_actividad  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ultimo_seq        INTEGER     NOT NULL DEFAULT 0,
    estado            TEXT        NOT NULL DEFAULT 'activa'  -- activa | finalizada
);
CREATE INDEX idx_sesiones_equipo    ON sesiones (equipo_id);
CREATE INDEX idx_sesiones_actividad ON sesiones (ultima_actividad DESC);

-- Cada delta/lote recibido. La clave compuesta hace la deduplicación sola:
-- un reintento con el mismo seq choca con la PK y se descarta.
CREATE TABLE lotes (
    sesion_id   TEXT        NOT NULL REFERENCES sesiones(id) ON DELETE CASCADE,
    seq         INTEGER     NOT NULL,
    ts_cliente  TIMESTAMPTZ,
    recibido    TIMESTAMPTZ NOT NULL DEFAULT now(),
    texto       TEXT        NOT NULL,
    PRIMARY KEY (sesion_id, seq)
);
