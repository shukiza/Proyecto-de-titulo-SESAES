import pytest

from app.rbac.admin_access import (
    PERFIL_PERMISOS_PERMITIDOS,
    PERMISOS_NO_ADMINISTRATIVOS,
    PERMISOS_RESERVADOS_SUPERADMIN,
    PerfilAccesoAdmin,
    permisos_permitidos_para_perfil,
    validar_permisos_para_perfil,
)
from app.rbac.permissions import Permission, has_permission


def test_catalogo_incorpora_permisos_de_lectura_administrativa():
    assert Permission.USUARIOS_VER.value == "usuarios.ver"
    assert Permission.PROFESIONALES_VER.value == "profesionales.ver"
    assert Permission.AGENDA_VER.value == "agenda.ver"


def test_superadmin_conserva_lectura_administrativa_explicita():
    assert has_permission("superadmin", Permission.USUARIOS_VER)
    assert has_permission("superadmin", Permission.PROFESIONALES_VER)
    assert has_permission("superadmin", Permission.AGENDA_VER)


@pytest.mark.parametrize(
    "perfil",
    [
        PerfilAccesoAdmin.ADMINISTRADOR_GENERAL,
        PerfilAccesoAdmin.ADMINISTRADOR_ESPECIALIDAD,
    ],
)
def test_administradores_pueden_recibir_lectura_gestion_y_reportes(perfil):
    permisos = permisos_permitidos_para_perfil(perfil)

    assert Permission.USUARIOS_VER in permisos
    assert Permission.USUARIOS_GESTIONAR in permisos
    assert Permission.PROFESIONALES_VER in permisos
    assert Permission.PROFESIONALES_GESTIONAR in permisos
    assert Permission.AGENDA_VER in permisos
    assert Permission.AGENDA_GESTIONAR in permisos
    # A.4.3 — capacidad distinta de AGENDA_GESTIONAR, en el TECHO de
    # los mismos perfiles que ya podían gestionar agenda.
    assert Permission.AGENDA_SOBRECUPO in permisos
    assert Permission.REPORTES_VER in permisos


@pytest.mark.parametrize(
    "perfil",
    [
        PerfilAccesoAdmin.SECRETARIA_GENERAL,
        PerfilAccesoAdmin.SECRETARIA_ESPECIALIDAD,
    ],
)
def test_secretarias_solo_tienen_techo_operativo(perfil):
    permisos = permisos_permitidos_para_perfil(perfil)

    assert permisos == {
        Permission.USUARIOS_VER,
        Permission.PROFESIONALES_VER,
        Permission.AGENDA_VER,
        Permission.AGENDA_GESTIONAR,
        # A.4.3 — techo != concesión (ver test_sa9_migracion_permisos_admin
        # / SA-9 en general): que el techo lo permita no le da la
        # capacidad a ninguna cuenta secretaría existente sin una fila
        # AccesoAdminPermiso explícita.
        Permission.AGENDA_SOBRECUPO,
    }


@pytest.mark.parametrize("perfil", list(PerfilAccesoAdmin))
def test_ningun_perfil_admin_puede_recibir_permisos_reservados(perfil):
    assert PERFIL_PERMISOS_PERMITIDOS[perfil].isdisjoint(
        PERMISOS_RESERVADOS_SUPERADMIN
    )


@pytest.mark.parametrize("perfil", list(PerfilAccesoAdmin))
def test_ningun_perfil_admin_puede_recibir_permisos_no_administrativos(perfil):
    assert PERFIL_PERMISOS_PERMITIDOS[perfil].isdisjoint(
        PERMISOS_NO_ADMINISTRATIVOS
    )


def test_validar_permisos_acepta_subconjunto_explicito():
    resultado = validar_permisos_para_perfil(
        perfil="secretaria_general",
        permisos=["usuarios.ver", "agenda.ver"],
    )

    assert resultado == {
        Permission.USUARIOS_VER,
        Permission.AGENDA_VER,
    }


def test_validar_permisos_no_autocompleta_defaults():
    assert validar_permisos_para_perfil(
        perfil="administrador_general",
        permisos=[],
    ) == frozenset()


@pytest.mark.parametrize(
    "permiso",
    [
        Permission.ROLES_GESTIONAR,
        Permission.AUDITORIA_VER,
        Permission.CONFIGURACION_GESTIONAR,
        Permission.REPORTES_CGR_EXPORTAR,
        Permission.FICHA_VER_ASIGNADA,
        Permission.ATENCIONES_REGISTRAR,
    ],
)
def test_rechaza_permisos_reservados_y_clinicos(permiso):
    with pytest.raises(ValueError):
        validar_permisos_para_perfil(
            perfil="administrador_general",
            permisos=[permiso],
        )


def test_secretaria_no_puede_recibir_profesionales_gestionar():
    with pytest.raises(ValueError):
        validar_permisos_para_perfil(
            perfil="secretaria_general",
            permisos=[Permission.PROFESIONALES_GESTIONAR],
        )


def test_permiso_desconocido_falla_cerrado():
    with pytest.raises(ValueError):
        validar_permisos_para_perfil(
            perfil="administrador_general",
            permisos=["sistema.control_total"],
        )
