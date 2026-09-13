# -*- coding: utf-8 -*-
"""
SESAES — A.4.1: origen (trazabilidad de creador) de una Cita.

Centraliza cómo se resuelven `creado_por_usuario_id` / `creado_por_rol`
/ `creado_por_perfil` para CUALQUIER canal que cree una Cita —
POST /citas y POST /admin/citas/urgente— para que ambos usen
exactamente el mismo criterio. El diagnóstico de A.4 ya encontró una
duplicación real entre esos dos routers para la regla de DiaCerrado
(mismo chequeo reimplementado dos veces); esta pieza evita repetir ese
patrón para el origen de la cita.

Contrato de seguridad:
  - creado_por_usuario_id / creado_por_rol se derivan EXCLUSIVAMENTE de
    `current_user`, que en ambos routers llega ya resuelto por
    get_current_user() contra el estado ACTUAL de Usuario en BD (nunca
    el payload crudo de un JWT viejo). Nunca se acepta un valor
    "creado_por_*" que venga del body del request.
  - creado_por_perfil se resuelve con la infraestructura ya existente
    de AccesoAdministrativo (resolver_perfil_administrativo_snapshot),
    no con una lógica nueva. Es None para cualquier actor que no sea
    un ADMIN con configuración administrativa válida (incluido
    SUPERADMIN, que no depende de AccesoAdministrativo).
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.rbac.admin_authorization import resolver_perfil_administrativo_snapshot


@dataclass(frozen=True)
class OrigenCita:
    creado_por_usuario_id: int
    creado_por_rol: str
    creado_por_perfil: str | None


def resolver_origen_cita(db: Session, current_user: dict) -> OrigenCita:
    """
    Arma el snapshot de origen para una Cita que se está creando
    AHORA, a partir del actor autenticado de la request actual.

    Lanza KeyError si `current_user` no trae "id"/"rol" — igual que el
    resto del código de routers, se asume que current_user ya pasó por
    get_current_user() y siempre trae ambas claves; no se valida de
    nuevo acá para no duplicar esa responsabilidad.
    """
    return OrigenCita(
        creado_por_usuario_id=current_user["id"],
        creado_por_rol=current_user["rol"],
        creado_por_perfil=resolver_perfil_administrativo_snapshot(
            db, current_user,
        ),
    )
