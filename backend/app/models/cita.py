from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, DateTime
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from app.database import Base

class Cita(Base):
    __tablename__ = "cita"

    id                    = Column(Integer, primary_key=True, index=True)
    estudiante_id         = Column(Integer, ForeignKey("usuario.id"))
    profesional_id        = Column(Integer, ForeignKey("profesional.id"))
    fecha                 = Column(String)
    hora                  = Column(String)
    estado                = Column(String, default="pendiente")
    observaciones         = Column(String, nullable=True)
    urgente               = Column(Boolean, default=False)
    cancelada_por_admin   = Column(Boolean, default=False)
    motivo_cancelacion    = Column(String, nullable=True)
    medicamento           = Column(String, nullable=True)
    observaciones_atencion = Column(String, nullable=True)

    # Cita creada por el admin fuera del horario habitual del profesional
    # (fuera de horario declarado, o incluso en su hora de colación),
    # forzada manualmente como excepción. Se usa para diferenciarla
    # visualmente y en reportes de las citas normales.
    sobrecupo             = Column(Boolean, default=False)

    # ── A.4.1 — trazabilidad de origen ──
    #
    # Quién creó ESTA fila, sin importar si es una cita normal, urgente
    # o sobrecupo — aplica a POST /citas y POST /admin/citas/urgente por
    # igual (ver app.services.cita_origen_service.resolver_origen_cita,
    # única fuente que arma estos tres valores). Los tres son nullable
    # por compatibilidad histórica: las citas creadas antes de A.4.1 no
    # tienen esta información y NUNCA se le inventa un valor retroactivo
    # (ver migración backend/scripts/migrar_a4_1_trazabilidad.py).
    #
    # creado_por_usuario_id: SIEMPRE el actor autenticado
    # (current_user["id"]) — NO necesariamente igual a estudiante_id;
    # un ADMIN/SUPERADMIN agendando a nombre de un estudiante deja
    # estudiante_id = paciente, creado_por_usuario_id = el propio
    # admin/superadmin. Indexada (A.4.1 v2): reportes/auditoría
    # probablemente van a filtrar "todas las citas creadas por X".
    creado_por_usuario_id = Column(
        Integer, ForeignKey("usuario.id"), nullable=True, index=True,
    )
    # creado_por_rol: snapshot del rol BASE del actor en el momento de
    # crear la cita (current_user["rol"], ya resuelto contra BD actual
    # por get_current_user — nunca el rol crudo de un JWT viejo).
    creado_por_rol        = Column(String, nullable=True)
    # creado_por_perfil: snapshot de AccesoAdministrativo.perfil del
    # actor en ese momento, SOLO cuando aplica (actor con rol ADMIN y
    # configuración administrativa válida). NULL para
    # estudiante/profesional/SUPERADMIN, y también NULL si la
    # configuración ADMIN es inválida/inconsistente (fail-closed, mismo
    # criterio que el resto de app.rbac.admin_authorization). Nunca
    # introduce un rol/perfil "secretaria" nuevo — sigue siendo un
    # valor de AccesoAdministrativo.perfil ya existente
    # (secretaria_general / secretaria_especialidad).
    creado_por_perfil     = Column(String, nullable=True)
    # fecha_creacion: momento real de creación para filas NUEVAS
    # (DEFAULT a nivel de motor, aplicado por SQLAlchemy vía
    # server_default tanto en create_all() de test como, en
    # producción, por el ALTER ... SET DEFAULT explícito de la
    # migración — ver su docstring). Nullable=True porque las filas
    # históricas (anteriores a A.4.1) no tienen este dato real y la
    # migración las deja en NULL a propósito, en vez de asignarles la
    # fecha de la migración como si fuera su fecha de creación real.
    fecha_creacion        = Column(DateTime, server_default=func.now(), nullable=True)

    profesional = relationship("Profesional", back_populates="citas")
    estudiante  = relationship("Usuario", foreign_keys=[estudiante_id])
    creado_por_usuario = relationship("Usuario", foreign_keys=[creado_por_usuario_id])

    # 1:1 OPCIONAL — solo existe si esta Cita fue creada con
    # sobrecupo=True Y el sobrecupo fue efectivamente autorizado (ver
    # citas.py). Ver app.models.cita_sobrecupo para el porqué de una
    # tabla aparte en vez de columnas/JSON en Cita.
    sobrecupo_detalle = relationship(
        "CitaSobrecupo",
        back_populates="cita",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )