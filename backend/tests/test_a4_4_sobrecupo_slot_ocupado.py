# -*- coding: utf-8 -*-
"""
SESAES — A.4.4: sobrecupo INTENCIONAL sobre slot_ocupado (máximo 2
citas activas simultáneas por intervalo).

Dos niveles de test, igual que A.4.3:

  1. PUROS de `_max_ocupacion_concurrente()` — sin DB, sin
     profesional/cita reales: solo horas + duración, ejercitando el
     event sweep en sí (cardinalidad, adyacencia, recorte al intervalo
     solicitado, empates FIN-antes-que-INICIO).

  2. INTEGRACIÓN vía `app.routers.citas.crear_cita()` con SQLite en
     memoria real, actor SUPERADMIN real (permisos resueltos por
     `has_permission()` contra `ROLE_DEFAULT_PERMISSIONS`, sin
     monkeypatch — ver test_a3_concurrencia_postgres.py para el mismo
     criterio de "actor real, no un dict fabricado con permiso
     inventado") — para el mapeo a status HTTP, la persistencia de
     Cita/CitaSobrecupo/CitaSobrecupoConflicto, la auditoría y las
     combinaciones con otros conflictos.

No repite lo que ya cubren test_a4_2_conflictos.py (estructura de
ConflictoSlot, precedencia) ni test_a4_3_politica_sobrecupo.py
(evaluar_politica_sobrecupo en sí, que A.4.4 NO modifica) — este
archivo solo agrega la dimensión nueva: cardinalidad/capacidad máxima
de slot_ocupado.
"""

from __future__ import annotations

from datetime import date, timedelta, time

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models.init  # noqa: F401 — registra TODOS los modelos productivos
import app.models.solicitud_horario  # noqa: F401

import app.services.sobrecupo_policy_service as sobrecupo_policy_service
from app.database import Base
from app.models.auditoria import Auditoria
from app.models.cita import Cita
from app.models.cita_sobrecupo import CitaSobrecupo, CitaSobrecupoConflicto
from app.models.dia_cerrado import DiaCerrado
from app.models.profesional import Profesional
from app.models.usuario import Usuario
from app.rbac.permissions import Permission
from app.routers import citas
from app.schemas import CitaCreate
from app.services.agenda_disponibilidad_service import (
    CAPACIDAD_MAXIMA_CITAS_SIMULTANEAS,
    ConflictoSlot,
    _max_ocupacion_concurrente,
    analizar_conflictos_slot,
    hay_bloqueo_absoluto,
    listar_disponibilidad_rango,
)


# ══════════════════════════════════════════════════════════
# Parte 1 — puros de _max_ocupacion_concurrente()
# ══════════════════════════════════════════════════════════

def test_capacidad_maxima_es_2():
    """La constante de negocio en sí — si esto cambia, cambió la
    decisión de producto, no un detalle de implementación."""
    assert CAPACIDAD_MAXIMA_CITAS_SIMULTANEAS == 2


def test_1_cero_ocupaciones_max_cero():
    assert _max_ocupacion_concurrente(time(9, 0), time(9, 30), [], 30) == 0


def test_2_una_ocupacion_coincidente_max_uno():
    assert _max_ocupacion_concurrente(
        time(9, 0), time(9, 30), [time(9, 0)], 30,
    ) == 1


def test_3_dos_filas_exactamente_iguales_max_dos():
    assert _max_ocupacion_concurrente(
        time(9, 0), time(9, 30), [time(9, 0), time(9, 0)], 30,
    ) == 2


def test_4_tres_filas_iguales_max_tres_sin_tope_artificial():
    """El helper reporta la realidad (3), nunca cap la cuenta a 2 por
    su cuenta — quien decide qué hacer con un max >= 2 es
    analizar_conflictos_slot()/la política, no este helper."""
    assert _max_ocupacion_concurrente(
        time(9, 0), time(9, 30), [time(9, 0), time(9, 0), time(9, 0)], 30,
    ) == 3


def test_5_intervalos_adyacentes_nunca_cuentan_2_simultaneas():
    """Contraejemplo central del diseño: [09:00,09:30) y [09:30,10:00)
    nunca coexisten, aunque ambos solapen una solicitud [09:00,10:00)
    que los abarca a los dos."""
    assert _max_ocupacion_concurrente(
        time(9, 0), time(10, 0), [time(9, 0), time(9, 30)], 30,
    ) == 1


def test_6_solapamiento_parcial_real_se_cuenta_correctamente():
    """Dos existentes que SÍ coexisten de verdad: [09:00,09:45) y
    [09:15,10:00) (duracion=45) se superponen en [09:15,09:45) — deben
    contarse como 2 simultáneas."""
    assert _max_ocupacion_concurrente(
        time(9, 0), time(10, 0), [time(9, 0), time(9, 15)], 45,
    ) == 2


def test_7_duplicados_no_se_pierden_en_secuencia_mixta():
    assert _max_ocupacion_concurrente(
        time(9, 0), time(9, 30),
        [time(9, 0), time(9, 0), time(9, 0)],
        30,
    ) == 3


def test_8_fin_existente_igual_a_inicio_solicitud_no_es_overlap():
    """Existente [08:30,09:00), solicitud [09:00,09:30) — el fin del
    existente coincide exactamente con el inicio solicitado: NO debe
    contarse."""
    assert _max_ocupacion_concurrente(
        time(9, 0), time(9, 30), [time(8, 30)], 30,
    ) == 0


def test_9_inicio_existente_igual_a_fin_solicitud_no_es_overlap():
    """Existente [09:30,10:00), solicitud [09:00,09:30) — el inicio
    del existente coincide exactamente con el fin solicitado: NO debe
    contarse."""
    assert _max_ocupacion_concurrente(
        time(9, 0), time(9, 30), [time(9, 30)], 30,
    ) == 0


def test_no_cuenta_filas_que_solapan_la_solicitud_pero_no_entre_si():
    """El contraejemplo exacto del diseño, con solicitud MÁS ANGOSTA
    que la unión de A y B: A=[09:00,09:30), B=[09:30,10:00),
    solicitud=[09:00,10:00) — contar FILAS que solapan la solicitud
    daría 2 (incorrecto); la ocupación concurrente real nunca supera
    1."""
    ocupadas = [time(9, 0), time(9, 30)]
    assert _max_ocupacion_concurrente(time(9, 0), time(10, 0), ocupadas, 30) == 1


def test_10_duplicados_mas_adyacencia_combinados_max_dos_no_tres():
    """A y B duplicadas a las 09:00 (2 simultáneas reales) + C
    adyacente a las 09:30 (nunca coexiste con A ni B) sobre una
    solicitud [09:00,10:00) que abarca las tres. Prueba a la vez:
      - duplicados preservados (A y B cuentan como 2, no como 1);
      - FIN antes que INICIO en el empate exacto de las 09:30 (A y B
        terminan justo cuando C empieza — C nunca se suma a ellas);
      - semántica [inicio, fin) semiabierta.
    Resultado esperado: max=2, NUNCA 3."""
    ocupadas = [time(9, 0), time(9, 0), time(9, 30)]  # A, B, C
    assert _max_ocupacion_concurrente(time(9, 0), time(10, 0), ocupadas, 30) == 2


# ══════════════════════════════════════════════════════════
# Parte 1b — analizar_conflictos_slot(): dinamismo del flag y
# combinaciones (no repite test_a4_2_conflictos.py, solo agrega la
# dimensión de capacidad y las combinaciones L/M pedidas para A.4.4)
# ══════════════════════════════════════════════════════════

def _profesional_obj(**kwargs):
    kwargs.setdefault("nombre", "Prof A4.4")
    kwargs.setdefault("especialidad", "Nutrición")
    kwargs.setdefault("iniciales", "P44")
    kwargs.setdefault("estado", "activo")
    kwargs.setdefault("horario_inicio", "09:00")
    kwargs.setdefault("horario_fin", "18:00")
    kwargs.setdefault("duracion_min", 45)
    return Profesional(**kwargs)


def _fecha_habil_futura_obj(dias_calendario: int = 10) -> date:
    fecha = date.today() + timedelta(days=dias_calendario)
    while fecha.weekday() >= 5:
        fecha += timedelta(days=1)
    return fecha


def test_l_slot_ocupado_mas_excede_cierre_centro_es_rechazo(db_session):
    """L — slot_ocupado (overridable con 1 sola ocupación) +
    excede_cierre_centro (SIEMPRE absoluto, A.4.4 no lo toca) debe
    seguir siendo rechazo total: 17:45 + 45min = 18:30 > HORA_FIN_CENTRO
    (18:00)."""
    prof = _profesional_obj(horario_inicio="09:00", horario_fin="18:00", duracion_min=45)
    fecha = _fecha_habil_futura_obj()

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha, hora_obj=time(17, 45),
        hoy=date.today(), dia_cerrado=None, ocupadas=[time(17, 45)],
    )

    codigos = [c.codigo for c in conflictos]
    assert "excede_cierre_centro" in codigos
    assert "slot_ocupado" in codigos
    slot = next(c for c in conflictos if c.codigo == "slot_ocupado")
    # 1 sola ocupación: el flag propio de slot_ocupado SÍ es True...
    assert slot.overridable_con_sobrecupo is True
    # ...pero como coexiste con excede_cierre_centro (absoluto), el
    # conjunto completo sigue siendo un bloqueo absoluto.
    assert hay_bloqueo_absoluto(conflictos) is True


def test_m_slot_ocupado_mas_dia_cerrado_es_rechazo(db_session):
    """M — slot_ocupado + dia_cerrado (SIEMPRE absoluto) debe seguir
    siendo rechazo total."""
    prof = _profesional_obj()
    fecha = _fecha_habil_futura_obj()
    dia_cerrado = DiaCerrado(fecha=fecha.isoformat(), motivo="Feriado de prueba")

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha, hora_obj=time(9, 0),
        hoy=date.today(), dia_cerrado=dia_cerrado, ocupadas=[time(9, 0)],
    )

    codigos = [c.codigo for c in conflictos]
    assert "dia_cerrado" in codigos
    assert "slot_ocupado" in codigos
    assert hay_bloqueo_absoluto(conflictos) is True


def test_u_slot_ocupado_mas_fecha_pasada_es_rechazo_absoluto(db_session):
    """U — slot_ocupado (1 ocupación, por sí solo overridable) +
    fecha_pasada (SIEMPRE absoluto, A.4.4 no lo toca) debe seguir
    siendo rechazo total, sin importar permiso ni motivo."""
    prof = _profesional_obj()
    fecha_pasada = date.today() - timedelta(days=1)

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha_pasada, hora_obj=time(9, 0),
        hoy=date.today(), dia_cerrado=None, ocupadas=[time(9, 0)],
    )

    codigos = [c.codigo for c in conflictos]
    assert "fecha_pasada" in codigos
    assert "slot_ocupado" in codigos
    slot = next(c for c in conflictos if c.codigo == "slot_ocupado")
    # El flag propio de slot_ocupado sigue siendo True (1 sola
    # ocupación) — lo que hace el rechazo absoluto es fecha_pasada.
    assert slot.overridable_con_sobrecupo is True
    assert hay_bloqueo_absoluto(conflictos) is True


def test_v_solapamiento_real_sin_igualdad_de_hora_es_slot_ocupado(db_session):
    """V — A.4.4 sigue trabajando por INTERVALOS, no por igualdad de
    string/hora: existente 09:00–09:45 (duracion=45), solicitud
    09:30–10:15 — no coinciden en ningún string de hora exacto, pero
    sí se solapan en [09:30,09:45). Debe detectarse slot_ocupado con
    max=1 (overridable=True)."""
    prof = _profesional_obj(duracion_min=45)
    fecha = _fecha_habil_futura_obj()

    conflictos = analizar_conflictos_slot(
        profesional=prof, fecha_obj=fecha, hora_obj=time(9, 30),
        hoy=date.today(), dia_cerrado=None, ocupadas=[time(9, 0)],
    )

    assert [c.codigo for c in conflictos] == ["slot_ocupado"]
    assert conflictos[0].overridable_con_sobrecupo is True
    assert conflictos[0].metadata["ocupacion_maxima_existente"] == 1


# ══════════════════════════════════════════════════════════
# Parte 2 — integración vía citas.crear_cita() (SQLite real)
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


def _dia_habil_futuro(dias_calendario: int = 5) -> str:
    fecha = date.today() + timedelta(days=dias_calendario)
    while fecha.weekday() >= 5:
        fecha += timedelta(days=1)
    return fecha.isoformat()


def _profesional(db, *, horario_inicio="09:00", horario_fin="18:00", duracion_min=30, **kwargs):
    prof = Profesional(
        nombre="Profesional Test A4.4", especialidad="Nutrición", iniciales="PA44",
        estado="activo", horario_inicio=horario_inicio, horario_fin=horario_fin,
        duracion_min=duracion_min, **kwargs,
    )
    db.add(prof)
    db.flush()
    return prof


_contador_usuarios = 0


def _usuario(db, *, rol, correo=None, rut=None):
    """Actor/paciente REAL en BD — nunca un dict con id fabricado. Los
    permisos de SUPERADMIN se resuelven vía has_permission() contra
    ROLE_DEFAULT_PERMISSIONS (agenda.gestionar + agenda.sobrecupo ya
    incluidos ahí, sin bypass por nombre de rol) — ver
    app/rbac/permissions.py."""
    global _contador_usuarios
    _contador_usuarios += 1
    n = _contador_usuarios
    usuario = Usuario(
        correo=correo or f"a44-{rol}-{n}@sesaes.cl",
        password="hash-a44", rol=rol, nombre=f"Usuario A4.4 {n}",
        rut=rut or f"a44-{n}", activo=True,
    )
    db.add(usuario)
    db.flush()
    return usuario


def _current_user(usuario):
    return {"id": usuario.id, "rol": usuario.rol}


def _crear_cita_existente(db, *, profesional_id, hora, fecha, sobrecupo=False):
    paciente = _usuario(db, rol="estudiante")
    cita = Cita(
        estudiante_id=paciente.id, profesional_id=profesional_id,
        fecha=fecha, hora=hora, estado="pendiente", sobrecupo=sobrecupo,
    )
    db.add(cita)
    db.flush()
    return cita


# --- A: 1 cita + solicitud normal -> 409 --------------------------

def test_a_una_cita_existente_mas_solicitud_normal_es_409(db_session):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    db_session.commit()
    _crear_cita_existente(db_session, profesional_id=prof.id, hora="10:00", fecha=fecha)
    db_session.commit()

    paciente = _usuario(db_session, rol="estudiante")
    db_session.commit()

    payload = CitaCreate(
        estudiante_id=paciente.id, profesional_id=prof.id, fecha=fecha, hora="10:00",
    )
    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(paciente))

    assert exc.value.status_code == 409
    assert db_session.query(Cita).count() == 1


# --- B a G: 1 cita + sobrecupo autorizado -> segunda creada --------

def test_b_a_g_sobrecupo_autorizado_sobre_una_cita_existente(db_session):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    db_session.commit()
    original = _crear_cita_existente(db_session, profesional_id=prof.id, hora="10:00", fecha=fecha)
    db_session.commit()

    admin_superadmin = _usuario(db_session, rol="superadmin")
    paciente_nuevo = _usuario(db_session, rol="estudiante")
    db_session.commit()

    payload = CitaCreate(
        estudiante_id=paciente_nuevo.id, profesional_id=prof.id, fecha=fecha, hora="10:00",
        sobrecupo=True, sobrecupo_motivo="  Paciente con indicación urgente del profesional  ",
    )
    citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_superadmin))

    # A — sigue existiendo la original + B — se creó la segunda.
    assert db_session.query(Cita).filter(Cita.profesional_id == prof.id).count() == 2

    nueva = (
        db_session.query(Cita)
        .filter(Cita.estudiante_id == paciente_nuevo.id)
        .one()
    )
    # C — Cita.sobrecupo=True en la nueva; la original NO cambia.
    assert nueva.sobrecupo is True
    original_recargada = db_session.query(Cita).filter(Cita.id == original.id).one()
    assert original_recargada.sobrecupo is False

    # D — CitaSobrecupo creado, asociado a la nueva cita.
    detalle = db_session.query(CitaSobrecupo).filter(CitaSobrecupo.cita_id == nueva.id).one()

    # E — CitaSobrecupoConflicto persistido con codigo="slot_ocupado".
    conflictos_persistidos = (
        db_session.query(CitaSobrecupoConflicto)
        .filter(CitaSobrecupoConflicto.cita_sobrecupo_id == detalle.id)
        .all()
    )
    assert [c.codigo for c in conflictos_persistidos] == ["slot_ocupado"]

    # F — motivo normalizado (trim) persistido exacto.
    assert detalle.motivo == "Paciente con indicación urgente del profesional"

    # G — auditoría creada exactamente una vez, solo por el sobrecupo
    # efectivo (la creación de la cita ORIGINAL, sin sobrecupo, no
    # generó este evento).
    eventos = db_session.query(Auditoria).filter(Auditoria.accion == "Creó cita con sobrecupo").all()
    assert len(eventos) == 1
    assert eventos[0].usuario_id == admin_superadmin.id


# --- H / I: 2 citas existentes + nuevo sobrecupo -> 409 ------------

def test_h_i_capacidad_agotada_rechaza_tercera_cita_sin_dejar_rastro(db_session):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    db_session.commit()
    _crear_cita_existente(db_session, profesional_id=prof.id, hora="11:00", fecha=fecha)
    db_session.commit()
    _crear_cita_existente(db_session, profesional_id=prof.id, hora="11:00", fecha=fecha, sobrecupo=True)
    db_session.commit()

    admin_superadmin = _usuario(db_session, rol="superadmin")
    tercer_paciente = _usuario(db_session, rol="estudiante")
    db_session.commit()

    payload = CitaCreate(
        estudiante_id=tercer_paciente.id, profesional_id=prof.id, fecha=fecha, hora="11:00",
        sobrecupo=True, sobrecupo_motivo="Motivo válido",
    )
    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_superadmin))

    # H — 409, no autorizado pese a sobrecupo + permisos reales + motivo.
    assert exc.value.status_code == 409

    # I — sin rastro de la tercera: ni Cita, ni CitaSobrecupo, ni
    # auditoría de éxito.
    assert db_session.query(Cita).filter(Cita.profesional_id == prof.id).count() == 2
    assert db_session.query(CitaSobrecupo).count() == 0
    assert db_session.query(Auditoria).filter(Auditoria.accion == "Creó cita con sobrecupo").count() == 0


# --- J: slot_ocupado + en_colacion, con capacidad restante ---------

def test_j_slot_ocupado_mas_en_colacion_con_capacidad_ambos_persistidos(db_session):
    fecha = _dia_habil_futuro()
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="17:00",
        hora_almuerzo_inicio="13:00", hora_almuerzo_fin="14:00", duracion_min=30,
    )
    db_session.commit()
    _crear_cita_existente(db_session, profesional_id=prof.id, hora="13:00", fecha=fecha)
    db_session.commit()

    admin_superadmin = _usuario(db_session, rol="superadmin")
    paciente_nuevo = _usuario(db_session, rol="estudiante")
    db_session.commit()

    payload = CitaCreate(
        estudiante_id=paciente_nuevo.id, profesional_id=prof.id, fecha=fecha, hora="13:00",
        sobrecupo=True, sobrecupo_motivo="Motivo válido",
    )
    citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_superadmin))

    nueva = db_session.query(Cita).filter(Cita.estudiante_id == paciente_nuevo.id).one()
    detalle = db_session.query(CitaSobrecupo).filter(CitaSobrecupo.cita_id == nueva.id).one()
    codigos = {
        c.codigo for c in
        db_session.query(CitaSobrecupoConflicto)
        .filter(CitaSobrecupoConflicto.cita_sobrecupo_id == detalle.id)
        .all()
    }
    assert codigos == {"slot_ocupado", "en_colacion"}


# --- K: slot_ocupado + fuera_de_jornada, con capacidad restante ----

def test_k_slot_ocupado_mas_fuera_de_jornada_con_capacidad_ambos_persistidos(db_session):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="16:00", duracion_min=30)
    db_session.commit()
    _crear_cita_existente(db_session, profesional_id=prof.id, hora="16:00", fecha=fecha)
    db_session.commit()

    admin_superadmin = _usuario(db_session, rol="superadmin")
    paciente_nuevo = _usuario(db_session, rol="estudiante")
    db_session.commit()

    payload = CitaCreate(
        estudiante_id=paciente_nuevo.id, profesional_id=prof.id, fecha=fecha, hora="16:00",
        sobrecupo=True, sobrecupo_motivo="Motivo válido",
    )
    citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_superadmin))

    nueva = db_session.query(Cita).filter(Cita.estudiante_id == paciente_nuevo.id).one()
    detalle = db_session.query(CitaSobrecupo).filter(CitaSobrecupo.cita_id == nueva.id).one()
    codigos = {
        c.codigo for c in
        db_session.query(CitaSobrecupoConflicto)
        .filter(CitaSobrecupoConflicto.cita_sobrecupo_id == detalle.id)
        .all()
    }
    assert codigos == {"slot_ocupado", "fuera_de_jornada"}


# --- T: slot_ocupado + fuera_de_jornada + en_colacion, max=1 -------

def test_t_tres_conflictos_superables_con_capacidad_se_autoriza_y_persisten_los_3(db_session):
    """Mismo escenario de horario que
    test_slot_ocupado_mas_fuera_de_jornada_mas_en_colacion en
    test_a4_2_conflictos.py (horario 09:00-13:00, colación
    13:00-14:00, duracion=45 -> a las 12:30 el bloque [12:30,13:15)
    excede jornada Y solapa colación), con exactamente 1 ocupación
    previa: los 3 conflictos son overridable_con_sobrecupo=True, así
    que con permiso + motivo el sobrecupo se autoriza y los 3 códigos
    quedan persistidos."""
    fecha = _dia_habil_futuro()
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="13:00",
        hora_almuerzo_inicio="13:00", hora_almuerzo_fin="14:00", duracion_min=45,
    )
    db_session.commit()
    _crear_cita_existente(db_session, profesional_id=prof.id, hora="12:30", fecha=fecha)
    db_session.commit()

    admin_superadmin = _usuario(db_session, rol="superadmin")
    paciente_nuevo = _usuario(db_session, rol="estudiante")
    db_session.commit()

    payload = CitaCreate(
        estudiante_id=paciente_nuevo.id, profesional_id=prof.id, fecha=fecha, hora="12:30",
        sobrecupo=True, sobrecupo_motivo="Motivo válido",
    )
    citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_superadmin))

    nueva = db_session.query(Cita).filter(Cita.estudiante_id == paciente_nuevo.id).one()
    assert nueva.sobrecupo is True
    detalle = db_session.query(CitaSobrecupo).filter(CitaSobrecupo.cita_id == nueva.id).one()
    codigos = {
        c.codigo for c in
        db_session.query(CitaSobrecupoConflicto)
        .filter(CitaSobrecupoConflicto.cita_sobrecupo_id == detalle.id)
        .all()
    }
    assert codigos == {"slot_ocupado", "fuera_de_jornada", "en_colacion"}


# --- N: flag sobrecupo en slot libre -> cita normal (A.4.1, sin cambios) --

def test_n_flag_sobrecupo_en_slot_libre_crea_cita_normal(db_session):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    db_session.commit()

    admin_superadmin = _usuario(db_session, rol="superadmin")
    paciente = _usuario(db_session, rol="estudiante")
    db_session.commit()

    payload = CitaCreate(
        estudiante_id=paciente.id, profesional_id=prof.id, fecha=fecha, hora="10:00",
        sobrecupo=True, sobrecupo_motivo="Motivo válido",
    )
    citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_superadmin))

    creada = db_session.query(Cita).one()
    assert creada.sobrecupo is False
    assert db_session.query(CitaSobrecupo).count() == 0
    assert db_session.query(Auditoria).filter(Auditoria.accion == "Creó cita con sobrecupo").count() == 0


# --- Q: 1 ocupación + sobrecupo=True + agenda.gestionar SIN agenda.sobrecupo --

def test_q_falta_agenda_sobrecupo_especifico_es_rechazo_controlado(db_session, monkeypatch):
    """A.4.3 preservado: agenda.gestionar y agenda.sobrecupo son
    capacidades DISTINTAS (ver docstring de Permission.AGENDA_SOBRECUPO
    en app/rbac/permissions.py). Actor real SUPERADMIN (que por rol sí
    tendría ambos), pero se aísla la rama F —falta específicamente
    agenda.sobrecupo— parcheando SOLO esa resolución dentro de
    sobrecupo_policy_service, dejando agenda.gestionar intacta (real,
    True). Debe rechazar con el 403 específico de A.4.3
    (CODIGO_DENEGADO_SIN_PERMISO), sin crear segunda cita."""
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    db_session.commit()
    _crear_cita_existente(db_session, profesional_id=prof.id, hora="10:00", fecha=fecha)
    db_session.commit()

    admin_superadmin = _usuario(db_session, rol="superadmin")
    paciente_nuevo = _usuario(db_session, rol="estudiante")
    db_session.commit()

    real_tiene_permiso_efectivo = sobrecupo_policy_service.tiene_permiso_efectivo

    def _sin_agenda_sobrecupo(db, current_user, permiso):
        if permiso == Permission.AGENDA_SOBRECUPO:
            return False
        return real_tiene_permiso_efectivo(db, current_user, permiso)

    monkeypatch.setattr(
        sobrecupo_policy_service, "tiene_permiso_efectivo", _sin_agenda_sobrecupo,
    )

    payload = CitaCreate(
        estudiante_id=paciente_nuevo.id, profesional_id=prof.id, fecha=fecha, hora="10:00",
        sobrecupo=True, sobrecupo_motivo="Motivo válido",
    )
    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_superadmin))

    assert exc.value.status_code == 403
    assert "agenda.sobrecupo" in exc.value.detail
    assert db_session.query(Cita).filter(Cita.profesional_id == prof.id).count() == 1
    assert db_session.query(CitaSobrecupo).count() == 0


# --- R: 1 ocupación + permisos + motivo vacío/None/solo-espacios ----

@pytest.mark.parametrize("motivo_invalido", [None, "", "   "])
def test_r_motivo_invalido_es_rechazo_por_motivo(db_session, motivo_invalido):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    db_session.commit()
    _crear_cita_existente(db_session, profesional_id=prof.id, hora="10:00", fecha=fecha)
    db_session.commit()

    admin_superadmin = _usuario(db_session, rol="superadmin")
    paciente_nuevo = _usuario(db_session, rol="estudiante")
    db_session.commit()

    payload = CitaCreate(
        estudiante_id=paciente_nuevo.id, profesional_id=prof.id, fecha=fecha, hora="10:00",
        sobrecupo=True, sobrecupo_motivo=motivo_invalido,
    )
    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_superadmin))

    assert exc.value.status_code == 400
    assert db_session.query(Cita).filter(Cita.profesional_id == prof.id).count() == 1
    assert db_session.query(CitaSobrecupo).count() == 0


# --- S: SUPERADMIN sin permiso efectivo -> sin bypass por rol -------

def test_s_superadmin_sin_permiso_efectivo_no_tiene_bypass_por_rol(db_session, monkeypatch):
    """Demuestra que NO existe ningún atajo `if rol == "superadmin":
    return True` en ningún punto del camino: se parchea
    tiene_permiso_efectivo para que devuelva SIEMPRE False (para
    CUALQUIER permiso, incluido agenda.gestionar), y se comprueba que
    aun con un actor real cuyo current_user["rol"] == "superadmin" en
    BD, la operación se rechaza exactamente igual que para cualquier
    otro actor sin permisos — el string "superadmin" nunca se lee
    fuera de esa función de resolución."""
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    db_session.commit()
    _crear_cita_existente(db_session, profesional_id=prof.id, hora="10:00", fecha=fecha)
    db_session.commit()

    admin_superadmin = _usuario(db_session, rol="superadmin")
    paciente_nuevo = _usuario(db_session, rol="estudiante")
    db_session.commit()

    llamadas: list[tuple[str, Permission]] = []

    def _siempre_false(db, current_user, permiso):
        llamadas.append((current_user.get("rol"), permiso))
        return False

    monkeypatch.setattr(citas, "tiene_permiso_efectivo", _siempre_false)
    monkeypatch.setattr(sobrecupo_policy_service, "tiene_permiso_efectivo", _siempre_false)

    payload = CitaCreate(
        estudiante_id=paciente_nuevo.id, profesional_id=prof.id, fecha=fecha, hora="10:00",
        sobrecupo=True, sobrecupo_motivo="Motivo válido",
    )
    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_superadmin))

    # Rechazado igual que cualquier actor sin agenda.gestionar — pese
    # a que el rol real en BD es "superadmin".
    assert exc.value.status_code in (403, 409, 400)
    assert db_session.query(Cita).filter(Cita.profesional_id == prof.id).count() == 1
    assert db_session.query(CitaSobrecupo).count() == 0
    # El mock efectivamente se invocó con rol="superadmin" y aun así
    # decidió False — no hubo ningún camino de código que evitara
    # consultarlo por ser superadmin.
    assert any(rol == "superadmin" for rol, _permiso in llamadas)


# ══════════════════════════════════════════════════════════
# Parte 3 — listar_disponibilidad_rango(): regresión de cardinalidad
# (dict[str, set[time]] -> dict[str, list[time]])
# ══════════════════════════════════════════════════════════

def test_rango_una_ocupacion_es_overridable(db_session):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    db_session.commit()
    _crear_cita_existente(db_session, profesional_id=prof.id, hora="10:00", fecha=fecha)
    db_session.commit()

    resultado = listar_disponibilidad_rango(
        db_session, profesional_id=prof.id, fecha_inicio=fecha, fecha_fin=fecha,
    )
    slots = resultado["dias"][0]["slots"]
    slot = next(s for s in slots if s["hora"] == "10:00")
    assert slot["disponible"] is False
    assert slot["motivo"] == "slot_ocupado"
    assert slot["overridable_con_sobrecupo"] is True


def test_rango_dos_ocupaciones_no_es_overridable(db_session):
    """Regresión específica de cardinalidad: dos citas activas a la
    MISMA hora en ocupadas_por_fecha — si el dict interno todavía
    usara set[time] en vez de list[time], esto colapsaría a 1
    ocupación y el slot aparecería incorrectamente como overridable."""
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    db_session.commit()
    _crear_cita_existente(db_session, profesional_id=prof.id, hora="10:00", fecha=fecha)
    db_session.commit()
    _crear_cita_existente(db_session, profesional_id=prof.id, hora="10:00", fecha=fecha, sobrecupo=True)
    db_session.commit()

    resultado = listar_disponibilidad_rango(
        db_session, profesional_id=prof.id, fecha_inicio=fecha, fecha_fin=fecha,
    )
    slots = resultado["dias"][0]["slots"]
    slot = next(s for s in slots if s["hora"] == "10:00")
    assert slot["disponible"] is False
    assert slot["motivo"] == "slot_ocupado"
    assert slot["overridable_con_sobrecupo"] is False
