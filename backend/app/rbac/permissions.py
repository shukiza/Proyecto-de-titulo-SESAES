"""
SESAES — RBAC: catálogo de permisos y resolución (Fase 3.1)

Catálogo de 20 permisos agrupados por dominio real del sistema
(administración, clínico/profesional, estudiante/autoservicio), y la
tabla ROLE_DEFAULT_PERMISSIONS que define, para cada Role, el conjunto
de permisos que tiene por defecto.

Principio de mínimo privilegio — en particular para SUPERADMIN:

    SUPERADMIN NO implica acceso total. No existe ningún atajo del
    tipo `if role == Role.SUPERADMIN: return True`, ni wildcard
    ("*", "ALL", "FULL_ACCESS"). SUPERADMIN solo recibe permisos
    administrativos EXPLÍCITOS listados abajo; no recibe ficha
    clínica, anamnesis, evoluciones ni ningún otro permiso clínico
    por defecto.

Esta fase NO persiste permisos en base de datos (no hay tabla
permissions/role_permissions/user_permissions): ROLE_DEFAULT_PERMISSIONS
es la única fuente de verdad, definida en código.

Nota — autorización de recurso (no implementada todavía):

    Los permisos con sufijo "_asignada"/"_asignadas" (FICHA_VER_ASIGNADA,
    FICHA_EDITAR_ASIGNADA, ATENCIONES_VER_ASIGNADAS) representan
    únicamente una CAPACIDAD RBAC: que el rol, en general, puede
    realizar esa acción sobre fichas/atenciones que le correspondan.
    has_permission() NO verifica que un paciente/ficha/atención
    concreto esté dentro del alcance real asignado a ese profesional
    (eso hoy lo resuelve verificar_acceso_profesional en
    auth_dependencies.py para los endpoints ya migrados). En Fase 3.2,
    cuando los endpoints clínicos se migren a require_permission,
    deberán seguir verificando además esa pertenencia concreta del
    recurso — RBAC por sí solo no reemplaza esa comprobación.
"""

from __future__ import annotations

import enum
from typing import Dict, FrozenSet, Optional, Union

from app.rbac.roles import Role, normalizar_rol


class Permission(str, enum.Enum):
    """Catálogo de permisos de SESAES (20 permisos)."""

    # ── Administración ──────────────────────────────────────────
    USUARIOS_VER = "usuarios.ver"
    USUARIOS_GESTIONAR = "usuarios.gestionar"
    PROFESIONALES_VER = "profesionales.ver"
    PROFESIONALES_GESTIONAR = "profesionales.gestionar"
    AGENDA_VER = "agenda.ver"
    AGENDA_GESTIONAR = "agenda.gestionar"
    # A.4.3 — capacidad DISTINTA de AGENDA_GESTIONAR a propósito: poder
    # gestionar una agenda (crear/mover/cancelar citas normales) no
    # implica automáticamente poder forzar un sobrecupo sobre un
    # conflicto real. Ver app.services.sobrecupo_policy_service.
    AGENDA_SOBRECUPO = "agenda.sobrecupo"
    CONFIGURACION_GESTIONAR = "configuracion.gestionar"
    REPORTES_VER = "reportes.ver"
    REPORTES_CGR_EXPORTAR = "reportes.cgr.exportar"
    AUDITORIA_VER = "auditoria.ver"
    ROLES_GESTIONAR = "roles.gestionar"

    # ── Clínico / Profesional ───────────────────────────────────
    ATENCIONES_VER_ASIGNADAS = "atenciones.ver_asignadas"
    ATENCIONES_REGISTRAR = "atenciones.registrar"
    FICHA_VER_ASIGNADA = "ficha.ver_asignada"
    FICHA_EDITAR_ASIGNADA = "ficha.editar_asignada"
    AGENDA_VER_PROFESIONAL = "agenda.ver_profesional"
    AGENDA_GESTIONAR_PROPIA = "agenda.gestionar_propia"

    # ── Estudiante / Autoservicio ────────────────────────────────
    CITAS_GESTIONAR_PROPIAS = "citas.gestionar_propias"
    PERFIL_VER_PROPIO = "perfil.ver_propio"
    DOCUMENTOS_VER_PROPIOS = "documentos.ver_propios"


# ══════════════════════════════════════════════════════════════════
# ROLE_DEFAULT_PERMISSIONS — fuente central única
# ══════════════════════════════════════════════════════════════════
# Mínimo privilegio: cada rol solo recibe lo estrictamente necesario
# para sus dominios reales. Sin wildcard, sin fallback implícito.

ROLE_DEFAULT_PERMISSIONS: Dict[Role, FrozenSet[Permission]] = {
    Role.SUPERADMIN: frozenset(
        {
            Permission.USUARIOS_VER,
            Permission.USUARIOS_GESTIONAR,
            Permission.PROFESIONALES_VER,
            Permission.PROFESIONALES_GESTIONAR,
            Permission.AGENDA_VER,
            Permission.AGENDA_GESTIONAR,
            # A.4.3 — explícito, igual que el resto de esta lista: NO es
            # un atajo "SUPERADMIN puede forzar sobrecupo por ser
            # superadmin". La política de sobrecupo SIEMPRE resuelve
            # esto vía tiene_permiso_efectivo(), que para SUPERADMIN cae
            # en has_permission() contra esta misma tabla explícita — el
            # mismo mecanismo que ya rige AGENDA_GESTIONAR arriba.
            Permission.AGENDA_SOBRECUPO,
            Permission.CONFIGURACION_GESTIONAR,
            Permission.REPORTES_VER,
            Permission.REPORTES_CGR_EXPORTAR,
            Permission.AUDITORIA_VER,
            Permission.ROLES_GESTIONAR,
            # SA-5: AUDITORIA_GESTIONAR fue retirado del catálogo. La
            # auditoría es append-only a nivel de aplicación — no existe
            # ningún permiso de "gestión"/borrado de auditoría, ni para
            # SUPERADMIN. Ver política SA-5.
            # Deliberadamente SIN permisos clínicos (ficha.*, atenciones.*):
            # ver principio de mínimo privilegio en el docstring del módulo.
        }
    ),
    Role.ADMIN: frozenset(),
    Role.PROFESIONAL: frozenset(
        {
            Permission.ATENCIONES_VER_ASIGNADAS,
            Permission.ATENCIONES_REGISTRAR,
            Permission.FICHA_VER_ASIGNADA,
            Permission.FICHA_EDITAR_ASIGNADA,
            Permission.AGENDA_VER_PROFESIONAL,
            Permission.AGENDA_GESTIONAR_PROPIA,
        }
    ),
    Role.ESTUDIANTE: frozenset(
        {
            Permission.CITAS_GESTIONAR_PROPIAS,
            Permission.PERFIL_VER_PROPIO,
            Permission.DOCUMENTOS_VER_PROPIOS,
        }
    ),
}


def has_permission(
    user_or_role: Union[str, Role, dict, None],
    permission: Permission,
) -> bool:
    """
    Resuelve si un usuario/rol tiene un permiso dado.

    Acepta:
      - un Role,
      - un string de rol crudo (ej. current_user["rol"] del JWT),
      - un dict tipo current_user (con clave "rol"),
      - None.

    Fail-closed en todos los casos: rol inválido, desconocido, null o
    vacío → False. Nunca hay atajo de Superadmin ni permiso concedido
    por defecto para un permiso no listado explícitamente en
    ROLE_DEFAULT_PERMISSIONS.
    """
    rol_crudo: Optional[Union[str, Role]]

    if isinstance(user_or_role, dict):
        rol_crudo = user_or_role.get("rol")
    else:
        rol_crudo = user_or_role

    rol = normalizar_rol(rol_crudo)
    if rol is None:
        return False

    permisos_del_rol = ROLE_DEFAULT_PERMISSIONS.get(rol, frozenset())
    return permission in permisos_del_rol
