# -*- coding: utf-8 -*-
"""
SESAES — A.4.3: la migración manual del CHECK de
acceso_admin_permiso.permiso corre dos veces sin romper, y ejerce el
script real (no solo la definición del modelo SQLAlchemy).

Igual que test_a4_1_migracion_postgres.py, esto requiere una base
PostgreSQL de test real (TEST_POSTGRES_URL): el script usa sintaxis
específica de este motor (ALTER TABLE ... DROP/ADD CONSTRAINT) y
rechaza explícitamente cualquier otro dialecto — no tiene sentido, ni
es posible, ejercitarlo contra el SQLite que usa el resto de la suite.

Si TEST_POSTGRES_URL no está definida, el test se salta explícitamente
— NUNCA se simula "pasar" esta garantía sin una base real.

El fixture simula una base YA desplegada ANTES de A.4.3: crea
`acceso_admin_permiso` a mano, con el CHECK ORIGINAL de 7 valores (sin
'agenda.sobrecupo') — deliberadamente NO usa
`AccesoAdminPermiso.__table__.create()`, porque el modelo actual ya
declara el catálogo de 8 valores (A.4.3), lo que dejaría el "antes" ya
migrado y no ejercitaría el ALTER real del script.

Cómo correrlo (dos veces seguidas, para demostrar idempotencia real):
    TEST_POSTGRES_URL="postgresql+psycopg2://..." \\
        pytest tests/test_a4_3_migracion_permiso_sobrecupo.py -m postgres -q
    TEST_POSTGRES_URL="postgresql+psycopg2://..." \\
        pytest tests/test_a4_3_migracion_permiso_sobrecupo.py -m postgres -q
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.postgres

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

if not TEST_POSTGRES_URL:
    pytest.skip(
        "A.4.3: TEST_POSTGRES_URL no está definida — la migración "
        "manual del CHECK de acceso_admin_permiso (ALTER TABLE ... "
        "DROP/ADD CONSTRAINT, corrida dos veces) NO se validó contra "
        "Postgres real en esta ejecución. Define TEST_POSTGRES_URL "
        "apuntando a un Postgres de test DESECHABLE para ejercitar "
        "esta garantía — nunca contra una base productiva.",
        allow_module_level=True,
    )

from sqlalchemy import create_engine, inspect, text

from scripts.migrar_acceso_administrativo_sa8 import migrar_acceso_administrativo_sa8
from scripts.migrar_a4_3_permiso_sobrecupo import (  # noqa: E402
    MigracionA43IncompletaError,
    migrar_a4_3_permiso_sobrecupo,
)

_TABLA = "acceso_admin_permiso"

# El CHECK EXACTO tal como existía justo ANTES de A.4.3 (7 valores,
# sin 'agenda.sobrecupo') — ver git history de
# app/models/acceso_administrativo.py.
_SQL_CHECK_PRE_A43 = (
    "permiso IN ("
    "'usuarios.ver', 'usuarios.gestionar', "
    "'profesionales.ver', 'profesionales.gestionar', "
    "'agenda.ver', 'agenda.gestionar', "
    "'reportes.ver'"
    ")"
)


def _crear_tabla_permiso_pre_a43(engine) -> None:
    """
    Recrea `acceso_admin_permiso` desde cero con el esquema PRE-A.4.3
    (CHECK de 7 valores) — deliberadamente NO usa el modelo actual.
    Requiere que `acceso_administrativo` ya exista (SA-8, corre antes
    en el fixture).
    """
    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {_TABLA} CASCADE;"))
        conn.execute(text(
            f"CREATE TABLE {_TABLA} ("
            "id SERIAL PRIMARY KEY, "
            "acceso_admin_id INTEGER NOT NULL "
            "REFERENCES acceso_administrativo(id) ON DELETE CASCADE, "
            "permiso VARCHAR(96) NOT NULL, "
            "CONSTRAINT uq_acceso_admin_permiso UNIQUE (acceso_admin_id, permiso), "
            f"CONSTRAINT ck_acceso_admin_permiso_valido CHECK ({_SQL_CHECK_PRE_A43})"
            ");"
        ))


@pytest.fixture()
def engine_postgres_test():
    engine = create_engine(TEST_POSTGRES_URL)

    with engine.begin() as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {_TABLA} CASCADE;"))
        conn.execute(text("DROP TABLE IF EXISTS acceso_admin_especialidad CASCADE;"))
        conn.execute(text("DROP TABLE IF EXISTS acceso_administrativo CASCADE;"))

    # Este sandbox de test parte de un Postgres COMPLETAMENTE vacío —
    # a diferencia de test_a4_1_migracion_postgres.py (que asume
    # usuario/profesional/cita ya desplegados), acá se garantiza que
    # `usuario` exista (FK de acceso_administrativo) creando el
    # esquema completo de la app una sola vez, con checkfirst
    # implícito de create_all(). Esto NO reemplaza correr esta
    # migración contra un TEST_POSTGRES_URL con datos reales — sigue
    # siendo la misma URL que usa el resto de la suite Postgres.
    import app.models.init  # noqa: F401
    import app.models.solicitud_horario  # noqa: F401
    from app.database import Base as AppBase

    AppBase.metadata.create_all(bind=engine)

    # SA-8 real (no simulado) — acceso_admin_permiso depende de su FK.
    migrar_acceso_administrativo_sa8(bind=engine, emitir_mensaje=False)
    _crear_tabla_permiso_pre_a43(engine)

    try:
        yield engine
    finally:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {_TABLA} CASCADE;"))
            conn.execute(text("DROP TABLE IF EXISTS acceso_admin_especialidad CASCADE;"))
            conn.execute(text("DROP TABLE IF EXISTS acceso_administrativo CASCADE;"))
        engine.dispose()


def _sqltext_check_permiso(engine) -> str:
    inspector = inspect(engine)
    for check in inspector.get_check_constraints(_TABLA):
        sqltext = (check.get("sqltext") or "").lower()
        if "permiso" in sqltext and "'agenda.gestionar'" in sqltext:
            return sqltext
    raise AssertionError(f"no se encontró el CHECK de {_TABLA}.permiso")


def test_migracion_a43_rechaza_dialecto_no_postgres():
    from sqlalchemy import create_engine as _create_engine

    sqlite_engine = _create_engine("sqlite:///:memory:")
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        migrar_a4_3_permiso_sobrecupo(bind=sqlite_engine, emitir_mensaje=False)


def test_check_previo_no_acepta_agenda_sobrecupo(engine_postgres_test):
    """Confirma que el fixture realmente parte del estado PRE-A.4.3:
    el motor debe rechazar ese valor ANTES de correr la migración —
    específicamente por el CHECK (no por una FK inexistente), así que
    se usa una fila de acceso_administrativo real."""
    correo = "admin-a43-check-previo@sesaes.cl"
    with engine_postgres_test.begin() as conn:
        conn.execute(text("DELETE FROM usuario WHERE correo = :correo;"), {"correo": correo})
        usuario_id = conn.execute(text(
            "INSERT INTO usuario (correo, password, rol, activo) "
            "VALUES (:correo, 'x', 'admin', true) RETURNING id;"
        ), {"correo": correo}).scalar_one()
        acceso_admin_id = conn.execute(text(
            "INSERT INTO acceso_administrativo (usuario_id, perfil, tipo_alcance) "
            "VALUES (:usuario_id, 'administrador_general', 'institucional') "
            "RETURNING id;"
        ), {"usuario_id": usuario_id}).scalar_one()

    try:
        with pytest.raises(Exception, match="ck_acceso_admin_permiso_valido"):
            with engine_postgres_test.begin() as conn:
                conn.execute(text(
                    "INSERT INTO acceso_admin_permiso (acceso_admin_id, permiso) "
                    "VALUES (:acceso_admin_id, 'agenda.sobrecupo');"
                ), {"acceso_admin_id": acceso_admin_id})
    finally:
        with engine_postgres_test.begin() as conn:
            conn.execute(text(
                "DELETE FROM acceso_administrativo WHERE usuario_id IN ("
                "SELECT id FROM usuario WHERE correo = :correo);"
            ), {"correo": correo})
            conn.execute(text("DELETE FROM usuario WHERE correo = :correo;"), {"correo": correo})


def test_migracion_a43_agrega_agenda_sobrecupo_al_check(engine_postgres_test):
    migrar_a4_3_permiso_sobrecupo(bind=engine_postgres_test, emitir_mensaje=False)

    sqltext = _sqltext_check_permiso(engine_postgres_test)
    assert "'agenda.sobrecupo'" in sqltext


def test_migracion_a43_conserva_catalogo_previo_intacto(engine_postgres_test):
    """Ninguno de los 7 valores originales se pierde en el ALTER."""
    migrar_a4_3_permiso_sobrecupo(bind=engine_postgres_test, emitir_mensaje=False)

    sqltext = _sqltext_check_permiso(engine_postgres_test)
    for permiso in (
        "usuarios.ver", "usuarios.gestionar",
        "profesionales.ver", "profesionales.gestionar",
        "agenda.ver", "agenda.gestionar", "reportes.ver",
    ):
        assert f"'{permiso}'" in sqltext


def test_migracion_a43_no_agrega_filas_ni_toca_datos(engine_postgres_test):
    """La migración es puramente de esquema: cero INSERT/UPDATE/DELETE
    — ninguna cuenta ADMIN recibe agenda.sobrecupo automáticamente."""
    with engine_postgres_test.begin() as conn:
        antes = conn.execute(
            text(f"SELECT COUNT(*) FROM {_TABLA}")
        ).scalar_one()

    migrar_a4_3_permiso_sobrecupo(bind=engine_postgres_test, emitir_mensaje=False)

    with engine_postgres_test.begin() as conn:
        despues = conn.execute(
            text(f"SELECT COUNT(*) FROM {_TABLA}")
        ).scalar_one()

    assert antes == 0
    assert despues == 0


def test_migracion_a43_corre_dos_veces_sin_romper(engine_postgres_test):
    migrar_a4_3_permiso_sobrecupo(bind=engine_postgres_test, emitir_mensaje=False)
    migrar_a4_3_permiso_sobrecupo(bind=engine_postgres_test, emitir_mensaje=False)

    sqltext = _sqltext_check_permiso(engine_postgres_test)
    assert "'agenda.sobrecupo'" in sqltext

    with engine_postgres_test.begin() as conn:
        total = conn.execute(
            text(f"SELECT COUNT(*) FROM {_TABLA}")
        ).scalar_one()
    assert total == 0


def test_migracion_a43_ahora_permite_persistir_agenda_sobrecupo(engine_postgres_test):
    """Confirma el efecto real y observable: tras migrar, el motor SÍ
    acepta una fila con 'agenda.sobrecupo' (la asignación en sí sigue
    siendo responsabilidad exclusiva del flujo SA-9 explícito — este
    test solo prueba que el esquema ya lo permite, no que alguna
    cuenta lo reciba)."""
    migrar_a4_3_permiso_sobrecupo(bind=engine_postgres_test, emitir_mensaje=False)

    correo = "admin-a43-migracion@sesaes.cl"
    with engine_postgres_test.begin() as conn:
        conn.execute(text(
            "DELETE FROM acceso_admin_permiso WHERE acceso_admin_id IN ("
            "SELECT id FROM acceso_administrativo WHERE usuario_id IN ("
            "SELECT id FROM usuario WHERE correo = :correo));"
        ), {"correo": correo})
        conn.execute(text(
            "DELETE FROM acceso_administrativo WHERE usuario_id IN ("
            "SELECT id FROM usuario WHERE correo = :correo);"
        ), {"correo": correo})
        conn.execute(text("DELETE FROM usuario WHERE correo = :correo;"), {"correo": correo})

        usuario_id = conn.execute(text(
            "INSERT INTO usuario (correo, password, rol, activo) "
            "VALUES (:correo, 'x', 'admin', true) RETURNING id;"
        ), {"correo": correo}).scalar_one()

        acceso_admin_id = conn.execute(text(
            "INSERT INTO acceso_administrativo (usuario_id, perfil, tipo_alcance) "
            "VALUES (:usuario_id, 'administrador_general', 'institucional') "
            "RETURNING id;"
        ), {"usuario_id": usuario_id}).scalar_one()

        conn.execute(text(
            "INSERT INTO acceso_admin_permiso (acceso_admin_id, permiso) "
            "VALUES (:acceso_admin_id, 'agenda.sobrecupo');"
        ), {"acceso_admin_id": acceso_admin_id})

        total = conn.execute(text(
            "SELECT COUNT(*) FROM acceso_admin_permiso "
            "WHERE acceso_admin_id = :acceso_admin_id AND permiso = 'agenda.sobrecupo'"
        ), {"acceso_admin_id": acceso_admin_id}).scalar_one()

    try:
        assert total == 1
    finally:
        with engine_postgres_test.begin() as conn:
            conn.execute(text(
                "DELETE FROM acceso_admin_permiso WHERE acceso_admin_id IN ("
                "SELECT id FROM acceso_administrativo WHERE usuario_id IN ("
                "SELECT id FROM usuario WHERE correo = :correo));"
            ), {"correo": correo})
            conn.execute(text(
                "DELETE FROM acceso_administrativo WHERE usuario_id IN ("
                "SELECT id FROM usuario WHERE correo = :correo);"
            ), {"correo": correo})
            conn.execute(text("DELETE FROM usuario WHERE correo = :correo;"), {"correo": correo})
