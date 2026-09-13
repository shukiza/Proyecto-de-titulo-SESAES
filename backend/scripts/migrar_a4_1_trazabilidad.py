# -*- coding: utf-8 -*-
"""
SESAES — Migración puntual A.4.1: trazabilidad de origen de Cita +
persistencia normalizada de metadata de sobrecupo.

Sigue el mismo patrón ya establecido en
backend/scripts/migrar_auditoria_sa2.py (ALTER TABLE ... IF NOT EXISTS
sobre una tabla existente, dentro de una sola transacción, verificado
con inspect(conn) al final) combinado con el patrón de
backend/scripts/migrar_acceso_administrativo_sa8.py (Table.create(...,
checkfirst=True) para tablas nuevas).

Qué hace:
  1. Agrega a `cita` (si faltan): creado_por_usuario_id (+ FK a
     usuario.id + índice), creado_por_rol, creado_por_perfil,
     fecha_creacion.
  2. Crea (si faltan) las tablas `cita_sobrecupo` y
     `cita_sobrecupo_conflicto`, con sus FK ON DELETE CASCADE, el
     UNIQUE de `cita_sobrecupo.cita_id` y el índice de
     `cita_sobrecupo_conflicto.cita_sobrecupo_id` (declarados en los
     modelos — ver app/models/cita_sobrecupo.py).
  3. Verifica el esquema resultante dentro de la MISMA transacción
     antes de dar la migración por exitosa: columnas, nullability
     (incluyendo que `cita_sobrecupo`/`cita_sobrecupo_conflicto`, al
     ser tablas COMPLETAMENTE NUEVAS sin ningún antecedente histórico,
     tengan sus columnas obligatorias en NOT NULL — a diferencia de
     las columnas nuevas de `cita`, que sí deben quedar nullable por
     compatibilidad histórica), FKs (incluido ON DELETE CASCADE donde
     corresponde), UNIQUE e índices — no solo "la columna/tabla
     existe".

Qué NO hace (deliberado, alcance de A.4.1):
  - No borra ni transforma ninguna fila existente de `cita`.
  - No asigna un valor "aproximado" a las columnas nuevas para filas
    históricas — quedan en NULL a propósito (ver sección
    "fecha_creacion histórica" más abajo).
  - No usa create_all(): create_all() no altera una tabla EXISTENTE
    (`cita` ya existe desde antes de A.4.1), así que agregar columnas
    a una tabla ya creada requiere ALTER TABLE explícito sin importar
    qué haga create_all() con las tablas nuevas.

── fecha_creacion histórica — por qué NO lleva DEFAULT en el ALTER ──

Si el ALTER TABLE que agrega `fecha_creacion` incluyera
`DEFAULT now()` directamente, PostgreSQL calcularía `now()` UNA vez
y lo usaría para rellenar TODAS las filas existentes en ese mismo
instante — es decir, cada cita creada antes de esta migración
quedaría con `fecha_creacion` = el momento en que se corrió la
migración, no su fecha de creación real. Esto sería exactamente el
error que el ticket A.4.1 pide evitar explícitamente: "no afirmar que
conocemos su fecha real".

Por eso el ALTER se hace en dos pasos:
  1. `ADD COLUMN fecha_creacion TIMESTAMP NULL` (sin DEFAULT) — todas
     las filas existentes quedan en NULL, que es la respuesta honesta
     ("no sabemos cuándo se creó esta fila real").
  2. `ALTER COLUMN fecha_creacion SET DEFAULT now()` — a partir de acá,
     cualquier INSERT nuevo que no fije el valor explícitamente lo
     recibe automáticamente. Un ALTER COLUMN ... SET DEFAULT nunca
     modifica filas ya existentes, solo cambia el comportamiento de
     inserts futuros.

Las demás columnas nuevas (creado_por_usuario_id, creado_por_rol,
creado_por_perfil) tampoco llevan DEFAULT: no hay ningún valor
"neutro" honesto para ellas — quedan NULL en filas históricas, punto
final, y así se documenta también en el modelo (Cita, docstring de
cada columna).

Ejecución manual (desde backend/, con el venv activado):

    python -m scripts.migrar_a4_1_trazabilidad
"""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.database import engine
from app.models.cita import Cita  # registra 'cita' antes de inspeccionarla
from app.models.usuario import Usuario  # registra 'usuario' para las FK nuevas
from app.models.cita_sobrecupo import CitaSobrecupo, CitaSobrecupoConflicto


class MigracionA41IncompletaError(RuntimeError):
    """
    Las sentencias se ejecutaron sin lanzar excepción, pero el esquema
    resultante no cumple exactamente el contrato que A.4.1 necesita.
    No se asume éxito solo porque no hubo excepción — se confirma
    inspeccionando el esquema real, dentro de la misma transacción.
    """


_ALTER_CITA_COLUMNAS = (
    "ALTER TABLE cita ADD COLUMN IF NOT EXISTS creado_por_usuario_id INTEGER NULL "
    "REFERENCES usuario(id);",
    "ALTER TABLE cita ADD COLUMN IF NOT EXISTS creado_por_rol VARCHAR NULL;",
    "ALTER TABLE cita ADD COLUMN IF NOT EXISTS creado_por_perfil VARCHAR NULL;",
    # Sin DEFAULT en este ADD COLUMN — ver docstring del módulo,
    # sección "fecha_creacion histórica".
    "ALTER TABLE cita ADD COLUMN IF NOT EXISTS fecha_creacion TIMESTAMP NULL;",
)

# A.4.1 v2 — PostgreSQL NO crea automáticamente un índice sobre una FK
# (a diferencia de la PK). creado_por_usuario_id se va a consultar
# constantemente para reportes/auditoría ("todas las citas creadas por
# X"), así que se indexa explícitamente. Nombre alineado con la
# convención por defecto de SQLAlchemy (ix_<tabla>_<columna>) para que
# coincida con el índice que Table.create(...) habría generado si la
# columna hubiera nacido con la tabla.
_INDICE_CREADO_POR_USUARIO_ID = (
    "CREATE INDEX IF NOT EXISTS ix_cita_creado_por_usuario_id "
    "ON cita (creado_por_usuario_id);"
)

# Separado del bloque anterior a propósito: SET DEFAULT solo cambia el
# comportamiento de INSERTs futuros, nunca reescribe filas existentes
# — pero solo tiene sentido ejecutarlo si la columna ya existe (recién
# creada arriba, o ya existente de una corrida previa de este mismo
# script).
_ALTER_CITA_DEFAULT_FECHA_CREACION = (
    "ALTER TABLE cita ALTER COLUMN fecha_creacion SET DEFAULT now();"
)

_COLUMNAS_CITA_ESPERADAS = {
    "creado_por_usuario_id",
    "creado_por_rol",
    "creado_por_perfil",
    "fecha_creacion",
}

_TABLAS_NUEVAS_ESPERADAS = {"cita_sobrecupo", "cita_sobrecupo_conflicto"}


def _fk_hacia(fks: list[dict], columna: str, tabla_referida: str) -> dict | None:
    """
    Busca, entre las FKs reflejadas de una tabla, la que sale de
    `columna` hacia `tabla_referida`. Devuelve el dict crudo de
    inspector.get_foreign_keys() (para poder inspeccionar
    'options'/'ondelete') o None si no existe ninguna FK así.
    """
    for fk in fks:
        columnas_origen = fk.get("constrained_columns") or []
        if columnas_origen == [columna] and fk.get("referred_table") == tabla_referida:
            return fk
    return None


def _fk_es_cascade(fk: dict) -> bool:
    ondelete = (fk.get("options") or {}).get("ondelete")
    return bool(ondelete) and ondelete.upper() == "CASCADE"


def _validar_esquema(conn) -> None:
    inspector = inspect(conn)
    errores: list[str] = []

    columnas_cita = {c["name"]: c for c in inspector.get_columns("cita")}
    faltantes_cita = _COLUMNAS_CITA_ESPERADAS - columnas_cita.keys()
    if faltantes_cita:
        errores.append(
            "cita: faltan columnas " + ", ".join(sorted(faltantes_cita)) + "."
        )
    else:
        # Todas nullable: compatibilidad histórica obligatoria (ver
        # docstring del módulo) — ninguna de estas cuatro puede quedar
        # NOT NULL, o las filas anteriores a A.4.1 romperían cualquier
        # lectura/reflexión de esquema que asuma el contrato actual.
        for nombre in _COLUMNAS_CITA_ESPERADAS:
            if not columnas_cita[nombre].get("nullable", True):
                errores.append(
                    f"cita.{nombre} debe ser nullable (compatibilidad "
                    "histórica); quedó NOT NULL."
                )

        default_fecha = columnas_cita["fecha_creacion"].get("default") or ""
        if "now" not in default_fecha.lower():
            errores.append(
                "cita.fecha_creacion no tiene el DEFAULT now() esperado "
                f"a nivel de motor para filas nuevas (reflejado: {default_fecha!r})."
            )

        fk_creador = _fk_hacia(
            inspector.get_foreign_keys("cita"), "creado_por_usuario_id", "usuario",
        )
        if fk_creador is None:
            errores.append(
                "cita.creado_por_usuario_id debe tener una FK hacia usuario(id)."
            )

        indices_cita = {
            tuple(ix.get("column_names") or ()) for ix in inspector.get_indexes("cita")
        }
        if ("creado_por_usuario_id",) not in indices_cita:
            errores.append(
                "cita.creado_por_usuario_id debe estar indexada "
                "(se consulta por creador en reportes/auditoría)."
            )

    tablas = set(inspector.get_table_names())
    faltantes_tablas = _TABLAS_NUEVAS_ESPERADAS - tablas
    if faltantes_tablas:
        errores.append(
            "Faltan tablas A.4.1: " + ", ".join(sorted(faltantes_tablas)) + "."
        )
    else:
        columnas_sobrecupo = {
            c["name"]: c for c in inspector.get_columns("cita_sobrecupo")
        }
        esperadas_sobrecupo = {
            "id", "cita_id", "motivo", "fecha_creacion", "estado_revision",
        }
        faltantes_sobrecupo = esperadas_sobrecupo - columnas_sobrecupo.keys()
        if faltantes_sobrecupo:
            errores.append(
                "cita_sobrecupo: faltan columnas "
                + ", ".join(sorted(faltantes_sobrecupo)) + "."
            )
        else:
            # A.4.1 v3 — invariantes NOT NULL de la tabla NUEVA
            # cita_sobrecupo. A diferencia de las columnas nuevas de
            # `cita` (que SÍ deben ser nullable por compatibilidad
            # histórica, ver arriba), cita_sobrecupo no tiene ningún
            # antecedente anterior a esta migración: no existe, ni
            # puede existir, ningún CitaSobrecupo "histórico" sin
            # cita_id ni sin fecha_creacion real.
            if columnas_sobrecupo["cita_id"].get("nullable", True):
                errores.append("cita_sobrecupo.cita_id debe ser NOT NULL.")
            if columnas_sobrecupo["fecha_creacion"].get("nullable", True):
                errores.append(
                    "cita_sobrecupo.fecha_creacion debe ser NOT NULL "
                    "(tabla nueva, sin filas históricas que compatibilizar)."
                )
            default_fecha_sobrecupo = (
                columnas_sobrecupo["fecha_creacion"].get("default") or ""
            )
            if "now" not in default_fecha_sobrecupo.lower():
                errores.append(
                    "cita_sobrecupo.fecha_creacion no tiene el DEFAULT "
                    "now() esperado a nivel de motor (reflejado: "
                    f"{default_fecha_sobrecupo!r})."
                )
            # motivo / estado_revision SÍ pueden seguir NULL — ver
            # docstring de CitaSobrecupo (compatibilidad temporal con
            # el frontend actual, y preparación para una fase futura
            # de aprobación, respectivamente). No se valida su
            # nullability acá a propósito: exigir NOT NULL sería una
            # regresión de ese diseño deliberado.

        fk_sobrecupo_cita = _fk_hacia(
            inspector.get_foreign_keys("cita_sobrecupo"), "cita_id", "cita",
        )
        if fk_sobrecupo_cita is None:
            errores.append("cita_sobrecupo.cita_id debe tener una FK hacia cita(id).")
        elif not _fk_es_cascade(fk_sobrecupo_cita):
            errores.append("cita_sobrecupo.cita_id debe ser ON DELETE CASCADE.")

        uniques_sobrecupo = {
            tuple(u.get("column_names") or ())
            for u in inspector.get_unique_constraints("cita_sobrecupo")
        }
        # SQLite refleja UNIQUE de columna simple como índice único, no
        # siempre como "unique constraint" — se acepta cualquiera de
        # las dos formas para no romper la suite de tests (que corre
        # sobre SQLite), pero sobre PostgreSQL real (el único dialecto
        # que este script soporta, ver más abajo) sí debe aparecer acá.
        indices_unicos_sobrecupo = {
            tuple(ix.get("column_names") or ())
            for ix in inspector.get_indexes("cita_sobrecupo")
            if ix.get("unique")
        }
        if ("cita_id",) not in uniques_sobrecupo and ("cita_id",) not in indices_unicos_sobrecupo:
            errores.append(
                "cita_sobrecupo.cita_id debe ser UNIQUE (relación 1:1 con cita)."
            )

        columnas_conflicto = {
            c["name"]: c for c in inspector.get_columns("cita_sobrecupo_conflicto")
        }
        esperadas_conflicto = {"id", "cita_sobrecupo_id", "codigo"}
        faltantes_conflicto = esperadas_conflicto - columnas_conflicto.keys()
        if faltantes_conflicto:
            errores.append(
                "cita_sobrecupo_conflicto: faltan columnas "
                + ", ".join(sorted(faltantes_conflicto)) + "."
            )
        else:
            # A.4.1 v3 — igual que cita_sobrecupo: tabla completamente
            # nueva, sin filas históricas, así que ambas columnas
            # deben ser NOT NULL (ya lo eran en el modelo desde v1;
            # esto solo lo confirma también a nivel de esquema real).
            if columnas_conflicto["cita_sobrecupo_id"].get("nullable", True):
                errores.append(
                    "cita_sobrecupo_conflicto.cita_sobrecupo_id debe ser "
                    "NOT NULL."
                )
            if columnas_conflicto["codigo"].get("nullable", True):
                errores.append(
                    "cita_sobrecupo_conflicto.codigo debe ser NOT NULL."
                )

        fk_conflicto_sobrecupo = _fk_hacia(
            inspector.get_foreign_keys("cita_sobrecupo_conflicto"),
            "cita_sobrecupo_id", "cita_sobrecupo",
        )
        if fk_conflicto_sobrecupo is None:
            errores.append(
                "cita_sobrecupo_conflicto.cita_sobrecupo_id debe tener una "
                "FK hacia cita_sobrecupo(id)."
            )
        elif not _fk_es_cascade(fk_conflicto_sobrecupo):
            errores.append(
                "cita_sobrecupo_conflicto.cita_sobrecupo_id debe ser ON "
                "DELETE CASCADE."
            )

        indices_conflicto = {
            tuple(ix.get("column_names") or ())
            for ix in inspector.get_indexes("cita_sobrecupo_conflicto")
        }
        if ("cita_sobrecupo_id",) not in indices_conflicto:
            errores.append(
                "cita_sobrecupo_conflicto.cita_sobrecupo_id debe estar "
                "indexada (PostgreSQL no indexa FKs automáticamente, y "
                "esta relación se consulta constantemente)."
            )

    if errores:
        raise MigracionA41IncompletaError(
            "Migración A.4.1 incompleta:\n- " + "\n- ".join(errores)
        )


def migrar_a4_1_trazabilidad(
    bind: Engine = engine,
    *,
    emitir_mensaje: bool = True,
) -> None:
    dialecto = bind.dialect.name
    if dialecto != "postgresql":
        raise RuntimeError(
            "A.4.1: esta migración manual asume PostgreSQL (mismo "
            "supuesto que migrar_auditoria_sa2.py y "
            "migrar_acceso_administrativo_sa8.py, que ya usan "
            "sintaxis específica de este motor). No se debe correr "
            f"contra un dialecto no contemplado ({dialecto!r}) sin "
            "revisar antes si el SQL sigue siendo válido."
        )

    with bind.begin() as conn:
        for statement in _ALTER_CITA_COLUMNAS:
            conn.execute(text(statement))

        conn.execute(text(_ALTER_CITA_DEFAULT_FECHA_CREACION))
        conn.execute(text(_INDICE_CREADO_POR_USUARIO_ID))

        # Orden importante por la FK cita_sobrecupo_conflicto ->
        # cita_sobrecupo.
        CitaSobrecupo.__table__.create(bind=conn, checkfirst=True)
        CitaSobrecupoConflicto.__table__.create(bind=conn, checkfirst=True)

        _validar_esquema(conn)

    if emitir_mensaje:
        print(
            "Migración A.4.1 verificada: columnas de trazabilidad de "
            "origen en 'cita' y tablas 'cita_sobrecupo' / "
            "'cita_sobrecupo_conflicto' existen y cumplen el contrato "
            "esperado. Filas de 'cita' anteriores a esta migración "
            "quedaron con las columnas nuevas en NULL — a propósito, "
            "no se les asignó ningún valor histórico inventado."
        )


if __name__ == "__main__":
    migrar_a4_1_trazabilidad()
