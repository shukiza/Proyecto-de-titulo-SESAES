# -*- coding: utf-8 -*-
"""
SESAES — A.2C: hardening de duración completa de Agenda.

Cubre el intervalo semiabierto [inicio, fin) — fin = inicio +
_duracion_efectiva(profesional) — en vez de solo la hora de inicio, en
app.services.agenda_disponibilidad_service:

  - Nuevo motivo `excede_cierre_centro` (no overridable): el intervalo
    completo no puede terminar después de HORA_FIN_CENTRO aunque el
    inicio sí pertenezca a la grilla.
  - `fuera_de_jornada` / `en_colacion` (overridable, sin cambio de
    política) ahora se calculan por superposición de intervalos, no
    solo por el punto de inicio.
  - `slot_ocupado` (no overridable) ahora detecta solapamiento parcial
    entre intervalos, no solo igualdad exacta de hora de inicio.
  - Límites exactos (semiabiertos): terminar justo cuando empieza
    colación/jornada/cierre del centro sigue siendo válido.
  - `listar_horas_disponibles()` (GET /disponibilidad/{id}, Estudiante)
    queda alineada con el mismo criterio de intervalo completo, para
    que no ofrezca como "disponible" una hora que POST /citas
    rechazaría.

No se tocan aquí los tests ya existentes de A.2/A.2A/A.2B (siguen en
test_a2_disponibilidad_real.py / test_a2b_disponibilidad_rango*.py) —
este archivo es adicional.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Registrar modelos/relaciones antes de create_all.
import app.models.init  # noqa: F401
import app.models.solicitud_horario  # noqa: F401

from app.database import Base
from app.models.cita import Cita
from app.models.profesional import Profesional
from app.models.usuario import Usuario
from app.rbac.admin_authorization import AlcanceAdministrativoEfectivo
from app.rbac.permissions import Permission
from app.routers import citas
import app.services.sobrecupo_policy_service as sobrecupo_policy_service
from app.schemas import CitaCreate
from app.services.agenda_disponibilidad_service import (
    evaluar_disponibilidad_slot,
    listar_horas_disponibles,
)


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
        nombre="Profesional Test A2C",
        especialidad=especialidad,
        iniciales="PC",
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


def _estudiante(db, *, correo="est-a2c@sesaes.cl", rut="33.333.333-3"):
    usuario = Usuario(
        correo=correo,
        password="hash-a2c",
        rol="estudiante",
        nombre="Estudiante Test A2C",
        rut=rut,
        activo=True,
    )
    db.add(usuario)
    db.flush()
    return usuario


def _cita(db, *, estudiante_id, profesional_id, fecha, hora, estado="pendiente", sobrecupo=False):
    cita = Cita(
        estudiante_id=estudiante_id,
        profesional_id=profesional_id,
        fecha=fecha,
        hora=hora,
        estado=estado,
        sobrecupo=sobrecupo,
    )
    db.add(cita)
    db.flush()
    return cita


def _monkeypatch_admin_institucional(
    monkeypatch, *modulos, gestionar: bool = True, sobrecupo: bool = True,
):
    """A.4.3 — mismo criterio que test_a2_disponibilidad_real.py: nunca
    devuelve True para cualquier permiso, solo resuelve explícitamente
    agenda.gestionar/agenda.sobrecupo (la política de sobrecupo los
    consulta en su propio namespace, aparte de citas.py)."""
    def _resolver(db, current_user, permiso):
        if permiso == Permission.AGENDA_GESTIONAR:
            return gestionar
        if permiso == Permission.AGENDA_SOBRECUPO:
            return sobrecupo
        raise AssertionError(
            f"_monkeypatch_admin_institucional no contempla el permiso {permiso!r}"
        )

    for modulo in modulos:
        if hasattr(modulo, "tiene_permiso_efectivo"):
            monkeypatch.setattr(modulo, "tiene_permiso_efectivo", _resolver)
        if hasattr(modulo, "obtener_alcance_administrativo_efectivo"):
            monkeypatch.setattr(
                modulo,
                "obtener_alcance_administrativo_efectivo",
                lambda db, u: AlcanceAdministrativoEfectivo(
                    institucional=True, especialidades_normalizadas=frozenset(),
                ),
            )


# ──────────────────────────────────────────────────────────
# Caso 1 y 2 — colación: límite exacto válido vs. invasión
# ──────────────────────────────────────────────────────────

def test_cita_que_termina_exactamente_cuando_empieza_colacion_es_valida(db_session):
    """12:30–13:00 (duración 30) no invade una colación 13:00–14:00:
    el intervalo de la cita termina exactamente cuando empieza la
    colación (límite semiabierto, sin superposición)."""
    prof = _profesional(
        db_session,
        horario_inicio="09:00", horario_fin="17:00",
        hora_almuerzo_inicio="13:00", hora_almuerzo_fin="14:00",
        duracion_min=30,
    )
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=_dia_habil_futuro(), hora="12:30",
    )

    assert resultado.disponible is True
    assert resultado.motivo is None


def test_cita_que_invade_colacion_es_rechazada_y_overridable(db_session):
    """12:30–13:00 (duración 30) SÍ invade una colación 12:45–13:45:
    el inicio es antes de la colación pero el intervalo se superpone
    con ella (antes del hardening solo se comprobaba el punto de
    inicio, 12:30, que no cae dentro de [12:45,13:45) y se aceptaba
    incorrectamente)."""
    prof = _profesional(
        db_session,
        horario_inicio="09:00", horario_fin="17:00",
        hora_almuerzo_inicio="12:45", hora_almuerzo_fin="13:45",
        duracion_min=30,
    )
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=_dia_habil_futuro(), hora="12:30",
    )

    assert resultado.disponible is False
    assert resultado.motivo == "en_colacion"
    assert resultado.overridable_con_sobrecupo is True


# ──────────────────────────────────────────────────────────
# Caso 3 — comienza dentro de colación
# ──────────────────────────────────────────────────────────

def test_cita_que_comienza_en_colacion_es_rechazada_y_overridable(db_session):
    prof = _profesional(
        db_session,
        horario_inicio="09:00", horario_fin="17:00",
        hora_almuerzo_inicio="12:45", hora_almuerzo_fin="13:45",
        duracion_min=30,
    )
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=_dia_habil_futuro(), hora="13:00",
    )

    assert resultado.disponible is False
    assert resultado.motivo == "en_colacion"
    assert resultado.overridable_con_sobrecupo is True


# ──────────────────────────────────────────────────────────
# Caso 4 y 5 — fin de jornada: límite exacto válido vs. exceso
# ──────────────────────────────────────────────────────────

def test_cita_que_termina_exactamente_al_final_de_jornada_es_valida(db_session):
    """16:30–17:00 (duración 30) con jornada hasta las 17:00: termina
    justo en el límite, no lo excede."""
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="17:00", duracion_min=30,
    )
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=_dia_habil_futuro(), hora="16:30",
    )

    assert resultado.disponible is True
    assert resultado.motivo is None


def test_cita_que_excede_jornada_del_profesional_es_rechazada_y_overridable(db_session):
    """Caso que antes ESCAPABA al chequeo por punto: 17:00 (duración
    45) SÍ pertenece a la jornada por su hora de INICIO (09:00–17:30,
    17:00 < 17:30), así que la validación antigua basada solo en el
    punto de inicio la aceptaba como disponible. Pero el intervalo real
    es 17:00–17:45, que TERMINA después de que el profesional sale
    (17:30) — exactamente el caso "empieza dentro de jornada pero
    termina después de la jornada" del diagnóstico del hardening.
    Sigue siendo overridable porque es la jornada del PROFESIONAL, no
    el cierre del centro."""
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="17:30", duracion_min=45,
    )
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=_dia_habil_futuro(), hora="17:00",
    )

    assert resultado.disponible is False
    assert resultado.motivo == "fuera_de_jornada"
    assert resultado.overridable_con_sobrecupo is True


# ──────────────────────────────────────────────────────────
# Caso 6 y 7 — cierre del centro: límite exacto válido vs. exceso
# absoluto (NO overridable, a diferencia de la jornada del profesional)
# ──────────────────────────────────────────────────────────

def test_cita_que_termina_exactamente_al_cierre_del_centro_es_valida(db_session):
    """17:30–18:00 (duración 30) con jornada del profesional hasta las
    18:00 (igual que el cierre del centro): termina justo en el cierre,
    no lo excede."""
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="18:00", duracion_min=30,
    )
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=_dia_habil_futuro(), hora="17:30",
    )

    assert resultado.disponible is True
    assert resultado.motivo is None


def test_cita_que_excede_cierre_del_centro_es_bloqueo_absoluto(db_session):
    """17:45–18:30 (duración 45) con jornada del profesional hasta las
    18:00: el intervalo excede el cierre OPERATIVO DEL CENTRO
    (HORA_FIN_CENTRO=18:00), no solo la jornada del profesional. Debe
    rechazarse con el nuevo motivo 'excede_cierre_centro', NO
    overridable — a diferencia de 'fuera_de_jornada', el sobrecupo
    nunca puede "abrir el centro" más allá de su cierre."""
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="18:00", duracion_min=45,
    )
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=_dia_habil_futuro(), hora="17:45",
    )

    assert resultado.disponible is False
    assert resultado.motivo == "excede_cierre_centro"
    assert resultado.overridable_con_sobrecupo is False


def test_sobrecupo_admin_no_supera_excede_cierre_centro(db_session, monkeypatch):
    """Integración end-to-end: sobrecupo=True nunca debe poder superar
    'excede_cierre_centro' — es un bloqueo absoluto del centro, no de
    la jornada de un profesional."""
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="18:00", duracion_min=45,
    )
    est = _estudiante(db_session)
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, citas)

    payload = CitaCreate(
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=_dia_habil_futuro(),
        hora="17:45",
        sobrecupo=True,
    )

    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(cita=payload, db=db_session, current_user={"id": 999, "rol": "admin"})

    assert exc.value.status_code == 400
    assert db_session.query(Cita).count() == 0


# ──────────────────────────────────────────────────────────
# Caso 8 y 9 — solapamiento parcial de intervalos (no solo igualdad
# exacta de hora de inicio). Mismo ejemplo del bloque de hardening:
# existente 10:00–10:45, nueva 09:30–10:15 (comienza antes, termina
# dentro) y nueva 10:15–11:00 (comienza dentro).
# ──────────────────────────────────────────────────────────

def test_nueva_cita_que_comienza_antes_y_termina_dentro_de_cita_activa_es_slot_ocupado(db_session):
    fecha = _dia_habil_futuro()
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="17:00", duracion_min=45,
    )
    ocupante = _estudiante(db_session, correo="ocupante-a2c-1@sesaes.cl", rut="10-1")
    _cita(db_session, estudiante_id=ocupante.id, profesional_id=prof.id, fecha=fecha, hora="10:00")
    db_session.commit()

    # 09:30 está alineado a la grilla cruda del centro (pasos de 45 min
    # desde 08:00: 08:00, 08:45, 09:30, 10:15...).
    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=fecha, hora="09:30",
    )

    assert resultado.disponible is False
    assert resultado.motivo == "slot_ocupado"
    assert resultado.overridable_con_sobrecupo is False


def test_nueva_cita_que_comienza_dentro_de_cita_activa_es_slot_ocupado(db_session):
    fecha = _dia_habil_futuro()
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="17:00", duracion_min=45,
    )
    ocupante = _estudiante(db_session, correo="ocupante-a2c-2@sesaes.cl", rut="10-2")
    _cita(db_session, estudiante_id=ocupante.id, profesional_id=prof.id, fecha=fecha, hora="10:00")
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=fecha, hora="10:15",
    )

    assert resultado.disponible is False
    assert resultado.motivo == "slot_ocupado"
    assert resultado.overridable_con_sobrecupo is False


def test_intervalos_adyacentes_sin_solape_no_generan_falso_slot_ocupado(db_session):
    """Frontera semiabierta de ocupación: una cita existente 10:15–11:00
    y una nueva 09:30–10:15 son ADYACENTES, no solapadas — el intervalo
    nuevo termina exactamente cuando empieza el existente. Bajo la
    semántica semiabierta [inicio, fin) esto NO es superposición (ver
    _intervalos_se_superponen()); debe seguir disponible."""
    fecha = _dia_habil_futuro()
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="17:00", duracion_min=45,
    )
    ocupante = _estudiante(db_session, correo="ocupante-a2c-adyacente@sesaes.cl", rut="10-9")
    _cita(db_session, estudiante_id=ocupante.id, profesional_id=prof.id, fecha=fecha, hora="10:15")
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=fecha, hora="09:30",
    )

    assert resultado.disponible is True
    assert resultado.motivo is None


def test_crear_cita_estudiante_rechaza_solapamiento_parcial_aunque_hora_inicio_sea_distinta(db_session):
    """Integración end-to-end del ejemplo del bloque de hardening: una
    cita existente 10:00–10:45 debe bloquear un intento de agendar
    09:30–10:15 (hora de inicio distinta, pero intervalos solapados)."""
    fecha = _dia_habil_futuro()
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="17:00", duracion_min=45,
    )
    ocupante = _estudiante(db_session, correo="ocupante-a2c-3@sesaes.cl", rut="10-3")
    nuevo = _estudiante(db_session, correo="nuevo-a2c-3@sesaes.cl", rut="10-4")
    _cita(db_session, estudiante_id=ocupante.id, profesional_id=prof.id, fecha=fecha, hora="10:00")
    db_session.commit()

    payload = CitaCreate(
        estudiante_id=nuevo.id,
        profesional_id=prof.id,
        fecha=fecha,
        hora="09:30",
    )

    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(
            cita=payload,
            db=db_session,
            current_user={"id": nuevo.id, "rol": "estudiante"},
        )

    # A.3 — slot_ocupado (aquí, solapamiento parcial de intervalos)
    # tras la re-evaluación dentro del lock es un conflicto (409), no
    # una simple invalidez de datos (400).
    assert exc.value.status_code == 409
    assert db_session.query(Cita).count() == 1  # solo la ya existente


def test_sobrecupo_admin_no_supera_solapamiento_parcial(db_session, monkeypatch):
    """El nuevo cálculo de solapamiento sigue siendo un bloqueo
    ABSOLUTO: sobrecupo=True no debe poder saltárselo, igual que ya
    ocurría con la igualdad exacta de hora de inicio (corrección v4)."""
    fecha = _dia_habil_futuro()
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="17:00", duracion_min=45,
    )
    ocupante = _estudiante(db_session, correo="ocupante-a2c-4@sesaes.cl", rut="10-5")
    nuevo = _estudiante(db_session, correo="nuevo-a2c-4@sesaes.cl", rut="10-6")
    _cita(db_session, estudiante_id=ocupante.id, profesional_id=prof.id, fecha=fecha, hora="10:00")
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, citas)

    payload = CitaCreate(
        estudiante_id=nuevo.id,
        profesional_id=prof.id,
        fecha=fecha,
        hora="09:30",
        sobrecupo=True,
    )

    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(cita=payload, db=db_session, current_user={"id": 999, "rol": "admin"})

    # A.3 — ver nota en test_crear_cita_estudiante_rechaza_solapamiento_parcial...
    assert exc.value.status_code == 409
    assert db_session.query(Cita).count() == 1


# ──────────────────────────────────────────────────────────
# Caso 10 — cita existente cancelada no bloquea, ni siquiera con
# solapamiento parcial de intervalos.
# ──────────────────────────────────────────────────────────

def test_cita_cancelada_con_intervalo_solapado_no_bloquea(db_session):
    fecha = _dia_habil_futuro()
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="17:00", duracion_min=45,
    )
    otro = _estudiante(db_session, correo="cancelada-a2c@sesaes.cl", rut="10-7")
    _cita(
        db_session, estudiante_id=otro.id, profesional_id=prof.id,
        fecha=fecha, hora="10:00", estado="cancelada",
    )
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=fecha, hora="09:30",
    )

    assert resultado.disponible is True


# ──────────────────────────────────────────────────────────
# Caso 11 — día cerrado sigue siendo un bloqueo absoluto, incluso para
# un intervalo que de otro modo sería válido bajo el hardening.
# ──────────────────────────────────────────────────────────

def test_dia_cerrado_sigue_siendo_absoluto_con_intervalo_por_lo_demas_valido(db_session):
    from app.models.dia_cerrado import DiaCerrado

    fecha = _dia_habil_futuro()
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="17:00", duracion_min=30,
    )
    db_session.add(DiaCerrado(fecha=fecha, motivo="Feriado A2C"))
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=fecha, hora="10:00",
    )

    assert resultado.disponible is False
    assert resultado.motivo == "dia_cerrado"
    assert resultado.overridable_con_sobrecupo is False


# ──────────────────────────────────────────────────────────
# Caso 13 — sobrecupo=True en POST /citas se conserva únicamente donde
# ya estaba permitido: SÍ puede superar una invasión de colación
# detectada ahora por intervalo completo (motivo nuevo respecto al
# punto de inicio, pero sigue siendo overridable); NO puede superar
# fuera_de_grilla ni excede_cierre_centro (ya cubierto arriba).
# ──────────────────────────────────────────────────────────

def test_sobrecupo_admin_supera_invasion_de_colacion_detectada_por_intervalo(db_session, monkeypatch):
    prof = _profesional(
        db_session,
        horario_inicio="09:00", horario_fin="17:00",
        hora_almuerzo_inicio="12:45", hora_almuerzo_fin="13:45",
        duracion_min=30,
    )
    est = _estudiante(db_session)
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, citas, sobrecupo_policy_service)

    payload = CitaCreate(
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=_dia_habil_futuro(),
        hora="12:30",  # 12:30–13:00: invade la colación 12:45–13:45
        sobrecupo=True,
        # A.4.3 — sobrecupo efectivo ahora exige motivo humano no vacío.
        sobrecupo_motivo="Paciente con turno de práctica que termina justo antes",
    )

    resultado = citas.crear_cita(cita=payload, db=db_session, current_user={"id": 999, "rol": "admin"})

    assert resultado["estado"] == "pendiente"
    creada = db_session.query(Cita).one()
    assert creada.sobrecupo is True


def test_estudiante_sin_sobrecupo_no_puede_invadir_colacion_detectada_por_intervalo(db_session):
    """Contraparte del test anterior: un estudiante (sin agenda.gestionar,
    sin sobrecupo) sigue sin poder agendar ese mismo intervalo."""
    prof = _profesional(
        db_session,
        horario_inicio="09:00", horario_fin="17:00",
        hora_almuerzo_inicio="12:45", hora_almuerzo_fin="13:45",
        duracion_min=30,
    )
    est = _estudiante(db_session)
    db_session.commit()

    payload = CitaCreate(
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=_dia_habil_futuro(),
        hora="12:30",
    )

    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(
            cita=payload,
            db=db_session,
            current_user={"id": est.id, "rol": "estudiante"},
        )

    assert exc.value.status_code == 400
    assert db_session.query(Cita).count() == 0


# ──────────────────────────────────────────────────────────
# Extra (no listado explícitamente en los 13 casos, pero necesario
# para no introducir una contradicción con el hardening): la lista de
# horas de Estudiante (listar_horas_disponibles / GET /disponibilidad)
# no debe ofrecer como "disponible" una hora que, con la duración real
# del profesional, invadiría colación o excedería el cierre del
# centro — de lo contrario Estudiante vería una hora "libre" que POST
# /citas rechazaría al intentar agendarla.
# ──────────────────────────────────────────────────────────

def test_listar_horas_disponibles_excluye_hora_que_invadiria_colacion_por_duracion(db_session):
    prof = _profesional(
        db_session,
        horario_inicio="09:00", horario_fin="17:00",
        hora_almuerzo_inicio="12:45", hora_almuerzo_fin="13:45",
        duracion_min=30,
    )
    db_session.commit()

    horas, _mensaje = listar_horas_disponibles(
        db_session, profesional_id=prof.id, fecha=_dia_habil_futuro(),
    )

    # 12:30–13:00 invade la colación 12:45–13:45: no debe listarse.
    assert "12:30" not in horas
    # 12:00–12:30 no la invade: sí debe listarse.
    assert "12:00" in horas


def test_listar_horas_disponibles_excluye_hora_que_excederia_cierre_centro(db_session):
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="18:00", duracion_min=45,
    )
    db_session.commit()

    horas, _mensaje = listar_horas_disponibles(
        db_session, profesional_id=prof.id, fecha=_dia_habil_futuro(),
    )

    # 17:45–18:30 excede HORA_FIN_CENTRO (18:00): no debe listarse.
    assert "17:45" not in horas
    # 17:00–17:45 sí cabe dentro del cierre del centro.
    assert "17:00" in horas


def test_listar_horas_disponibles_excluye_hora_con_solapamiento_parcial(db_session):
    fecha = _dia_habil_futuro()
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="17:00", duracion_min=45,
    )
    otro = _estudiante(db_session, correo="ocupante-a2c-listar@sesaes.cl", rut="10-8")
    _cita(db_session, estudiante_id=otro.id, profesional_id=prof.id, fecha=fecha, hora="10:00")
    db_session.commit()

    horas, _mensaje = listar_horas_disponibles(
        db_session, profesional_id=prof.id, fecha=fecha,
    )

    # 09:30–10:15 y 10:15–11:00 se solapan parcialmente con la cita
    # activa 10:00–10:45 (una empieza antes y termina dentro, la otra
    # empieza dentro).
    assert "09:30" not in horas
    assert "10:15" not in horas
    # 11:00–11:45 no se solapa con 10:00–10:45.
    assert "11:00" in horas
