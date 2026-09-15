# -*- coding: utf-8 -*-
"""
SESAES — A.4.2 (v2): analizador estructurado de conflictos de agenda.

v2 corrige tres puntos de diseño de v1, revisados en el PR:

  1. UNA SOLA FUENTE DE VERDAD: `analizar_conflictos_slot()` es ahora
     la ÚNICA implementación de las reglas de un slot con contexto
     válido. `_evaluar_slot_en_contexto()` pasó a ser un wrapper
     delgado que llama al analizador y reduce su resultado
     (`_legacy_desde_conflictos()`) — ya no reimplementa ninguna
     condición por su cuenta. `evaluar_disponibilidad_slot()` llama al
     analizador UNA sola vez y reduce esa misma tupla, sin una segunda
     llamada independiente.
  2. RELOJ DETERMINISTA: `analizar_conflictos_slot()` ya no lee
     `datetime.now()` internamente — el chequeo `hora_pasada` recibe
     la hora actual inyectada (`ahora: time | None`).
     `evaluar_disponibilidad_slot()` y `listar_disponibilidad_rango()`
     capturan el reloj UNA sola vez y reutilizan ese mismo snapshot.
  3. La equivalencia `overridable_con_sobrecupo` <=>
     `not hay_bloqueo_absoluto(conflictos)` se documenta únicamente
     para resultados con contexto analizable — NO como una
     equivalencia universal (los motivos terminales pueden traer
     `conflictos=()` con `overridable_con_sobrecupo=False`).

Cubre, en app.services.agenda_disponibilidad_service:

  - `ConflictoSlot` / metadata JSON-serializable / sin IDs de citas
    ocupantes.
  - `analizar_conflictos_slot()` como única fuente: cada motivo
    individual, categoría, overridable, intervalos semiabiertos,
    adjacency, combinaciones simultáneas obligatorias, orden
    determinista.
  - `hay_bloqueo_absoluto()`.
  - `_evaluar_slot_en_contexto()` como wrapper delgado: mismo motivo,
    mismo mensaje EXACTO, mismo overridable que antes de A.4.2.
  - El reloj determinista: sin `ahora`, `hora_pasada` no se evalúa (no
    lee el reloj global); con `ahora`, es consistente entre el
    análisis estructurado y la reducción legacy porque es la MISMA
    llamada.
  - `ResultadoDisponibilidad.conflictos` (default `()`), vacío en los
    casos terminales (profesional_no_encontrado/inactivo, fecha/hora
    inválida).
  - `resultado.motivo == conflictos[0].codigo` cuando hay contexto
    analizable y conflictos no está vacío.
  - `listar_disponibilidad_rango()` sin cambio de contrato.

Y en app.routers.citas:

  - POST /citas persiste UNA fila de CitaSobrecupoConflicto POR CADA
    conflicto estructurado realmente superado.
  - Un conflicto absoluto presente sigue rechazando el sobrecupo con
    400, pese a sobrecupo=True y capacidad administrativa
    (`puede_gestionar_agenda AND cita.sobrecupo AND
    resultado.overridable_con_sobrecupo AND NOT
    hay_bloqueo_absoluto(resultado.conflictos)` — sin debilitar).
  - slot_ocupado (A.3) sigue siendo 409.
  - La auditoría de sobrecupo de A.4.1 sigue intacta: el detalle sigue
    diciendo "Conflicto superado: <motivo ganador legacy>" (singular,
    sin cambios respecto a A.4.1) incluso cuando se persisten varias
    filas de CitaSobrecupoConflicto.
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Registrar modelos/relaciones antes de create_all — mismo patrón que
# el resto de la suite (ver test_a3_concurrencia.py / test_a4_1_trazabilidad.py).
import app.models.init  # noqa: F401
import app.models.solicitud_horario  # noqa: F401

from app.database import Base
from app.models.auditoria import Auditoria
from app.models.cita import Cita
from app.models.cita_sobrecupo import CitaSobrecupo, CitaSobrecupoConflicto
from app.models.dia_cerrado import DiaCerrado
from app.models.profesional import Profesional
from app.models.usuario import Usuario
from app.rbac.admin_authorization import AlcanceAdministrativoEfectivo
from app.routers import citas
from app.schemas import CitaCreate
import app.services.agenda_disponibilidad_service as agenda_disponibilidad_service
from app.services.agenda_disponibilidad_service import (
    ConflictoSlot,
    analizar_conflictos_slot,
    evaluar_disponibilidad_slot,
    hay_bloqueo_absoluto,
    _evaluar_slot_en_contexto,
)

PRECEDENCIA_LEGACY_MOTIVOS = [
    "profesional_no_encontrado",
    "profesional_inactivo",
    "fecha_invalida",
    "hora_invalida",
    "fecha_pasada",
    "fin_de_semana",
    "dia_cerrado",
    "hora_pasada",
    "hora_fuera_de_grilla",
    "excede_cierre_centro",
    "slot_ocupado",
    "fuera_de_jornada",
    "en_colacion",
]


# ══════════════════════════════════════════════════════════
# Fixtures / helpers (mismo estilo que el resto de la suite)
# ══════════════════════════════════════════════════════════

@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _dia_habil_futuro(dias_calendario: int = 1) -> str:
    fecha = date.today() + timedelta(days=dias_calendario)
    while fecha.weekday() >= 5:
        fecha += timedelta(days=1)
    return fecha.isoformat()


def _dia_habil_futuro_obj(dias_calendario: int = 1) -> date:
    return datetime.strptime(_dia_habil_futuro(dias_calendario), "%Y-%m-%d").date()


def _dia_habil_pasado_obj(dias_calendario: int = 1) -> date:
    """Día hábil (lunes a viernes) estrictamente en el pasado, para
    aislar `fecha_pasada` sin arriesgar que además caiga en fin de
    semana."""
    fecha = date.today() - timedelta(days=dias_calendario)
    while fecha.weekday() >= 5:
        fecha -= timedelta(days=1)
    return fecha


def _proximo_sabado_estricto() -> date:
    """Próximo sábado, estrictamente después de hoy — para que
    `fecha_pasada` nunca pueda colarse junto a `fin_de_semana`."""
    fecha = date.today() + timedelta(days=1)
    while fecha.weekday() != 5:
        fecha += timedelta(days=1)
    return fecha


def _profesional(
    db,
    *,
    especialidad="Nutrición",
    estado="activo",
    horario_inicio="09:00",
    horario_fin="17:00",
    hora_almuerzo_inicio=None,
    hora_almuerzo_fin=None,
    duracion_min=30,
):
    prof = Profesional(
        nombre="Profesional Test A4.2",
        especialidad=especialidad,
        iniciales="P42",
        estado=estado,
        horario_inicio=horario_inicio,
        horario_fin=horario_fin,
        hora_almuerzo_inicio=hora_almuerzo_inicio,
        hora_almuerzo_fin=hora_almuerzo_fin,
        duracion_min=duracion_min,
    )
    db.add(prof)
    db.flush()
    return prof


def _usuario(db, *, correo, rol, rut="a42-rut"):
    usuario = Usuario(
        correo=correo, password="hash-a42", rol=rol,
        nombre="Usuario Test A4.2", rut=rut, activo=True,
    )
    db.add(usuario)
    db.flush()
    return usuario


def _current_user(usuario):
    return {"id": usuario.id, "rol": usuario.rol}


def _monkeypatch_admin_institucional(monkeypatch, modulo):
    if hasattr(modulo, "tiene_permiso_efectivo"):
        monkeypatch.setattr(modulo, "tiene_permiso_efectivo", lambda db, u, p: True)
    monkeypatch.setattr(
        modulo,
        "obtener_alcance_administrativo_efectivo",
        lambda db, u: AlcanceAdministrativoEfectivo(
            institucional=True, especialidades_normalizadas=frozenset(),
        ),
    )


def _codigos(conflictos):
    return [c.codigo for c in conflictos]


def _legacy(prof, fecha_obj, hora_obj, hoy, dia_cerrado=None, ocupadas=None, ahora=None):
    """Atajo al mismo wrapper delgado que usa evaluar_disponibilidad_slot()
    (_evaluar_slot_en_contexto — ahora una reducción de
    analizar_conflictos_slot(), no una segunda implementación) para
    comparar el motivo ganador legacy contra la lista estructurada."""
    return _evaluar_slot_en_contexto(
        profesional=prof, fecha_obj=fecha_obj, hora_obj=hora_obj, hoy=hoy,
        dia_cerrado=dia_cerrado, ocupadas=ocupadas or set(), ahora=ahora,
    )


# ══════════════════════════════════════════════════════════
# 1) Cada motivo individual, aislado: código, categoría,
#    overridable, metadata JSON-serializable
# ══════════════════════════════════════════════════════════

def test_fecha_pasada_aislado(db_session):
    prof = _profesional(db_session)
    db_session.commit()
    fecha = _dia_habil_pasado_obj()

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha, hora_obj=time(10, 0),
        hoy=date.today(), dia_cerrado=None, ocupadas=set(),
    )

    assert _codigos(conflictos) == ["fecha_pasada"]
    assert conflictos[0].categoria == "precondicion"
    assert conflictos[0].overridable_con_sobrecupo is False
    json.dumps(conflictos[0].metadata)


def test_fin_de_semana_aislado(db_session):
    prof = _profesional(db_session)
    db_session.commit()
    fecha = _proximo_sabado_estricto()

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha, hora_obj=time(10, 0),
        hoy=date.today(), dia_cerrado=None, ocupadas=set(),
    )

    assert _codigos(conflictos) == ["fin_de_semana"]
    assert conflictos[0].categoria == "calendario_centro"
    assert conflictos[0].overridable_con_sobrecupo is False
    json.dumps(conflictos[0].metadata)


def test_dia_cerrado_aislado(db_session):
    prof = _profesional(db_session)
    fecha = _dia_habil_futuro_obj()
    dia_cerrado = DiaCerrado(fecha=fecha.isoformat(), motivo="Feriado de prueba A4.2")
    db_session.commit()

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha, hora_obj=time(10, 0),
        hoy=date.today(), dia_cerrado=dia_cerrado, ocupadas=set(),
    )

    assert _codigos(conflictos) == ["dia_cerrado"]
    assert conflictos[0].categoria == "calendario_centro"
    assert conflictos[0].overridable_con_sobrecupo is False
    json.dumps(conflictos[0].metadata)


def test_hora_pasada_aislado_con_ahora_inyectado(db_session):
    """v2: ya no se monkeypatchea el reloj global — `ahora` se inyecta
    directamente, la función nunca lo lee internamente."""
    prof = _profesional(db_session, horario_inicio="00:00", horario_fin="23:59")
    db_session.commit()
    hoy = date.today()

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=hoy, hora_obj=time(9, 30),
        hoy=hoy, dia_cerrado=None, ocupadas=set(), ahora=time(10, 0),
    )

    assert _codigos(conflictos) == ["hora_pasada"]
    assert conflictos[0].categoria == "precondicion"
    assert conflictos[0].overridable_con_sobrecupo is False
    json.dumps(conflictos[0].metadata)


def test_hora_pasada_sin_ahora_no_se_evalua(db_session):
    """v2, punto 2: analizar_conflictos_slot() nunca consulta el reloj
    global por su cuenta. Si `ahora` no se entrega, hora_pasada
    simplemente no se evalúa — no se sustituye por una lectura
    interna."""
    prof = _profesional(db_session, horario_inicio="00:00", horario_fin="23:59")
    db_session.commit()
    hoy = date.today()

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=hoy, hora_obj=time(0, 0),
        hoy=hoy, dia_cerrado=None, ocupadas=set(),
    )

    assert "hora_pasada" not in _codigos(conflictos)


def test_hora_fuera_de_grilla_aislado(db_session):
    prof = _profesional(db_session, horario_inicio="08:00", horario_fin="17:00", duracion_min=30)
    fecha = _dia_habil_futuro_obj()
    db_session.commit()

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha, hora_obj=time(8, 7),
        hoy=date.today(), dia_cerrado=None, ocupadas=set(),
    )

    assert _codigos(conflictos) == ["hora_fuera_de_grilla"]
    assert conflictos[0].categoria == "grilla_centro"
    assert conflictos[0].overridable_con_sobrecupo is False
    json.dumps(conflictos[0].metadata)


def test_excede_cierre_centro_aislado(db_session):
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="20:00", duracion_min=90)
    fecha = _dia_habil_futuro_obj()
    db_session.commit()

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha, hora_obj=time(17, 0),
        hoy=date.today(), dia_cerrado=None, ocupadas=set(),
    )

    assert _codigos(conflictos) == ["excede_cierre_centro"]
    assert conflictos[0].categoria == "grilla_centro"
    assert conflictos[0].overridable_con_sobrecupo is False
    metadata = conflictos[0].metadata
    assert set(metadata.keys()) == {
        "inicio_solicitado", "fin_solicitado", "duracion_min", "hora_fin_centro",
    }
    assert metadata == {
        "inicio_solicitado": "17:00", "fin_solicitado": "18:30",
        "duracion_min": 90, "hora_fin_centro": "18:00",
    }
    json.dumps(metadata)


def test_slot_ocupado_aislado(db_session):
    prof = _profesional(db_session, duracion_min=30)
    fecha = _dia_habil_futuro_obj()
    db_session.commit()

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha, hora_obj=time(9, 30),
        hoy=date.today(), dia_cerrado=None, ocupadas={time(9, 30)},
    )

    assert _codigos(conflictos) == ["slot_ocupado"]
    assert conflictos[0].categoria == "ocupacion"
    assert conflictos[0].overridable_con_sobrecupo is False
    metadata = conflictos[0].metadata
    assert set(metadata.keys()) == {"inicio_solicitado", "fin_solicitado", "duracion_min"}
    json.dumps(metadata)


def test_slot_ocupado_no_incluye_ids_de_citas_ocupantes(db_session):
    prof = _profesional(db_session, duracion_min=30)
    fecha = _dia_habil_futuro_obj()
    db_session.commit()

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha, hora_obj=time(9, 30),
        hoy=date.today(), dia_cerrado=None, ocupadas={time(9, 30)},
    )

    for clave in conflictos[0].metadata:
        assert "id" not in clave.lower()


def test_adjacency_no_cuenta_como_overlap_en_ocupacion(db_session):
    prof = _profesional(db_session, duracion_min=30)
    fecha = _dia_habil_futuro_obj()
    db_session.commit()

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha, hora_obj=time(9, 30),
        hoy=date.today(), dia_cerrado=None, ocupadas={time(9, 0)},
    )

    assert "slot_ocupado" not in _codigos(conflictos)


def test_fuera_de_jornada_aislado(db_session):
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="16:00", duracion_min=30)
    fecha = _dia_habil_futuro_obj()
    db_session.commit()

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha, hora_obj=time(16, 0),
        hoy=date.today(), dia_cerrado=None, ocupadas=set(),
    )

    assert _codigos(conflictos) == ["fuera_de_jornada"]
    assert conflictos[0].categoria == "jornada_profesional"
    assert conflictos[0].overridable_con_sobrecupo is True
    metadata = conflictos[0].metadata
    assert set(metadata.keys()) == {
        "jornada_inicio", "jornada_fin", "inicio_solicitado", "fin_solicitado",
    }
    json.dumps(metadata)


def test_en_colacion_aislado(db_session):
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="17:00",
        hora_almuerzo_inicio="13:00", hora_almuerzo_fin="14:00", duracion_min=30,
    )
    fecha = _dia_habil_futuro_obj()
    db_session.commit()

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha, hora_obj=time(13, 0),
        hoy=date.today(), dia_cerrado=None, ocupadas=set(),
    )

    assert _codigos(conflictos) == ["en_colacion"]
    assert conflictos[0].categoria == "jornada_profesional"
    assert conflictos[0].overridable_con_sobrecupo is True
    metadata = conflictos[0].metadata
    assert set(metadata.keys()) == {
        "inicio_colacion", "fin_colacion", "inicio_conflicto", "fin_conflicto",
        "minutos_afectados",
    }
    json.dumps(metadata)


def test_en_colacion_metadata_coincide_con_el_ejemplo_del_ticket(db_session):
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="18:00",
        hora_almuerzo_inicio="13:30", hora_almuerzo_fin="14:30", duracion_min=45,
    )
    fecha = _dia_habil_futuro_obj()
    db_session.commit()

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha, hora_obj=time(13, 15),
        hoy=date.today(), dia_cerrado=None, ocupadas=set(),
    )

    en_colacion = [c for c in conflictos if c.codigo == "en_colacion"][0]
    assert en_colacion.metadata == {
        "inicio_colacion": "13:30",
        "fin_colacion": "14:30",
        "inicio_conflicto": "13:30",
        "fin_conflicto": "14:00",
        "minutos_afectados": 30,
    }


def test_en_colacion_limite_semiabierto_no_cuenta_como_invasion(db_session):
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="17:00",
        hora_almuerzo_inicio="13:00", hora_almuerzo_fin="14:00", duracion_min=30,
    )
    fecha = _dia_habil_futuro_obj()
    db_session.commit()

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha, hora_obj=time(12, 30),
        hoy=date.today(), dia_cerrado=None, ocupadas=set(),
    )

    assert conflictos == ()


# ══════════════════════════════════════════════════════════
# 2) Combinaciones simultáneas obligatorias
# ══════════════════════════════════════════════════════════

def test_slot_ocupado_mas_en_colacion(db_session):
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="17:00",
        hora_almuerzo_inicio="13:00", hora_almuerzo_fin="14:00", duracion_min=30,
    )
    fecha = _dia_habil_futuro_obj()
    db_session.commit()

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha, hora_obj=time(13, 0),
        hoy=date.today(), dia_cerrado=None, ocupadas={time(13, 0)},
    )

    assert _codigos(conflictos) == ["slot_ocupado", "en_colacion"]
    assert hay_bloqueo_absoluto(conflictos) is True

    disponible, motivo, _mensaje, overridable = _legacy(
        prof, fecha, time(13, 0), date.today(), ocupadas={time(13, 0)},
    )
    assert (disponible, motivo, overridable) == (False, "slot_ocupado", False)
    assert motivo == conflictos[0].codigo


def test_slot_ocupado_mas_fuera_de_jornada(db_session):
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="16:00", duracion_min=30)
    fecha = _dia_habil_futuro_obj()
    db_session.commit()

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha, hora_obj=time(16, 0),
        hoy=date.today(), dia_cerrado=None, ocupadas={time(16, 0)},
    )

    assert _codigos(conflictos) == ["slot_ocupado", "fuera_de_jornada"]
    assert hay_bloqueo_absoluto(conflictos) is True

    disponible, motivo, _mensaje, overridable = _legacy(
        prof, fecha, time(16, 0), date.today(), ocupadas={time(16, 0)},
    )
    assert (disponible, motivo, overridable) == (False, "slot_ocupado", False)
    assert motivo == conflictos[0].codigo


def test_slot_ocupado_mas_fuera_de_jornada_mas_en_colacion(db_session):
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="13:00",
        hora_almuerzo_inicio="13:00", hora_almuerzo_fin="14:00", duracion_min=45,
    )
    fecha = _dia_habil_futuro_obj()
    db_session.commit()

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha, hora_obj=time(12, 30),
        hoy=date.today(), dia_cerrado=None, ocupadas={time(12, 30)},
    )

    assert _codigos(conflictos) == ["slot_ocupado", "fuera_de_jornada", "en_colacion"]
    assert hay_bloqueo_absoluto(conflictos) is True

    disponible, motivo, _mensaje, overridable = _legacy(
        prof, fecha, time(12, 30), date.today(), ocupadas={time(12, 30)},
    )
    assert motivo == conflictos[0].codigo == "slot_ocupado"


def test_fuera_de_jornada_mas_en_colacion_sin_slot_ocupado(db_session):
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="13:00",
        hora_almuerzo_inicio="13:00", hora_almuerzo_fin="14:00", duracion_min=45,
    )
    fecha = _dia_habil_futuro_obj()
    db_session.commit()

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha, hora_obj=time(12, 30),
        hoy=date.today(), dia_cerrado=None, ocupadas=set(),
    )

    assert _codigos(conflictos) == ["fuera_de_jornada", "en_colacion"]
    assert hay_bloqueo_absoluto(conflictos) is False

    disponible, motivo, _mensaje, overridable = _legacy(
        prof, fecha, time(12, 30), date.today(),
    )
    assert (disponible, motivo, overridable) == (False, "fuera_de_jornada", True)
    assert motivo == conflictos[0].codigo


def test_excede_cierre_centro_mas_fuera_de_jornada(db_session):
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="17:00", duracion_min=90)
    fecha = _dia_habil_futuro_obj()
    db_session.commit()

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha, hora_obj=time(17, 0),
        hoy=date.today(), dia_cerrado=None, ocupadas=set(),
    )

    assert _codigos(conflictos) == ["excede_cierre_centro", "fuera_de_jornada"]
    assert hay_bloqueo_absoluto(conflictos) is True

    disponible, motivo, _mensaje, overridable = _legacy(
        prof, fecha, time(17, 0), date.today(),
    )
    assert (disponible, motivo, overridable) == (False, "excede_cierre_centro", False)
    assert motivo == conflictos[0].codigo


# ══════════════════════════════════════════════════════════
# 3) Slot totalmente disponible -> conflictos vacío
# ══════════════════════════════════════════════════════════

def test_slot_disponible_conflictos_vacio(db_session):
    prof = _profesional(db_session)
    fecha = _dia_habil_futuro_obj()
    db_session.commit()

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha, hora_obj=time(10, 0),
        hoy=date.today(), dia_cerrado=None, ocupadas=set(),
    )

    assert conflictos == ()
    assert hay_bloqueo_absoluto(conflictos) is False


# ══════════════════════════════════════════════════════════
# 4) Orden determinista
# ══════════════════════════════════════════════════════════

def test_orden_es_determinista_entre_llamadas(db_session):
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="13:00",
        hora_almuerzo_inicio="13:00", hora_almuerzo_fin="14:00", duracion_min=45,
    )
    fecha = _dia_habil_futuro_obj()
    db_session.commit()

    primera = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha, hora_obj=time(12, 30),
        hoy=date.today(), dia_cerrado=None, ocupadas={time(12, 30)},
    )
    segunda = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha, hora_obj=time(12, 30),
        hoy=date.today(), dia_cerrado=None, ocupadas={time(12, 30)},
    )

    assert _codigos(primera) == _codigos(segunda) == ["slot_ocupado", "fuera_de_jornada", "en_colacion"]


# ══════════════════════════════════════════════════════════
# 5) resultado.motivo == conflictos[0].codigo (invariante general)
# ══════════════════════════════════════════════════════════

def test_motivo_coincide_con_primer_conflicto_en_multiples_escenarios(db_session):
    """v2: para cualquier ResultadoDisponibilidad con contexto
    analizable donde conflictos no está vacío, el motivo ganador
    legacy es siempre exactamente conflictos[0].codigo — por
    construcción de _legacy_desde_conflictos(), ya no por dos
    implementaciones que "casualmente" coinciden."""
    fecha = _dia_habil_futuro()

    escenarios = [
        # (kwargs profesional, hora, ocupar_primero, motivo_esperado)
        (dict(horario_inicio="09:00", horario_fin="17:00",
              hora_almuerzo_inicio="13:00", hora_almuerzo_fin="14:00", duracion_min=30),
         "13:00", False, "en_colacion"),
        (dict(horario_inicio="09:00", horario_fin="16:00", duracion_min=30),
         "16:00", False, "fuera_de_jornada"),
        (dict(horario_inicio="09:00", horario_fin="17:00", duracion_min=90),
         "17:00", False, "excede_cierre_centro"),
        (dict(duracion_min=30), "09:30", True, "slot_ocupado"),
    ]

    for kwargs, hora, ocupar, motivo_esperado in escenarios:
        prof = _profesional(db_session, **kwargs)
        db_session.commit()
        if ocupar:
            ocupante = _usuario(db_session, correo=f"ocupante-{prof.id}@sesaes.cl", rol="estudiante", rut=f"oc-{prof.id}")
            db_session.commit()
            citas.crear_cita(
                cita=CitaCreate(estudiante_id=ocupante.id, profesional_id=prof.id, fecha=fecha, hora=hora),
                db=db_session, current_user=_current_user(ocupante),
            )

        resultado = evaluar_disponibilidad_slot(
            db_session, profesional_id=prof.id, fecha=fecha, hora=hora,
        )

        assert resultado.motivo == motivo_esperado, (kwargs, hora, resultado.motivo)
        assert resultado.conflictos, "se esperaba al menos un conflicto estructurado"
        assert resultado.motivo == resultado.conflictos[0].codigo


def test_terminales_mantienen_conflictos_vacio(db_session):
    """Los 4 motivos terminales (resueltos en el wrapper público, antes
    de que exista contexto analizable) siguen con conflictos == ()."""
    prof_inactivo = _profesional(db_session, estado="licencia")
    db_session.commit()

    casos = [
        dict(profesional_id=999999, fecha=_dia_habil_futuro(), hora="09:00"),  # profesional_no_encontrado
        dict(profesional_id=prof_inactivo.id, fecha=_dia_habil_futuro(), hora="09:00"),  # profesional_inactivo
    ]
    motivos_esperados = ["profesional_no_encontrado", "profesional_inactivo"]

    for caso, motivo_esperado in zip(casos, motivos_esperados):
        resultado = evaluar_disponibilidad_slot(db_session, **caso)
        assert resultado.motivo == motivo_esperado
        assert resultado.conflictos == ()

    prof = _profesional(db_session)
    db_session.commit()
    resultado_fecha = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha="fecha-no-valida", hora="09:00",
    )
    assert resultado_fecha.motivo == "fecha_invalida"
    assert resultado_fecha.conflictos == ()

    resultado_hora = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=_dia_habil_futuro(), hora="hora-no-valida",
    )
    assert resultado_hora.motivo == "hora_invalida"
    assert resultado_hora.conflictos == ()


# ══════════════════════════════════════════════════════════
# 6) Reloj determinista: un solo snapshot por evaluación
# ══════════════════════════════════════════════════════════

def test_evaluar_disponibilidad_slot_lee_el_reloj_una_sola_vez(db_session, monkeypatch):
    """v2, punto 2: evaluar_disponibilidad_slot() debe leer
    datetime.now() EXACTAMENTE UNA VEZ por evaluación, y ese mismo
    snapshot debe alimentar tanto el análisis estructurado como la
    decisión legacy — no pueden discrepar por un cambio de minuto a
    mitad de la evaluación porque ya no hay una segunda lectura.

    Se simula un reloj que avanza entre lecturas: si el código
    volviera a leerlo una segunda vez, obtendría un valor distinto que
    haría que hora_pasada NO se detectara — este test falla si eso
    ocurre.
    """
    prof = _profesional(db_session, horario_inicio="00:00", horario_fin="23:59", duracion_min=30)
    hoy = date.today()
    db_session.commit()

    # Primera lectura (la única que debería ocurrir): 08:31 -> 08:30 ya
    # pasó. Segunda lectura, si hubiera una (bug): 07:00 -> 08:30 NO
    # habría pasado todavía. Ambas lecturas llevan a conclusiones
    # opuestas a propósito, para que cualquier lectura extra sea
    # detectable.
    lecturas = iter([
        datetime.combine(hoy, time(8, 31)),
        datetime.combine(hoy, time(7, 0)),
    ])
    contador = {"n": 0}

    class _RelojQueAvanza(datetime):
        @classmethod
        def now(cls, tz=None):
            contador["n"] += 1
            return next(lecturas)

    monkeypatch.setattr(agenda_disponibilidad_service, "datetime", _RelojQueAvanza)

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=hoy.isoformat(), hora="08:30",
    )

    assert contador["n"] == 1, "datetime.now() se leyó más de una vez en una sola evaluación"
    assert resultado.motivo == "hora_pasada"
    assert resultado.conflictos[0].codigo == "hora_pasada"


def test_listar_disponibilidad_rango_lee_el_reloj_una_sola_vez_por_rango(db_session, monkeypatch):
    """Mismo patrón que ya usaba `hoy`: un solo snapshot de reloj para
    todo el rango (no uno por día ni por slot)."""
    from app.services.agenda_disponibilidad_service import listar_disponibilidad_rango

    prof = _profesional(db_session, duracion_min=30)
    fecha = _dia_habil_futuro()
    db_session.commit()

    contador = {"n": 0}
    real_datetime = agenda_disponibilidad_service.datetime

    class _RelojContador(real_datetime):
        @classmethod
        def now(cls, tz=None):
            contador["n"] += 1
            return real_datetime.now(tz)

    monkeypatch.setattr(agenda_disponibilidad_service, "datetime", _RelojContador)

    listar_disponibilidad_rango(
        db_session, profesional_id=prof.id, fecha_inicio=fecha, fecha_fin=fecha,
    )

    assert contador["n"] == 1


# ══════════════════════════════════════════════════════════
# 7) ResultadoDisponibilidad.conflictos (integración con
#    evaluar_disponibilidad_slot)
# ══════════════════════════════════════════════════════════

def test_resultado_disponibilidad_incluye_conflictos_estructurados(db_session):
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="13:00",
        hora_almuerzo_inicio="13:00", hora_almuerzo_fin="14:00", duracion_min=45,
    )
    fecha = _dia_habil_futuro()
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=fecha, hora="12:30",
    )

    assert resultado.motivo == "fuera_de_jornada"
    assert _codigos(resultado.conflictos) == ["fuera_de_jornada", "en_colacion"]
    for conflicto in resultado.conflictos:
        assert isinstance(conflicto, ConflictoSlot)
        json.dumps(conflicto.metadata)


def test_resultado_disponibilidad_conflictos_vacio_cuando_disponible(db_session):
    prof = _profesional(db_session)
    fecha = _dia_habil_futuro()
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=fecha, hora="10:00",
    )

    assert resultado.disponible is True
    assert resultado.conflictos == ()


# ══════════════════════════════════════════════════════════
# 8) listar_disponibilidad_rango(): sin cambio de contrato
# ══════════════════════════════════════════════════════════

def test_listar_disponibilidad_rango_no_gana_campo_conflictos(db_session):
    from app.services.agenda_disponibilidad_service import listar_disponibilidad_rango

    prof = _profesional(db_session)
    fecha = _dia_habil_futuro()
    db_session.commit()

    resultado = listar_disponibilidad_rango(
        db_session, profesional_id=prof.id, fecha_inicio=fecha, fecha_fin=fecha,
    )

    assert set(resultado.keys()) == {
        "profesional_id", "fecha_inicio", "fecha_fin", "duracion_min", "dias",
    }
    dia = resultado["dias"][0]
    assert set(dia.keys()) == {"fecha", "slots"}
    for slot in dia["slots"]:
        assert set(slot.keys()) == {"hora", "disponible", "motivo", "overridable_con_sobrecupo"}


# ══════════════════════════════════════════════════════════
# 9) Persistencia: N filas de CitaSobrecupoConflicto por cada
#    conflicto realmente superado
# ══════════════════════════════════════════════════════════

def test_sobrecupo_con_un_conflicto_persiste_una_fila(db_session, monkeypatch):
    fecha = _dia_habil_futuro()
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="17:00",
        hora_almuerzo_inicio="13:00", hora_almuerzo_fin="14:00", duracion_min=30,
    )
    paciente = _usuario(db_session, correo="paciente-1conf-a42@sesaes.cl", rol="estudiante", rut="a42-1")
    admin_user = _usuario(db_session, correo="admin-1conf-a42@sesaes.cl", rol="admin", rut="a42-2")
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, citas)

    payload = CitaCreate(
        estudiante_id=paciente.id, profesional_id=prof.id, fecha=fecha, hora="13:00",
        sobrecupo=True,
    )
    citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_user))

    creada = db_session.query(Cita).one()
    assert creada.sobrecupo is True
    detalle = db_session.query(CitaSobrecupo).filter(CitaSobrecupo.cita_id == creada.id).one()
    conflictos = (
        db_session.query(CitaSobrecupoConflicto)
        .filter(CitaSobrecupoConflicto.cita_sobrecupo_id == detalle.id)
        .order_by(CitaSobrecupoConflicto.id)
        .all()
    )
    assert [c.codigo for c in conflictos] == ["en_colacion"]


def test_sobrecupo_con_dos_conflictos_persiste_dos_filas(db_session, monkeypatch):
    fecha = _dia_habil_futuro()
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="13:00",
        hora_almuerzo_inicio="13:00", hora_almuerzo_fin="14:00", duracion_min=45,
    )
    paciente = _usuario(db_session, correo="paciente-2conf-a42@sesaes.cl", rol="estudiante", rut="a42-3")
    admin_user = _usuario(db_session, correo="admin-2conf-a42@sesaes.cl", rol="admin", rut="a42-4")
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, citas)

    payload = CitaCreate(
        estudiante_id=paciente.id, profesional_id=prof.id, fecha=fecha, hora="12:30",
        sobrecupo=True,
    )
    citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_user))

    creada = db_session.query(Cita).one()
    assert creada.sobrecupo is True
    detalle = db_session.query(CitaSobrecupo).filter(CitaSobrecupo.cita_id == creada.id).one()
    conflictos = (
        db_session.query(CitaSobrecupoConflicto)
        .filter(CitaSobrecupoConflicto.cita_sobrecupo_id == detalle.id)
        .order_by(CitaSobrecupoConflicto.id)
        .all()
    )
    assert [c.codigo for c in conflictos] == ["fuera_de_jornada", "en_colacion"]

    # A.4.1 — la auditoría sigue intacta, SIN CAMBIOS: el detalle sigue
    # mencionando únicamente el motivo ganador legacy (singular), no la
    # lista completa de conflictos superados.
    evento = db_session.query(Auditoria).filter(Auditoria.entidad_id == creada.id).one()
    assert "Conflicto superado: fuera_de_jornada" in evento.detalle
    assert "en_colacion" not in evento.detalle


def test_cita_normal_no_persiste_ninguna_fila(db_session):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    est = _usuario(db_session, correo="est-normal-a42@sesaes.cl", rol="estudiante", rut="a42-5")
    db_session.commit()

    payload = CitaCreate(estudiante_id=est.id, profesional_id=prof.id, fecha=fecha, hora="09:30")
    citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(est))

    assert db_session.query(CitaSobrecupo).count() == 0
    assert db_session.query(CitaSobrecupoConflicto).count() == 0


# ══════════════════════════════════════════════════════════
# 10) Conflicto absoluto -> rechazo, incluso con sobrecupo=True
# ══════════════════════════════════════════════════════════

def test_conflicto_absoluto_junto_a_overridable_rechaza_sobrecupo(db_session, monkeypatch):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="17:00", duracion_min=90)
    paciente = _usuario(db_session, correo="paciente-absoluto-a42@sesaes.cl", rol="estudiante", rut="a42-6")
    admin_user = _usuario(db_session, correo="admin-absoluto-a42@sesaes.cl", rol="admin", rut="a42-7")
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, citas)

    payload = CitaCreate(
        estudiante_id=paciente.id, profesional_id=prof.id, fecha=fecha, hora="17:00",
        sobrecupo=True,
    )
    with pytest.raises(HTTPException) as exc_info:
        citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_user))

    assert exc_info.value.status_code == 400
    assert db_session.query(Cita).count() == 0
    assert db_session.query(CitaSobrecupo).count() == 0
    assert db_session.query(CitaSobrecupoConflicto).count() == 0


def test_slot_ocupado_sigue_rechazado_pese_a_sobrecupo(db_session, monkeypatch):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session, duracion_min=30)
    ocupante = _usuario(db_session, correo="ocupante-a42@sesaes.cl", rol="estudiante", rut="a42-8")
    db_session.commit()

    citas.crear_cita(
        cita=CitaCreate(estudiante_id=ocupante.id, profesional_id=prof.id, fecha=fecha, hora="09:30"),
        db=db_session, current_user=_current_user(ocupante),
    )

    paciente = _usuario(db_session, correo="paciente-slotocupado-a42@sesaes.cl", rol="estudiante", rut="a42-9")
    admin_user = _usuario(db_session, correo="admin-slotocupado-a42@sesaes.cl", rol="admin", rut="a42-10")
    db_session.commit()
    _monkeypatch_admin_institucional(monkeypatch, citas)

    payload = CitaCreate(
        estudiante_id=paciente.id, profesional_id=prof.id, fecha=fecha, hora="09:30",
        sobrecupo=True,
    )
    with pytest.raises(HTTPException) as exc_info:
        citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_user))

    assert exc_info.value.status_code == 409
    assert db_session.query(Cita).count() == 1
    assert db_session.query(CitaSobrecupo).count() == 0
