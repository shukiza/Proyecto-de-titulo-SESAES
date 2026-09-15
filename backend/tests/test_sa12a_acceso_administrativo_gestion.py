"""
SA-12A — Gestión productiva de permisos y alcance ADMIN por SUPERADMIN.

Los endpoints se llaman directamente como funciones Python, siguiendo
el patrón de test_sa3_gobernanza.py. La autorización de entrada
roles.gestionar ya está cubierta por SA-3/SA-9; aquí se valida el
contrato de dominio y persistencia.
"""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.acceso_administrativo import (
    AccesoAdministrativo,
    AccesoAdminEspecialidad,
    AccesoAdminPermiso,
)
from app.models.auditoria import Auditoria
from app.models.usuario import Usuario
from app.rbac.permissions import Permission
from app.routers.usuarios import (
    obtener_acceso_administrativo_de_admin,
    obtener_catalogo_acceso_administrativo,
    reemplazar_acceso_administrativo_de_admin,
)
from app.schemas import (
    AccesoAdministrativoGestionUpdate,
    AlcanceAdministrativoGestionIn,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")

    Usuario.__table__.create(engine)
    Auditoria.__table__.create(engine)
    AccesoAdministrativo.__table__.create(engine)
    AccesoAdminEspecialidad.__table__.create(engine)
    AccesoAdminPermiso.__table__.create(engine)

    with Session(engine) as session:
        yield session

    engine.dispose()


def _crear_usuario(
    db,
    *,
    correo,
    rol="admin",
    activo=True,
):
    usuario = Usuario(
        correo=correo,
        password="hash-sa12a",
        rol=rol,
        nombre="Usuario SA-12A",
        activo=activo,
        debe_cambiar_password=False,
    )
    db.add(usuario)
    db.commit()
    db.refresh(usuario)
    return usuario


def _current_user_de(usuario):
    return {
        "id": usuario.id,
        "rol": usuario.rol,
        "correo": usuario.correo,
    }


def _config(
    *,
    perfil,
    tipo,
    permisos=(),
    especialidades=(),
):
    return AccesoAdministrativoGestionUpdate(
        perfil=perfil,
        permisos=list(permisos),
        alcance=AlcanceAdministrativoGestionIn(
            tipo=tipo,
            especialidades=list(especialidades),
        ),
    )


def test_catalogo_expone_techo_real_por_perfil_sin_permisos_reservados():
    resultado = obtener_catalogo_acceso_administrativo(
        current_user={
            "id": 1,
            "rol": "superadmin",
            "correo": "super@utem.cl",
        }
    )

    por_perfil = {
        item["perfil"]: item
        for item in resultado["perfiles"]
    }

    secretaria = por_perfil["secretaria_especialidad"]
    assert secretaria["tipo_alcance"] == "especialidades"
    assert set(secretaria["permisos_permitidos"]) == {
        Permission.USUARIOS_VER.value,
        Permission.PROFESIONALES_VER.value,
        Permission.AGENDA_VER.value,
        Permission.AGENDA_GESTIONAR.value,
        # A.4.3 — agenda.sobrecupo se agrega al TECHO de este perfil
        # (ya tenía agenda.gestionar): techo != concesión, ninguna
        # cuenta existente recibe la fila AccesoAdminPermiso solo por
        # este cambio.
        Permission.AGENDA_SOBRECUPO.value,
    }

    todos = {
        permiso
        for item in resultado["perfiles"]
        for permiso in item["permisos_permitidos"]
    }
    assert Permission.ROLES_GESTIONAR.value not in todos
    assert Permission.AUDITORIA_VER.value not in todos
    assert Permission.CONFIGURACION_GESTIONAR.value not in todos
    assert Permission.REPORTES_CGR_EXPORTAR.value not in todos


def test_get_admin_sin_configuracion_devuelve_fail_closed_explicito(db):
    superadmin = _crear_usuario(
        db,
        correo="super-sin-config@utem.cl",
        rol="superadmin",
    )
    admin = _crear_usuario(
        db,
        correo="admin-sin-config@utem.cl",
        rol="admin",
    )

    resultado = obtener_acceso_administrativo_de_admin(
        admin.id,
        db,
        _current_user_de(superadmin),
    )

    assert resultado == {
        "usuario_id": admin.id,
        "configurado": False,
        "perfil": None,
        "permisos": [],
        "alcance": {
            "tipo": None,
            "especialidades": [],
        },
    }


def test_put_secretaria_especialidad_persiste_permiso_y_scope_normalizados(db):
    superadmin = _crear_usuario(
        db,
        correo="super-config@utem.cl",
        rol="superadmin",
    )
    admin = _crear_usuario(
        db,
        correo="secretaria-odonto@utem.cl",
        rol="admin",
    )

    resultado = reemplazar_acceso_administrativo_de_admin(
        admin.id,
        _config(
            perfil="secretaria_especialidad",
            tipo="especialidades",
            permisos=[
                "agenda.ver",
                "agenda.gestionar",
                "profesionales.ver",
            ],
            especialidades=[
                "  Odontología  ",
                "Medicina   General",
            ],
        ),
        db,
        _current_user_de(superadmin),
    )

    assert resultado["configurado"] is True
    assert resultado["perfil"] == "secretaria_especialidad"
    assert resultado["permisos"] == [
        "agenda.gestionar",
        "agenda.ver",
        "profesionales.ver",
    ]
    assert resultado["alcance"]["tipo"] == "especialidades"
    assert resultado["alcance"]["especialidades"] == [
        "Medicina General",
        "Odontología",
    ]

    acceso = (
        db.query(AccesoAdministrativo)
        .filter(AccesoAdministrativo.usuario_id == admin.id)
        .one()
    )
    assert acceso.perfil == "secretaria_especialidad"

    especialidades = (
        db.query(AccesoAdminEspecialidad)
        .filter(AccesoAdminEspecialidad.acceso_admin_id == acceso.id)
        .all()
    )
    assert {
        (fila.especialidad, fila.especialidad_normalizada)
        for fila in especialidades
    } == {
        ("Odontología", "odontología"),
        ("Medicina General", "medicina general"),
    }


def test_put_reemplaza_configuracion_completa_sin_dejar_residuos(db):
    superadmin = _crear_usuario(
        db,
        correo="super-reemplazo@utem.cl",
        rol="superadmin",
    )
    admin = _crear_usuario(
        db,
        correo="admin-reemplazo@utem.cl",
        rol="admin",
    )

    reemplazar_acceso_administrativo_de_admin(
        admin.id,
        _config(
            perfil="administrador_especialidad",
            tipo="especialidades",
            permisos=[
                "usuarios.ver",
                "agenda.ver",
                "reportes.ver",
            ],
            especialidades=["Odontología"],
        ),
        db,
        _current_user_de(superadmin),
    )

    resultado = reemplazar_acceso_administrativo_de_admin(
        admin.id,
        _config(
            perfil="secretaria_general",
            tipo="institucional",
            permisos=[
                "usuarios.ver",
                "agenda.ver",
            ],
        ),
        db,
        _current_user_de(superadmin),
    )

    assert resultado["perfil"] == "secretaria_general"
    assert resultado["alcance"] == {
        "tipo": "institucional",
        "especialidades": [],
    }
    assert resultado["permisos"] == [
        "agenda.ver",
        "usuarios.ver",
    ]

    acceso = (
        db.query(AccesoAdministrativo)
        .filter(AccesoAdministrativo.usuario_id == admin.id)
        .one()
    )
    assert (
        db.query(AccesoAdminEspecialidad)
        .filter(AccesoAdminEspecialidad.acceso_admin_id == acceso.id)
        .count()
        == 0
    )
    assert {
        fila.permiso
        for fila in (
            db.query(AccesoAdminPermiso)
            .filter(AccesoAdminPermiso.acceso_admin_id == acceso.id)
            .all()
        )
    } == {
        "agenda.ver",
        "usuarios.ver",
    }


def test_put_permiso_fuera_del_techo_devuelve_422_y_conserva_config_anterior(db):
    superadmin = _crear_usuario(
        db,
        correo="super-rollback@utem.cl",
        rol="superadmin",
    )
    admin = _crear_usuario(
        db,
        correo="admin-rollback@utem.cl",
        rol="admin",
    )

    reemplazar_acceso_administrativo_de_admin(
        admin.id,
        _config(
            perfil="secretaria_general",
            tipo="institucional",
            permisos=["agenda.ver"],
        ),
        db,
        _current_user_de(superadmin),
    )

    with pytest.raises(HTTPException) as exc_info:
        reemplazar_acceso_administrativo_de_admin(
            admin.id,
            _config(
                perfil="secretaria_general",
                tipo="institucional",
                permisos=["profesionales.gestionar"],
            ),
            db,
            _current_user_de(superadmin),
        )

    assert exc_info.value.status_code == 422

    resultado = obtener_acceso_administrativo_de_admin(
        admin.id,
        db,
        _current_user_de(superadmin),
    )
    assert resultado["perfil"] == "secretaria_general"
    assert resultado["permisos"] == ["agenda.ver"]


def test_put_especialidades_duplicadas_normalizadas_devuelve_422(db):
    superadmin = _crear_usuario(
        db,
        correo="super-duplicadas@utem.cl",
        rol="superadmin",
    )
    admin = _crear_usuario(
        db,
        correo="admin-duplicadas@utem.cl",
        rol="admin",
    )

    with pytest.raises(HTTPException) as exc_info:
        reemplazar_acceso_administrativo_de_admin(
            admin.id,
            _config(
                perfil="administrador_especialidad",
                tipo="especialidades",
                permisos=["agenda.ver"],
                especialidades=[
                    "Odontología",
                    "  odontología  ",
                ],
            ),
            db,
            _current_user_de(superadmin),
        )

    assert exc_info.value.status_code == 422
    assert db.query(AccesoAdministrativo).count() == 0


def test_superadmin_no_es_target_valido_de_acceso_admin(db):
    actor = _crear_usuario(
        db,
        correo="actor-super@utem.cl",
        rol="superadmin",
    )
    target = _crear_usuario(
        db,
        correo="target-super@utem.cl",
        rol="superadmin",
    )

    with pytest.raises(HTTPException) as exc_info:
        reemplazar_acceso_administrativo_de_admin(
            target.id,
            _config(
                perfil="administrador_general",
                tipo="institucional",
                permisos=["agenda.ver"],
            ),
            db,
            _current_user_de(actor),
        )

    assert exc_info.value.status_code == 409
    assert db.query(AccesoAdministrativo).count() == 0


def test_put_registra_auditoria_con_actor_current_user(db):
    superadmin = _crear_usuario(
        db,
        correo="super-auditoria@utem.cl",
        rol="superadmin",
    )
    admin = _crear_usuario(
        db,
        correo="admin-auditoria@utem.cl",
        rol="admin",
    )

    reemplazar_acceso_administrativo_de_admin(
        admin.id,
        _config(
            perfil="administrador_general",
            tipo="institucional",
            permisos=["agenda.ver"],
        ),
        db,
        _current_user_de(superadmin),
    )

    evento = (
        db.query(Auditoria)
        .filter(Auditoria.accion == "Configuró acceso administrativo")
        .one()
    )

    assert evento.usuario_id == superadmin.id
    assert evento.actor_rol == "superadmin"
    assert evento.entidad == "usuario"
    assert evento.entidad_id == admin.id
    assert evento.resultado == "exito"


def test_schema_gestion_acceso_rechaza_campos_extra():
    with pytest.raises(ValidationError):
        AccesoAdministrativoGestionUpdate(
            perfil="administrador_general",
            permisos=["agenda.ver"],
            alcance={
                "tipo": "institucional",
                "especialidades": [],
            },
            roles=["superadmin"],
        )
