# -*- coding: utf-8 -*-
"""
SESAES — A.4.1: la migración manual corre dos veces sin romper.

Igual que test_a3_concurrencia_postgres.py, esto requiere una base
PostgreSQL de test real (TEST_POSTGRES_URL) — el script de migración
usa sintaxis específica de PostgreSQL (ALTER TABLE ... ADD COLUMN IF
NOT EXISTS, ALTER COLUMN ... SET DEFAULT) y además rechaza
explícitamente cualquier otro dialecto (ver
migrar_a4_1_trazabilidad.py), así que no tiene sentido — ni es
posible — ejercitarlo contra el SQLite que usa el resto de la suite.

Si TEST_POSTGRES_URL no está definida, el test se salta explícitamente
— NUNCA se simula "pasar" esta garantía sin una base real.

Repetibilidad (A.4.1 v2): este archivo puede correrse VARIAS veces
seguidas contra la MISMA base de test sin fallar por datos residuales.
El fixture limpia, al inicio Y al final de cada test, exactamente las
filas de negocio (`usuario`/`profesional`/`cita`) que crea — nunca un
TRUNCATE ni un DELETE sin filtro, que borraría datos ajenos si
TEST_POSTGRES_URL apuntara (por error) a una base con más contenido.
Se identifican por un correo/iniciales fijos y reservados a este
archivo (sufijo "-a41-migracion") para no chocar con datos de otras
suites que puedan compartir la misma base desechable.

Cómo correrlo (dos veces seguidas, para demostrar idempotencia real):
    TEST_POSTGRES_URL="postgresql+psycopg2://..." \\
        pytest tests/test_a4_1_migracion_postgres.py -m postgres -q
    TEST_POSTGRES_URL="postgresql+psycopg2://..." \\
        pytest tests/test_a4_1_migracion_postgres.py -m postgres -q
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.postgres

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

if not TEST_POSTGRES_URL:
    pytest.skip(
        "A.4.1: TEST_POSTGRES_URL no está definida — la migración "
        "manual (ALTER TABLE / CREATE TABLE específicos de "
        "PostgreSQL, corrida dos veces) NO se validó contra Postgres "
        "real en esta ejecución. Define TEST_POSTGRES_URL apuntando "
        "a un Postgres de test DESECHABLE para ejercitar esta "
        "garantía — nunca contra una base productiva.",
        allow_module_level=True,
    )

from sqlalchemy import create_engine, inspect, text

from scripts.migrar_a4_1_trazabilidad import migrar_a4_1_trazabilidad  # noqa: E402

_CORREO_HISTORICO = "historico-a41-migracion@sesaes.cl"
_INICIALES_PROFESIONAL = "PA4M"  # "M" de "migración" — no choca con
                                  # las iniciales "PA4" que usa
                                  # test_a4_1_trazabilidad.py (SQLite,
                                  # bases completamente separadas, pero
                                  # se mantiene distinto por claridad).


def _limpiar_filas_de_prueba(engine) -> None:
    """
    Borra ÚNICAMENTE las filas que este archivo crea, identificadas
    por su correo/iniciales fijos — en orden seguro por FK (cita antes
    que profesional/usuario). Se llama tanto ANTES como DESPUÉS de
    cada test, así que el archivo puede correrse cualquier cantidad de
    veces seguidas contra la misma base sin chocar con una corrida
    previa interrumpida.
    """
    with engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM cita WHERE profesional_id IN "
            "(SELECT id FROM profesional WHERE iniciales = :iniciales) "
            "OR estudiante_id IN "
            "(SELECT id FROM usuario WHERE correo = :correo);"
        ), {"iniciales": _INICIALES_PROFESIONAL, "correo": _CORREO_HISTORICO})
        conn.execute(text(
            "DELETE FROM profesional WHERE iniciales = :iniciales;"
        ), {"iniciales": _INICIALES_PROFESIONAL})
        conn.execute(text(
            "DELETE FROM usuario WHERE correo = :correo;"
        ), {"correo": _CORREO_HISTORICO})


@pytest.fixture()
def engine_postgres_test():
    engine = create_engine(TEST_POSTGRES_URL)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS cita_sobrecupo_conflicto CASCADE;"))
        conn.execute(text("DROP TABLE IF EXISTS cita_sobrecupo CASCADE;"))
        conn.execute(text(
            "ALTER TABLE cita "
            "DROP COLUMN IF EXISTS creado_por_usuario_id, "
            "DROP COLUMN IF EXISTS creado_por_rol, "
            "DROP COLUMN IF EXISTS creado_por_perfil, "
            "DROP COLUMN IF EXISTS fecha_creacion;"
        ))
    _limpiar_filas_de_prueba(engine)
    try:
        yield engine
    finally:
        _limpiar_filas_de_prueba(engine)
        engine.dispose()


def test_migracion_a41_corre_dos_veces_sin_romper(engine_postgres_test):
    migrar_a4_1_trazabilidad(bind=engine_postgres_test, emitir_mensaje=False)
    migrar_a4_1_trazabilidad(bind=engine_postgres_test, emitir_mensaje=False)

    inspector = inspect(engine_postgres_test)
    columnas_cita = {c["name"] for c in inspector.get_columns("cita")}
    assert {
        "creado_por_usuario_id", "creado_por_rol", "creado_por_perfil", "fecha_creacion",
    } <= columnas_cita
    assert "cita_sobrecupo" in inspector.get_table_names()
    assert "cita_sobrecupo_conflicto" in inspector.get_table_names()


def test_migracion_a41_no_asigna_fecha_a_filas_historicas(engine_postgres_test):
    """La corrección explícita que pide el ticket: filas de `cita`
    creadas ANTES de correr la migración no deben quedar con
    fecha_creacion = momento de la migración."""
    with engine_postgres_test.begin() as conn:
        conn.execute(text(
            "INSERT INTO usuario (correo, password, rol, activo) "
            "VALUES (:correo, 'x', 'estudiante', true)"
        ), {"correo": _CORREO_HISTORICO})
        estudiante_id = conn.execute(text(
            "SELECT id FROM usuario WHERE correo = :correo"
        ), {"correo": _CORREO_HISTORICO}).scalar_one()
        conn.execute(text(
            "INSERT INTO profesional (nombre, especialidad, iniciales, estado, duracion_min) "
            "VALUES ('Prof Test A41 Migración', 'Nutrición', :iniciales, 'activo', 45)"
        ), {"iniciales": _INICIALES_PROFESIONAL})
        profesional_id = conn.execute(text(
            "SELECT id FROM profesional WHERE iniciales = :iniciales"
        ), {"iniciales": _INICIALES_PROFESIONAL}).scalar_one()
        conn.execute(text(
            "INSERT INTO cita (estudiante_id, profesional_id, fecha, hora, estado) "
            "VALUES (:est, :prof, '2020-01-15', '09:00 AM', 'completada')"
        ), {"est": estudiante_id, "prof": profesional_id})

    migrar_a4_1_trazabilidad(bind=engine_postgres_test, emitir_mensaje=False)

    with engine_postgres_test.begin() as conn:
        fecha_creacion = conn.execute(text(
            "SELECT fecha_creacion FROM cita WHERE fecha = '2020-01-15' "
            "AND estudiante_id = (SELECT id FROM usuario WHERE correo = :correo)"
        ), {"correo": _CORREO_HISTORICO}).scalar_one()

    assert fecha_creacion is None
