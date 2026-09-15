# -*- coding: utf-8 -*-
"""
SESAES — Migración puntual A.4.3: agrega 'agenda.sobrecupo' al CHECK
constraint que valida acceso_admin_permiso.permiso.

Contexto: A.4.3 separa agenda.sobrecupo de agenda.gestionar como
capacidades DISTINTAS (ver app.rbac.permissions.Permission y
app.services.sobrecupo_policy_service) para que "poder gestionar una
agenda" no implique automáticamente "poder forzar un sobrecupo". La
tabla acceso_admin_permiso (SA-9.2) restringe a nivel de motor qué
valores de `permiso` puede persistir una fila, mediante el CHECK
constraint `ck_acceso_admin_permiso_valido` — para que una cuenta ADMIN
pueda recibir agenda.sobrecupo (vía el flujo SA-9 de asignación
explícita, sin cambios), ese CHECK debe aceptar también ese valor.

Sigue el mismo patrón que scripts/migrar_a4_1_trazabilidad.py y
scripts/migrar_permisos_admin_sa9.py:
  - PostgreSQL únicamente (rechaza explícitamente cualquier otro
    dialecto — igual que A.4.1, este script usa
    ALTER TABLE ... DROP/ADD CONSTRAINT, sintaxis específica del
    motor);
  - una sola transacción;
  - idempotente: si el CHECK ya acepta 'agenda.sobrecupo' (por haber
    corrido esta migración antes), no hace nada más que validar;
  - NO inserta ninguna fila en acceso_admin_permiso — ninguna cuenta
    ADMIN existente recibe agenda.sobrecupo automáticamente. La
    asignación sigue siendo exclusivamente vía SA-9 (explícita, por
    SUPERADMIN, permiso por permiso);
  - NO toca ninguna otra columna, tabla ni dato existente;
  - localiza el CHECK constraint real POR CONTENIDO (busca el que
    valida la columna `permiso` de esta tabla y ya acepta
    'agenda.gestionar'), no asumiendo ciegamente que su nombre en la
    base de datos real coincide con el que declara el modelo — aunque
    en la práctica coincidirá si la tabla se creó con
    migrar_permisos_admin_sa9.py / Base.metadata, según convención de
    SQLAlchemy;
  - valida el esquema resultante, DENTRO de la misma transacción,
    antes de considerar exitosa la migración: confirma que el catálogo
    final de valores aceptados es EXACTAMENTE el esperado (los 7
    originales + agenda.sobrecupo) — ni de más ni de menos.

Ejecución manual (desde backend/, con el venv activado):

    python -m scripts.migrar_a4_3_permiso_sobrecupo

Repetible: correrlo dos veces seguidas contra la misma base no debe
fallar ni duplicar nada (ver test_a4_3_migracion_permiso_sobrecupo.py,
que exige TEST_POSTGRES_URL y se salta explícitamente si no está
definida — nunca se simula esta garantía contra SQLite).
"""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.database import engine
from app.models.usuario import Usuario  # noqa: F401 — registra 'usuario'
from app.models.acceso_administrativo import (  # noqa: F401
    AccesoAdministrativo,
    AccesoAdminPermiso,
)


class MigracionA43IncompletaError(RuntimeError):
    """
    Las sentencias se ejecutaron sin lanzar excepción, pero el
    catálogo de permisos aceptado por el CHECK real no coincide
    exactamente con lo que A.4.3 necesita. No se asume éxito solo
    porque no hubo excepción — se confirma inspeccionando el esquema
    real, dentro de la misma transacción.
    """


_TABLA = "acceso_admin_permiso"
_COLUMNA = "permiso"

# Catálogo COMPLETO esperado tras la migración: los 7 valores
# originales de SA-9.2/SA-9.6D + 'agenda.sobrecupo' (A.4.3). Ningún
# valor original se retira.
_PERMISOS_ESPERADOS = (
    "usuarios.ver",
    "usuarios.gestionar",
    "profesionales.ver",
    "profesionales.gestionar",
    "agenda.ver",
    "agenda.gestionar",
    "agenda.sobrecupo",
    "reportes.ver",
)

_PERMISO_NUEVO = "agenda.sobrecupo"
# Un permiso que YA existía en el CHECK original — sirve para
# reconocer, por contenido, cuál de los check constraints reflejados
# de la tabla es el que valida `permiso` (en vez de asumir ciegamente
# su nombre).
_PERMISO_ANCLA_ORIGINAL = "agenda.gestionar"

_NOMBRE_CHECK_MODELO = "ck_acceso_admin_permiso_valido"


def _validar_prerrequisitos(conn) -> None:
    inspector = inspect(conn)
    tablas = set(inspector.get_table_names())

    if _TABLA not in tablas:
        raise MigracionA43IncompletaError(
            f"A.4.3 requiere que la tabla {_TABLA} ya exista "
            "(SA-9.2 / scripts.migrar_permisos_admin_sa9)."
        )


def _localizar_check_de_permiso(conn) -> dict | None:
    """
    Busca, entre los check constraints reflejados de `_TABLA`, el que
    valida la columna `permiso` — identificado POR CONTENIDO (su
    `sqltext` menciona la columna `permiso` y ya incluye el valor
    ancla original), no por nombre asumido a ciegas. Devuelve el dict
    crudo de inspector.get_check_constraints() o None si no se
    encuentra ninguno así.
    """
    inspector = inspect(conn)

    for check in inspector.get_check_constraints(_TABLA):
        sqltext = (check.get("sqltext") or "").lower()
        if _COLUMNA in sqltext and f"'{_PERMISO_ANCLA_ORIGINAL}'" in sqltext:
            return check

    return None


def _validar_esquema(conn) -> None:
    errores: list[str] = []

    check = _localizar_check_de_permiso(conn)

    if check is None:
        errores.append(
            f"No se encontró ningún CHECK constraint sobre "
            f"{_TABLA}.{_COLUMNA} que valide un catálogo de valores "
            "(se esperaba encontrar al menos "
            f"{_PERMISO_ANCLA_ORIGINAL!r} en su definición)."
        )
    else:
        sqltext = (check.get("sqltext") or "").lower()

        faltantes = [
            permiso
            for permiso in _PERMISOS_ESPERADOS
            if f"'{permiso}'" not in sqltext
        ]
        if faltantes:
            errores.append(
                "El CHECK constraint de "
                f"{_TABLA}.{_COLUMNA} no acepta: "
                + ", ".join(sorted(faltantes))
                + "."
            )

    if errores:
        raise MigracionA43IncompletaError(
            "Migración A.4.3 incompleta:\n- " + "\n- ".join(errores)
        )


def migrar_a4_3_permiso_sobrecupo(
    bind: Engine = engine,
    *,
    emitir_mensaje: bool = True,
) -> None:
    dialecto = bind.dialect.name
    if dialecto != "postgresql":
        raise RuntimeError(
            "A.4.3: esta migración manual asume PostgreSQL (mismo "
            "supuesto que migrar_a4_1_trazabilidad.py — usa "
            "ALTER TABLE ... DROP/ADD CONSTRAINT, sintaxis específica "
            f"de este motor). No se debe correr contra un dialecto no "
            f"contemplado ({dialecto!r}) sin revisar antes si el SQL "
            "sigue siendo válido."
        )

    with bind.begin() as conn:
        _validar_prerrequisitos(conn)

        check_actual = _localizar_check_de_permiso(conn)

        if check_actual is None:
            raise MigracionA43IncompletaError(
                f"No se encontró el CHECK constraint que valida "
                f"{_TABLA}.{_COLUMNA} — ¿corrió ya "
                "scripts.migrar_permisos_admin_sa9?"
            )

        sqltext_actual = (check_actual.get("sqltext") or "").lower()

        if f"'{_PERMISO_NUEVO}'" in sqltext_actual:
            # Idempotente: una corrida anterior de este mismo script
            # ya dejó el catálogo correcto — no hay nada que alterar.
            pass
        else:
            nombre_check = check_actual.get("name") or _NOMBRE_CHECK_MODELO

            conn.execute(
                text(
                    f'ALTER TABLE {_TABLA} '
                    f'DROP CONSTRAINT "{nombre_check}";'
                )
            )

            valores_sql = ", ".join(f"'{p}'" for p in _PERMISOS_ESPERADOS)
            conn.execute(
                text(
                    f'ALTER TABLE {_TABLA} '
                    f'ADD CONSTRAINT "{nombre_check}" '
                    f"CHECK ({_COLUMNA} IN ({valores_sql}));"
                )
            )

        _validar_esquema(conn)

    if emitir_mensaje:
        print(
            "Migración A.4.3 verificada: "
            f"{_TABLA}.{_COLUMNA} ahora acepta 'agenda.sobrecupo' "
            "además de los valores existentes. No se asignó ese "
            "permiso a ninguna cuenta ADMIN — la asignación sigue "
            "siendo exclusivamente vía SA-9 (explícita, por "
            "SUPERADMIN)."
        )


if __name__ == "__main__":
    migrar_a4_3_permiso_sobrecupo()
