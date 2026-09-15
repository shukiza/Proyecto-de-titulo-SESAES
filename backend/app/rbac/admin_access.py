"""
SESAES - SA-8: perfiles y alcance administrativo.

Esta capa NO concede permisos. Solo modela:
- perfil administrativo;
- tipo de alcance;
- especialidades incluidas en un alcance limitado.

La autorizacion efectiva se implementara en SA-9 combinando:
permiso + alcance + estado de la cuenta.

SUPERADMIN no usa esta configuracion y no obtiene acceso clinico por ella.
"""

from __future__ import annotations

import enum
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from app.rbac.permissions import Permission


class PerfilAccesoAdmin(str, enum.Enum):
    ADMINISTRADOR_GENERAL = "administrador_general"
    ADMINISTRADOR_ESPECIALIDAD = "administrador_especialidad"
    SECRETARIA_GENERAL = "secretaria_general"
    SECRETARIA_ESPECIALIDAD = "secretaria_especialidad"


class TipoAlcanceAdmin(str, enum.Enum):
    INSTITUCIONAL = "institucional"
    ESPECIALIDADES = "especialidades"


PERFILES_INSTITUCIONALES = frozenset(
    {
        PerfilAccesoAdmin.ADMINISTRADOR_GENERAL,
        PerfilAccesoAdmin.SECRETARIA_GENERAL,
    }
)

PERFILES_ESPECIALIDAD = frozenset(
    {
        PerfilAccesoAdmin.ADMINISTRADOR_ESPECIALIDAD,
        PerfilAccesoAdmin.SECRETARIA_ESPECIALIDAD,
    }
)


@dataclass(frozen=True)
class EspecialidadAlcance:
    nombre: str
    normalizada: str


def limpiar_especialidad(valor: str) -> str:
    """
    Conserva una representacion legible pero elimina diferencias triviales:
    Unicode compatible, espacios externos y espacios repetidos.
    """
    if not isinstance(valor, str):
        raise ValueError("La especialidad debe ser texto.")

    limpia = " ".join(unicodedata.normalize("NFKC", valor).split())

    if not limpia:
        raise ValueError("La especialidad no puede estar vacia.")

    return limpia


def normalizar_especialidad(valor: str) -> EspecialidadAlcance:
    """
    Genera nombre visible + clave estable para comparar/deduplicar.

    No valida contra un catalogo fijo: Profesional.especialidad sigue siendo
    texto libre en la arquitectura actual de SESAES.
    """
    nombre = limpiar_especialidad(valor)

    return EspecialidadAlcance(
        nombre=nombre,
        normalizada=nombre.casefold(),
    )


def validar_configuracion_acceso_admin(
    *,
    rol_usuario: object,
    perfil: PerfilAccesoAdmin | str,
    tipo_alcance: TipoAlcanceAdmin | str,
    especialidades: Iterable[str] | None = None,
) -> tuple[
    PerfilAccesoAdmin,
    TipoAlcanceAdmin,
    tuple[EspecialidadAlcance, ...],
]:
    """
    Valida las invariantes de SA-8.

    Reglas:
    - solo rol base ADMIN puede tener esta configuracion;
    - perfiles generales -> alcance institucional y cero especialidades;
    - perfiles de especialidad -> alcance especialidades y al menos una;
    - no se permiten especialidades duplicadas tras normalizacion.
    """
    rol_valor = getattr(rol_usuario, "value", rol_usuario)
    rol_normalizado = str(rol_valor or "").strip().lower()

    if rol_normalizado != "admin":
        raise ValueError(
            "Solo una cuenta con rol base admin puede tener "
            "configuracion de acceso administrativo."
        )

    try:
        perfil_enum = PerfilAccesoAdmin(perfil)
    except (TypeError, ValueError) as exc:
        raise ValueError("Perfil administrativo invalido.") from exc

    try:
        alcance_enum = TipoAlcanceAdmin(tipo_alcance)
    except (TypeError, ValueError) as exc:
        raise ValueError("Tipo de alcance administrativo invalido.") from exc

    normalizadas: list[EspecialidadAlcance] = []
    claves: set[str] = set()

    for valor in especialidades or ():
        especialidad = normalizar_especialidad(valor)

        if especialidad.normalizada in claves:
            raise ValueError(
                "No se permiten especialidades duplicadas en el alcance."
            )

        claves.add(especialidad.normalizada)
        normalizadas.append(especialidad)

    if perfil_enum in PERFILES_INSTITUCIONALES:
        if alcance_enum is not TipoAlcanceAdmin.INSTITUCIONAL:
            raise ValueError(
                "Un perfil general debe tener alcance institucional."
            )

        if normalizadas:
            raise ValueError(
                "Un alcance institucional no debe declarar especialidades."
            )

    elif perfil_enum in PERFILES_ESPECIALIDAD:
        if alcance_enum is not TipoAlcanceAdmin.ESPECIALIDADES:
            raise ValueError(
                "Un perfil de especialidad debe tener alcance por especialidades."
            )

        if not normalizadas:
            raise ValueError(
                "Un perfil de especialidad requiere al menos una especialidad."
            )

    return perfil_enum, alcance_enum, tuple(normalizadas)
# ??????????????????????????????????????????????????????????????????
# SA-9.1 - Techo de permisos por perfil administrativo
# ??????????????????????????????????????????????????????????????????
#
# Esta matriz NO concede permisos automaticamente.
# Define el subconjunto MAXIMO que SUPERADMIN podra asignar a una
# cuenta ADMIN de cada perfil en SA-9.2.
#
# Autorizacion efectiva:
#
#   permiso asignado explicitamente
#   + permitido por perfil
#   + recurso dentro del alcance
#   + cuenta activa
#
# Permisos reservados, clinicos y de autoservicio nunca forman
# parte del techo administrativo.

PERMISOS_ADMIN_OPERATIVOS = frozenset(
    {
        Permission.USUARIOS_VER,
        Permission.USUARIOS_GESTIONAR,
        Permission.PROFESIONALES_VER,
        Permission.PROFESIONALES_GESTIONAR,
        Permission.AGENDA_VER,
        Permission.AGENDA_GESTIONAR,
        # A.4.3 — techo, no concesión: que el catálogo lo permita NO
        # significa que ninguna cuenta lo tenga ya asignado. Ver
        # PERFIL_PERMISOS_PERMITIDOS más abajo y el flujo SA-9 de
        # asignación explícita.
        Permission.AGENDA_SOBRECUPO,
        Permission.REPORTES_VER,
    }
)

PERMISOS_RESERVADOS_SUPERADMIN = frozenset(
    {
        Permission.CONFIGURACION_GESTIONAR,
        Permission.REPORTES_CGR_EXPORTAR,
        Permission.AUDITORIA_VER,
        Permission.ROLES_GESTIONAR,
    }
)

PERMISOS_NO_ADMINISTRATIVOS = frozenset(
    {
        Permission.ATENCIONES_VER_ASIGNADAS,
        Permission.ATENCIONES_REGISTRAR,
        Permission.FICHA_VER_ASIGNADA,
        Permission.FICHA_EDITAR_ASIGNADA,
        Permission.AGENDA_VER_PROFESIONAL,
        Permission.AGENDA_GESTIONAR_PROPIA,
        Permission.CITAS_GESTIONAR_PROPIAS,
        Permission.PERFIL_VER_PROPIO,
        Permission.DOCUMENTOS_VER_PROPIOS,
    }
)

# A.4.3 — AGENDA_SOBRECUPO se agrega al TECHO de los cuatro perfiles
# porque los cuatro ya incluyen AGENDA_GESTIONAR (ninguno de los
# perfiles actuales es de solo lectura de agenda). Técho != concesión:
# ninguna cuenta ADMIN existente recibe una fila AccesoAdminPermiso
# nueva por este cambio — sigue exigiendo asignación explícita vía
# SA-9 (ver validar_permisos_para_perfil()/endpoint de asignación).
PERFIL_PERMISOS_PERMITIDOS = {
    PerfilAccesoAdmin.ADMINISTRADOR_GENERAL: frozenset(
        {
            Permission.USUARIOS_VER,
            Permission.USUARIOS_GESTIONAR,
            Permission.PROFESIONALES_VER,
            Permission.PROFESIONALES_GESTIONAR,
            Permission.AGENDA_VER,
            Permission.AGENDA_GESTIONAR,
            Permission.AGENDA_SOBRECUPO,
            Permission.REPORTES_VER,
        }
    ),
    PerfilAccesoAdmin.ADMINISTRADOR_ESPECIALIDAD: frozenset(
        {
            Permission.USUARIOS_VER,
            Permission.USUARIOS_GESTIONAR,
            Permission.PROFESIONALES_VER,
            Permission.PROFESIONALES_GESTIONAR,
            Permission.AGENDA_VER,
            Permission.AGENDA_GESTIONAR,
            Permission.AGENDA_SOBRECUPO,
            Permission.REPORTES_VER,
        }
    ),
    PerfilAccesoAdmin.SECRETARIA_GENERAL: frozenset(
        {
            Permission.USUARIOS_VER,
            Permission.PROFESIONALES_VER,
            Permission.AGENDA_VER,
            Permission.AGENDA_GESTIONAR,
            Permission.AGENDA_SOBRECUPO,
        }
    ),
    PerfilAccesoAdmin.SECRETARIA_ESPECIALIDAD: frozenset(
        {
            Permission.USUARIOS_VER,
            Permission.PROFESIONALES_VER,
            Permission.AGENDA_VER,
            Permission.AGENDA_GESTIONAR,
            Permission.AGENDA_SOBRECUPO,
        }
    ),
}


def permisos_permitidos_para_perfil(
    perfil: PerfilAccesoAdmin | str,
) -> frozenset[Permission]:
    try:
        perfil_enum = PerfilAccesoAdmin(perfil)
    except (TypeError, ValueError) as exc:
        raise ValueError("Perfil administrativo invalido.") from exc

    return PERFIL_PERMISOS_PERMITIDOS[perfil_enum]


def validar_permisos_para_perfil(
    *,
    perfil: PerfilAccesoAdmin | str,
    permisos: Iterable[Permission | str],
) -> frozenset[Permission]:
    """
    Valida un subconjunto explicito de permisos para un perfil.

    No completa defaults ni concede permisos automaticamente.
    Un conjunto vacio es valido y representa fail-closed.
    """
    permitidos = permisos_permitidos_para_perfil(perfil)
    resultado: set[Permission] = set()

    for permiso in permisos:
        try:
            permiso_enum = Permission(permiso)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Permiso administrativo invalido: {permiso!r}."
            ) from exc

        if permiso_enum not in permitidos:
            raise ValueError(
                "El permiso "
                f"{permiso_enum.value!r} no esta permitido para "
                f"el perfil {PerfilAccesoAdmin(perfil).value!r}."
            )

        resultado.add(permiso_enum)

    return frozenset(resultado)
