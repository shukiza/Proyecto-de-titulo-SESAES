"""
SESAES - SA-9.3: resolucion efectiva de autorizacion administrativa.

Esta capa combina:
- estado ACTUAL de Usuario;
- rol base ACTUAL;
- configuracion acceso_administrativo;
- techo de permisos del perfil;
- permisos persistidos explicitamente;
- alcance institucional o por especialidades.

No modifica require_permission() ni routers todavia. SA-9.4/SA-9.5
conectaran este resolver de forma progresiva.

Reglas principales:
- ADMIN sin configuracion -> fail closed;
- ADMIN sin permiso persistido -> fail closed;
- permiso fuera del techo del perfil -> fail closed;
- configuracion inconsistente -> fail closed;
- SUPERADMIN no depende de acceso_administrativo;
- roles no ADMIN conservan sus capacidades RBAC de rol;
- ningun rol recibe wildcard ni bypass clinico.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.acceso_administrativo import (
    AccesoAdministrativo,
    AccesoAdminEspecialidad,
    AccesoAdminPermiso,
)
from app.models.usuario import Usuario
from app.rbac.admin_access import (
    EspecialidadAlcance,
    PerfilAccesoAdmin,
    TipoAlcanceAdmin,
    normalizar_especialidad,
    permisos_permitidos_para_perfil,
    validar_configuracion_acceso_admin,
)
from app.rbac.permissions import Permission, ROLE_DEFAULT_PERMISSIONS, has_permission
from app.rbac.roles import Role, normalizar_rol


@dataclass(frozen=True)
class ContextoAutorizacionAdmin:
    acceso_id: int
    perfil: PerfilAccesoAdmin
    tipo_alcance: TipoAlcanceAdmin
    especialidades_normalizadas: frozenset[str]
    # Conserva las mismas especialidades visibles que YA fueron
    # normalizadas y validadas por validar_configuracion_acceso_admin().
    # El endpoint SA-10 nunca reconstruye nombres desde claves guardadas.
    especialidades: tuple[EspecialidadAlcance, ...] = ()


@dataclass(frozen=True)
class AccesoAdministrativoEfectivo:
    """
    Snapshot efectivo para presentar la autorización administrativa
    actual al propio ADMIN/SUPERADMIN.

    No concede permisos: resume reglas ya resueltas server-side.
    """

    rol: Role
    perfil: PerfilAccesoAdmin | None
    tipo_alcance: TipoAlcanceAdmin
    especialidades: tuple[EspecialidadAlcance, ...]
    permisos: frozenset[Permission]


def _normalizar_permiso(
    permiso: Permission | str,
) -> Permission | None:
    try:
        return Permission(permiso)
    except (TypeError, ValueError):
        return None


def _cargar_usuario_activo(
    db: Session,
    usuario_id: object,
) -> Usuario | None:
    if isinstance(usuario_id, bool) or not isinstance(usuario_id, int):
        return None

    usuario = (
        db.query(Usuario)
        .filter(Usuario.id == usuario_id)
        .first()
    )

    if usuario is None or usuario.activo is not True:
        return None

    return usuario


def _cargar_contexto_admin_desde_usuario(
    db: Session,
    usuario: Usuario,
) -> ContextoAutorizacionAdmin | None:
    rol = normalizar_rol(usuario.rol)

    if rol is not Role.ADMIN:
        return None

    acceso = (
        db.query(AccesoAdministrativo)
        .filter(
            AccesoAdministrativo.usuario_id == usuario.id
        )
        .first()
    )

    if acceso is None:
        return None

    filas_especialidad = (
        db.query(AccesoAdminEspecialidad)
        .filter(
            AccesoAdminEspecialidad.acceso_admin_id == acceso.id
        )
        .all()
    )

    # No confiamos en especialidad_normalizada almacenada para autorizar.
    # Recalculamos desde el nombre visible y revalidamos TODA la
    # configuracion para detectar tambien:
    # - perfil especialidad sin especialidades;
    # - perfil institucional con especialidades residuales;
    # - duplicados normalizados;
    # - perfil/alcance incompatible.
    try:
        perfil, tipo_alcance, especialidades = (
            validar_configuracion_acceso_admin(
                rol_usuario=rol.value,
                perfil=acceso.perfil,
                tipo_alcance=acceso.tipo_alcance,
                especialidades=[
                    fila.especialidad
                    for fila in filas_especialidad
                ],
            )
        )
    except (TypeError, ValueError):
        return None

    return ContextoAutorizacionAdmin(
        acceso_id=acceso.id,
        perfil=perfil,
        tipo_alcance=tipo_alcance,
        especialidades_normalizadas=frozenset(
            especialidad.normalizada
            for especialidad in especialidades
        ),
        especialidades=especialidades,
    )


def obtener_contexto_admin(
    db: Session,
    usuario_id: object,
) -> ContextoAutorizacionAdmin | None:
    """
    Obtiene una configuracion ADMIN valida y activa.

    Cualquier ausencia o inconsistencia falla cerrado con None.
    """
    usuario = _cargar_usuario_activo(db, usuario_id)

    if usuario is None:
        return None

    return _cargar_contexto_admin_desde_usuario(
        db,
        usuario,
    )


def _tiene_permiso_admin_con_contexto(
    db: Session,
    contexto: ContextoAutorizacionAdmin,
    permiso: Permission,
) -> bool:
    permitidos = permisos_permitidos_para_perfil(
        contexto.perfil
    )

    if permiso not in permitidos:
        return False

    registro = (
        db.query(AccesoAdminPermiso.id)
        .filter(
            AccesoAdminPermiso.acceso_admin_id
            == contexto.acceso_id,
            AccesoAdminPermiso.permiso
            == permiso.value,
        )
        .first()
    )

    return registro is not None


def _permisos_admin_efectivos_con_contexto(
    db: Session,
    contexto: ContextoAutorizacionAdmin,
) -> frozenset[Permission]:
    """
    Lista todos los permisos ADMIN realmente efectivos del contexto.

    Replica exactamente la semántica de
    _tiene_permiso_admin_con_contexto:
    - debe existir persistido;
    - debe pertenecer al techo del perfil;
    - valores desconocidos/inconsistentes fallan cerrado.
    """
    permitidos = permisos_permitidos_para_perfil(
        contexto.perfil
    )

    filas = (
        db.query(AccesoAdminPermiso)
        .filter(
            AccesoAdminPermiso.acceso_admin_id
            == contexto.acceso_id
        )
        .all()
    )

    resultado: set[Permission] = set()

    for fila in filas:
        permiso = _normalizar_permiso(
            fila.permiso
        )

        if (
            permiso is not None
            and permiso in permitidos
        ):
            resultado.add(permiso)

    return frozenset(resultado)


def tiene_permiso_admin(
    db: Session,
    usuario_id: object,
    permiso: Permission | str,
) -> bool:
    """
    True solo si ADMIN activo tiene:
    - configuracion valida;
    - permiso permitido por su perfil;
    - permiso persistido explicitamente.
    """
    permiso_enum = _normalizar_permiso(permiso)

    if permiso_enum is None:
        return False

    contexto = obtener_contexto_admin(
        db,
        usuario_id,
    )

    if contexto is None:
        return False

    return _tiene_permiso_admin_con_contexto(
        db,
        contexto,
        permiso_enum,
    )


def especialidad_en_alcance_admin(
    db: Session,
    usuario_id: object,
    especialidad: object,
) -> bool:
    """
    Verifica si una especialidad concreta pertenece al alcance ADMIN.

    Un alcance institucional cubre cualquier especialidad valida.
    Un alcance limitado exige coincidencia normalizada.
    """
    if not isinstance(especialidad, str):
        return False

    try:
        especialidad_objetivo = normalizar_especialidad(
            especialidad
        )
    except ValueError:
        return False

    contexto = obtener_contexto_admin(
        db,
        usuario_id,
    )

    if contexto is None:
        return False

    if (
        contexto.tipo_alcance
        is TipoAlcanceAdmin.INSTITUCIONAL
    ):
        return True

    return (
        especialidad_objetivo.normalizada
        in contexto.especialidades_normalizadas
    )


def tiene_permiso_admin_en_especialidad(
    db: Session,
    usuario_id: object,
    permiso: Permission | str,
    especialidad: object,
) -> bool:
    """
    Combinacion permiso + alcance para recursos asociados a una
    especialidad concreta.
    """
    permiso_enum = _normalizar_permiso(permiso)

    if permiso_enum is None:
        return False

    contexto = obtener_contexto_admin(
        db,
        usuario_id,
    )

    if contexto is None:
        return False

    if not _tiene_permiso_admin_con_contexto(
        db,
        contexto,
        permiso_enum,
    ):
        return False

    if not isinstance(especialidad, str):
        return False

    try:
        objetivo = normalizar_especialidad(
            especialidad
        )
    except ValueError:
        return False

    if (
        contexto.tipo_alcance
        is TipoAlcanceAdmin.INSTITUCIONAL
    ):
        return True

    return (
        objetivo.normalizada
        in contexto.especialidades_normalizadas
    )


def tiene_permiso_efectivo(
    db: Session,
    current_user: object,
    permiso: Permission | str,
) -> bool:
    """
    Resolver general preparado para las dependencias de SA-9.4/9.5.

    Usa siempre Usuario ACTUAL de BD como autoridad:
    - ADMIN -> permisos persistidos + perfil;
    - SUPERADMIN -> permisos explicitos de ROLE_DEFAULT_PERMISSIONS;
    - PROFESIONAL/ESTUDIANTE -> capacidades RBAC actuales de rol.

    El alcance de recurso sigue siendo una segunda comprobacion:
    esta funcion responde solo a la capacidad efectiva general.
    """
    permiso_enum = _normalizar_permiso(permiso)

    if permiso_enum is None or not isinstance(current_user, dict):
        return False

    usuario = _cargar_usuario_activo(
        db,
        current_user.get("id"),
    )

    if usuario is None:
        return False

    rol = normalizar_rol(usuario.rol)

    if rol is None:
        return False

    if rol is Role.ADMIN:
        contexto = _cargar_contexto_admin_desde_usuario(
            db,
            usuario,
        )

        if contexto is None:
            return False

        return _tiene_permiso_admin_con_contexto(
            db,
            contexto,
            permiso_enum,
        )

    # Para SUPERADMIN/PROFESIONAL/ESTUDIANTE se conserva la fuente
    # explicita de permisos por rol. No se confia en current_user["rol"]:
    # usamos el rol ACTUAL resuelto nuevamente desde Usuario.
    return has_permission(
        rol,
        permiso_enum,
    )
# ??????????????????????????????????????????????????????????????????
# SA-9.4 - Alcance administrativo efectivo para recursos
# ??????????????????????????????????????????????????????????????????

def obtener_acceso_administrativo_efectivo(
    db: Session,
    current_user: object,
) -> AccesoAdministrativoEfectivo | None:
    """
    Resuelve el contexto administrativo completo para SA-10.

    Usa siempre Usuario ACTUAL de BD:
    - ADMIN requiere configuración v?lida y permisos persistidos;
    - SUPERADMIN no depende de acceso_administrativo;
    - otros roles no poseen contexto administrativo.

    No introduce una segunda fuente de autorización.
    """
    if not isinstance(current_user, dict):
        return None

    usuario = _cargar_usuario_activo(
        db,
        current_user.get("id"),
    )

    if usuario is None:
        return None

    rol = normalizar_rol(usuario.rol)

    if rol is Role.SUPERADMIN:
        return AccesoAdministrativoEfectivo(
            rol=rol,
            perfil=None,
            tipo_alcance=TipoAlcanceAdmin.INSTITUCIONAL,
            especialidades=(),
            permisos=ROLE_DEFAULT_PERMISSIONS[
                Role.SUPERADMIN
            ],
        )

    if rol is not Role.ADMIN:
        return None

    contexto = _cargar_contexto_admin_desde_usuario(
        db,
        usuario,
    )

    if contexto is None:
        return None

    return AccesoAdministrativoEfectivo(
        rol=rol,
        perfil=contexto.perfil,
        tipo_alcance=contexto.tipo_alcance,
        especialidades=contexto.especialidades,
        permisos=_permisos_admin_efectivos_con_contexto(
            db,
            contexto,
        ),
    )


@dataclass(frozen=True)
class AlcanceAdministrativoEfectivo:
    """
    Alcance ya resuelto para una request administrativa.

    institucional=True:
        no restringe por especialidad.

    institucional=False:
        solo permite las especialidades normalizadas indicadas.
    """

    institucional: bool
    especialidades_normalizadas: frozenset[str]


def obtener_alcance_administrativo_efectivo(
    db: Session,
    current_user: object,
) -> AlcanceAdministrativoEfectivo | None:
    """
    Resuelve el alcance administrativo efectivo.

    SUPERADMIN:
        alcance institucional, pero sigue necesitando el permiso RBAC
        explicito exigido por el endpoint.

    ADMIN:
        requiere configuracion SA-8 valida y activa.

    Otros roles:
        no tienen alcance administrativo.
    """
    if not isinstance(current_user, dict):
        return None

    rol = normalizar_rol(current_user.get("rol"))

    if rol is Role.SUPERADMIN:
        return AlcanceAdministrativoEfectivo(
            institucional=True,
            especialidades_normalizadas=frozenset(),
        )

    if rol is not Role.ADMIN:
        return None

    contexto = obtener_contexto_admin(
        db,
        current_user.get("id"),
    )

    if contexto is None:
        return None

    return AlcanceAdministrativoEfectivo(
        institucional=(
            contexto.tipo_alcance
            is TipoAlcanceAdmin.INSTITUCIONAL
        ),
        especialidades_normalizadas=(
            contexto.especialidades_normalizadas
        ),
    )


def resolver_perfil_administrativo_snapshot(
    db: Session,
    current_user: object,
) -> str | None:
    """
    A.4.1 — snapshot INMUTABLE de AccesoAdministrativo.perfil vigente
    en el momento de una acción, para trazabilidad de origen (p. ej.
    Cita.creado_por_perfil). NO es una función de autorización: no
    concede ni verifica ningún permiso, solo describe quién creó algo.

    Reutiliza obtener_contexto_admin() en vez de reimplementar la
    resolución de perfil, así que hereda su mismo criterio
    fail-closed:
      - rol distinto de ADMIN (incluido SUPERADMIN, que no depende de
        AccesoAdministrativo, y PROFESIONAL/ESTUDIANTE) -> None;
      - ADMIN sin fila AccesoAdministrativo, o con una configuración
        inválida/inconsistente -> None;
      - nunca "adivina" ni aproxima un perfil — None es siempre la
        respuesta honesta cuando no hay un perfil real que reportar.

    No depende de que el permiso esté además persistido
    (AccesoAdminPermiso): un ADMIN con perfil válido pero sin ningún
    permiso concedido todavía tiene un perfil real que registrar como
    snapshot de trazabilidad — eso es una pregunta de identidad, no
    de autorización efectiva (que ya se resolvió antes, en el
    endpoint, con tiene_permiso_efectivo/require_effective_permission).
    """
    if not isinstance(current_user, dict):
        return None

    # A.4.1 v2 — guard EXPLÍCITO por rol antes de tocar
    # AccesoAdministrativo: aunque obtener_contexto_admin() ya
    # devuelve None para cualquier rol distinto de ADMIN (porque
    # normaliza current_user["rol"] y compara contra Role.ADMIN
    # internamente), ese comportamiento dependía de la implementación
    # interna de _cargar_contexto_admin_desde_usuario(), no de un
    # chequeo visible acá. Se hace explícito para que, incluso si en
    # el futuro esa función cambiara de criterio, o si por datos
    # legacy/stale existiera accidentalmente una fila
    # AccesoAdministrativo asociada a un usuario que YA NO es ADMIN
    # (p. ej. degradado a SUPERADMIN/ESTUDIANTE/PROFESIONAL sin borrar
    # su fila vieja), un actor no-ADMIN JAMÁS reciba un
    # creado_por_perfil.
    if current_user.get("rol") != "admin":
        return None

    contexto = obtener_contexto_admin(db, current_user.get("id"))
    if contexto is None:
        return None

    return contexto.perfil.value


def especialidad_permitida_por_alcance(
    alcance: AlcanceAdministrativoEfectivo | None,
    especialidad: object,
) -> bool:
    """
    Comprueba una especialidad concreta contra un alcance resuelto.

    El alcance institucional no restringe por especialidad.

    Para alcance limitado, un valor vacio/invalido falla cerrado.
    """
    if alcance is None:
        return False

    if alcance.institucional:
        return True

    if not isinstance(especialidad, str):
        return False

    try:
        objetivo = normalizar_especialidad(
            especialidad
        )
    except ValueError:
        return False

    return (
        objetivo.normalizada
        in alcance.especialidades_normalizadas
    )
