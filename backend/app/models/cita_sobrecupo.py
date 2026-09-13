"""
SESAES — A.4.1: persistencia normalizada de metadata de sobrecupo.

Por qué una tabla aparte (y no columnas JSON en Cita):

- No toda Cita es un sobrecupo — es una relación 1:1 OPCIONAL, no un
  atributo que toda fila necesite.
- El diagnóstico de A.4 (punto 6/10) confirmó que
  `evaluar_disponibilidad_slot()` hoy devuelve UN solo motivo ganador,
  no una lista — pero el ticket A.4 pide explícitamente no depender
  de un blob JSON en una columna de Cita para los conflictos
  superados, sino un modelo que ya soporte varios conflictos por
  sobrecupo cuando una fase futura evolucione el motor de
  disponibilidad a "lista de conflictos" (ver diagnóstico A.4). Por
  eso `CitaSobrecupoConflicto` es una tabla de detalle (N filas por
  CitaSobrecupo), no una columna.

A.4.1 explícitamente NO:
  - decide qué motivos son sobrecupables (eso ya vive en
    agenda_disponibilidad_service.py, sin cambios);
  - implementa aprobación/rechazo del profesional (fase futura de
    revisión profesional);
  - hace que el motor produzca más de un conflicto por evaluación
    (fase futura de análisis estructurado de conflictos) — hoy cada
    CitaSobrecupo tendrá en la práctica exactamente una fila en
    CitaSobrecupoConflicto, con el único motivo que
    `evaluar_disponibilidad_slot()` puede devolver como
    `overridable_con_sobrecupo=True` (`en_colacion` o
    `fuera_de_jornada`).
"""

from sqlalchemy import Column, Integer, String, ForeignKey, DateTime
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from app.database import Base


class CitaSobrecupo(Base):
    """
    Metadata de UN sobrecupo concreto. Existe si y solo si la Cita
    asociada fue creada con `sobrecupo=True` Y ese sobrecupo fue
    efectivamente autorizado por `evaluar_disponibilidad_slot()`
    (nunca se crea una fila acá solo porque el cliente envió
    `sobrecupo=True` sobre un slot que en realidad ya estaba libre —
    ver citas.py: eso no es un conflicto real que "superar", y esta
    tabla no debe registrar un conflicto inventado).
    """

    __tablename__ = "cita_sobrecupo"

    id = Column(Integer, primary_key=True, index=True)

    # UNIQUE: fuerza la relación 1:1 con Cita a nivel de esquema, no
    # solo por convención de la capa ORM.
    cita_id = Column(
        Integer,
        ForeignKey("cita.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    # Motivo HUMANO (texto libre), distinto del código estructurado
    # (ver CitaSobrecupoConflicto.codigo). Deliberadamente NO se
    # reutiliza Cita.observaciones: ese campo es del dominio clínico
    # ("motivo de consulta"), no de "por qué se fuerza este horario".
    #
    # Nullable por compatibilidad temporal: el frontend actual (ver
    # dashboard-admin.ts) todavía NO envía `sobrecupo_motivo` en el
    # body de POST /citas — exigirlo ahora rompería silenciosamente
    # cada sobrecupo creado desde la UI existente. La fase futura de
    # frontend debe: (a) agregar el campo al modal de confirmación de
    # sobrecupo en Angular, y (b) solo entonces evaluar si corresponde
    # volver este campo NOT NULL para sobrecupos nuevos (nunca para
    # los históricos ya creados sin motivo).
    motivo = Column(String, nullable=True)

    # A.4.1 v3 — a diferencia de Cita.fecha_creacion (nullable=True
    # porque existen citas HISTÓRICAS anteriores a A.4.1 cuya fecha
    # real de creación se desconoce), CitaSobrecupo es una tabla
    # COMPLETAMENTE NUEVA: no existe, ni puede existir, ningún
    # CitaSobrecupo anterior a esta migración. Todo sobrecupo que el
    # nuevo sistema registre nace con fecha de creación real conocida
    # (server_default=func.now()), así que aquí sí corresponde
    # NOT NULL — no hay ningún caso histórico que compatibilizar.
    fecha_creacion = Column(DateTime, server_default=func.now(), nullable=False)

    # Preparado para una fase futura de aprobación/rechazo del
    # profesional, que A.4.1 NO implementa. Se deja en NULL (no en un
    # string como "no_aplica") porque NULL representa honestamente
    # "todavía no existe ninguna función que lea esta columna" — usar
    # "no_aplica" implicaría ya haber decidido una política de negocio
    # (p. ej. "este tipo de sobrecupo nunca requiere revisión") que
    # A.4.1 no está tomando. Los valores reales que esa fase futura
    # definirá aquí (p. ej. "pendiente_profesional" /
    # "aceptado_profesional" / "rechazado_profesional") no se
    # anticipan como constantes.
    estado_revision = Column(String, nullable=True)

    cita = relationship("Cita", back_populates="sobrecupo_detalle")

    conflictos = relationship(
        "CitaSobrecupoConflicto",
        back_populates="sobrecupo",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class CitaSobrecupoConflicto(Base):
    """
    Un motivo estructurado (código de `evaluar_disponibilidad_slot`)
    que este sobrecupo superó. Modelada como N filas por
    CitaSobrecupo desde A.4.1 aunque hoy, con el motor actual de
    disponibilidad (un solo motivo ganador — ver diagnóstico A.4),
    cada sobrecupo solo pueda producir exactamente una fila acá: así
    la fase futura de análisis estructurado de conflictos no necesita
    ninguna migración de esquema, solo empezar a insertar más de una
    fila por sobrecupo.
    """

    __tablename__ = "cita_sobrecupo_conflicto"

    id = Column(Integer, primary_key=True, index=True)

    cita_sobrecupo_id = Column(
        Integer,
        ForeignKey("cita_sobrecupo.id", ondelete="CASCADE"),
        nullable=False,
        index=True,  # A.4.1 v2 — PostgreSQL no indexa FKs automáticamente
                     # y esta relación se consulta constantemente
                     # (cargar todos los conflictos de un sobrecupo).
    )

    # Código tal como lo devuelve evaluar_disponibilidad_slot() en
    # ResultadoDisponibilidad.motivo. A.4.1 solo puede llegar a
    # persistir "en_colacion" o "fuera_de_jornada" -- los únicos dos
    # motivos con overridable_con_sobrecupo=True hoy -- pero se guarda
    # como texto libre (no Enum de DB) para que las fases futuras de
    # análisis de conflictos / unificación con urgencias no requieran
    # una migración de esquema al agregar códigos nuevos.
    codigo = Column(String, nullable=False)

    sobrecupo = relationship("CitaSobrecupo", back_populates="conflictos")
