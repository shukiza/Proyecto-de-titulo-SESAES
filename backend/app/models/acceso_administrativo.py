"""
SESAES - SA-8: persistencia de perfil y alcance administrativo.

Estas tablas no conceden permisos por si solas. SA-9 combinara los permisos
RBAC con esta configuracion para resolver autorizacion efectiva.
"""

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.database import Base


class AccesoAdministrativo(Base):
    __tablename__ = "acceso_administrativo"

    __table_args__ = (
        UniqueConstraint(
            "usuario_id",
            name="uq_acceso_administrativo_usuario",
        ),
        CheckConstraint(
            "perfil IN ("
            "'administrador_general', "
            "'administrador_especialidad', "
            "'secretaria_general', "
            "'secretaria_especialidad'"
            ")",
            name="ck_acceso_administrativo_perfil",
        ),
        CheckConstraint(
            "tipo_alcance IN ('institucional', 'especialidades')",
            name="ck_acceso_administrativo_tipo_alcance",
        ),
        CheckConstraint(
            "("
            "perfil IN ('administrador_general', 'secretaria_general') "
            "AND tipo_alcance = 'institucional'"
            ") OR ("
            "perfil IN ('administrador_especialidad', 'secretaria_especialidad') "
            "AND tipo_alcance = 'especialidades'"
            ")",
            name="ck_acceso_administrativo_perfil_alcance",
        ),
    )

    id = Column(Integer, primary_key=True)
    usuario_id = Column(
        Integer,
        ForeignKey("usuario.id", ondelete="RESTRICT"),
        nullable=False,
    )
    perfil = Column(String(64), nullable=False)
    tipo_alcance = Column(String(32), nullable=False)

    usuario = relationship("Usuario")

    especialidades = relationship(
        "AccesoAdminEspecialidad",
        back_populates="acceso",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    permisos = relationship(
        "AccesoAdminPermiso",
        back_populates="acceso",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class AccesoAdminEspecialidad(Base):
    __tablename__ = "acceso_admin_especialidad"

    __table_args__ = (
        UniqueConstraint(
            "acceso_admin_id",
            "especialidad_normalizada",
            name="uq_acceso_admin_especialidad_normalizada",
        ),
        CheckConstraint(
            "length(trim(especialidad)) > 0",
            name="ck_acceso_admin_especialidad_nombre_no_vacio",
        ),
        CheckConstraint(
            "length(trim(especialidad_normalizada)) > 0",
            name="ck_acceso_admin_especialidad_normalizada_no_vacia",
        ),
    )

    id = Column(Integer, primary_key=True)

    acceso_admin_id = Column(
        Integer,
        ForeignKey(
            "acceso_administrativo.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    especialidad = Column(String(160), nullable=False)
    especialidad_normalizada = Column(String(160), nullable=False)

    acceso = relationship(
        "AccesoAdministrativo",
        back_populates="especialidades",
    )
class AccesoAdminPermiso(Base):
    """
    Permiso administrativo asignado explicitamente a una cuenta ADMIN.

    La tabla solo admite permisos administrativos delegables. La
    compatibilidad concreta perfil -> permiso se valida en la capa de
    dominio de SA-9; esta restriccion de BD impide persistir permisos
    reservados, clinicos o de autoservicio.
    """

    __tablename__ = "acceso_admin_permiso"

    __table_args__ = (
        UniqueConstraint(
            "acceso_admin_id",
            "permiso",
            name="uq_acceso_admin_permiso",
        ),
        CheckConstraint(
            "permiso IN ("
            "'usuarios.ver', "
            "'usuarios.gestionar', "
            "'profesionales.ver', "
            "'profesionales.gestionar', "
            "'agenda.ver', "
            "'agenda.gestionar', "
            # A.4.3 — agenda.sobrecupo: capacidad DISTINTA de
            # agenda.gestionar (ver app.rbac.permissions.Permission y
            # app.services.sobrecupo_policy_service). En una base ya
            # desplegada antes de A.4.3, este valor solo queda
            # aceptado por el motor tras correr
            # scripts/migrar_a4_3_permiso_sobrecupo.py — este
            # CheckConstraint es la fuente de verdad para esquemas
            # NUEVOS (tests, instalaciones frescas); no reescribe
            # ninguna base ya existente por sí solo.
            "'agenda.sobrecupo', "
            "'reportes.ver'"
            ")",
            name="ck_acceso_admin_permiso_valido",
        ),
    )

    id = Column(Integer, primary_key=True)

    acceso_admin_id = Column(
        Integer,
        ForeignKey(
            "acceso_administrativo.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    permiso = Column(String(96), nullable=False)

    acceso = relationship(
        "AccesoAdministrativo",
        back_populates="permisos",
    )
