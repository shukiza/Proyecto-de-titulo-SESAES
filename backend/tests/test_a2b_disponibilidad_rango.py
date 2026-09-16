"""
SESAES — A.2B: disponibilidad de rango (Agenda Admin → Semana), servicio.

Cubre app.services.agenda_disponibilidad_service.listar_disponibilidad_rango
de forma aislada, con una base SQLite en memoria real (mismo patrón que
test_a2_disponibilidad_real.py).

No se tocan aquí los tests de RBAC/scope del endpoint — esos viven en
test_a2b_disponibilidad_rango_rbac.py, con FakeDB, igual que el resto de
fase 3.5F/SA-9. Este archivo es adicional, no reemplaza cobertura de
evaluar_disponibilidad_slot()/listar_horas_disponibles() ya cubierta en
test_a2_disponibilidad_real.py.

Fechas de test: igual que en A.2A, se usan helpers relativos a
date.today() en vez de fechas fijas de calendario, para que la suite no
dependa de cuándo se ejecute.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

# Registrar modelos/relaciones antes de create_all.
import app.models.init  # noqa: F401
import app.models.solicitud_horario  # noqa: F401

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.cita import Cita
from app.models.dia_cerrado import DiaCerrado
from app.models.profesional import Profesional
from app.models.usuario import Usuario
from app.rbac.admin_authorization import AlcanceAdministrativoEfectivo
from app.services.agenda_disponibilidad_service import (
    MAX_DIAS_RANGO_DISPONIBILIDAD,
    ParametrosRangoInvalidosError,
    ProfesionalNoEncontradoError,
    evaluar_disponibilidad_slot,
    listar_disponibilidad_rango,
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


def _proximo_lunes_futuro(dias_minimos: int = 1) -> date:
    """
    Próximo lunes que sea al menos `dias_minimos` días calendario en el
    futuro respecto a hoy (nunca hoy mismo). Determinístico respecto de
    la fecha real de ejecución.
    """
    fecha = date.today() + timedelta(days=dias_minimos)
    while fecha.weekday() != 0:
        fecha += timedelta(days=1)
    return fecha


def _dia_habil_futuro(dias_calendario: int = 1) -> date:
    fecha = date.today() + timedelta(days=dias_calendario)
    while fecha.weekday() >= 5:
        fecha += timedelta(days=1)
    return fecha


def _fecha_futura_habil_dia_no_padded(desde: date | None = None) -> date:
    """
    Día hábil futuro cuyo día del mes es < 10 (un solo dígito), para
    poder construir una fecha de entrada "no zero-padded" (p. ej.
    "2026-9-7") que sea sintácticamente distinta de su forma canónica
    ("2026-09-07") sin depender de qué fecha sea "hoy" al ejecutar la
    suite. Búsqueda acotada: en <= 60 días siempre hay al menos un día
    hábil con día-de-mes de un dígito. `desde`, si se entrega, permite
    encadenar llamadas para obtener fechas distintas entre sí dentro
    de un mismo test (por defecto busca desde mañana).
    """
    fecha = (desde or date.today()) + timedelta(days=1)
    for _ in range(60):
        if fecha.weekday() < 5 and fecha.day < 10:
            return fecha
        fecha += timedelta(days=1)
    raise AssertionError("no se encontró un día hábil con día de mes < 10 en 60 días")


def _profesional(
    db,
    *,
    especialidad="Nutrición",
    estado="activo",
    horario_inicio="09:00",
    horario_fin="13:00",
    hora_almuerzo_inicio=None,
    hora_almuerzo_fin=None,
    duracion_min=30,
):
    prof = Profesional(
        nombre="Profesional Test",
        especialidad=especialidad,
        iniciales="PT",
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


def _estudiante(db, *, correo="est-a2b@sesaes.cl", rut="22.222.222-2"):
    usuario = Usuario(
        correo=correo,
        password="hash-a2b",
        rol="estudiante",
        nombre="Estudiante Test A2B",
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


# ──────────────────────────────────────────────────────────
# Validación de parámetros del rango
# ──────────────────────────────────────────────────────────

def test_rango_rechaza_fecha_fin_anterior_a_fecha_inicio(db_session):
    prof = _profesional(db_session)
    db_session.commit()

    inicio = _dia_habil_futuro(5).isoformat()
    fin = _dia_habil_futuro(1).isoformat()

    with pytest.raises(ParametrosRangoInvalidosError) as exc:
        listar_disponibilidad_rango(
            db_session, profesional_id=prof.id, fecha_inicio=inicio, fecha_fin=fin,
        )
    assert exc.value.motivo == "rango_invertido"


def test_rango_rechaza_fecha_inicio_con_formato_invalido(db_session):
    prof = _profesional(db_session)
    db_session.commit()

    with pytest.raises(ParametrosRangoInvalidosError) as exc:
        listar_disponibilidad_rango(
            db_session,
            profesional_id=prof.id,
            fecha_inicio="07-09-2026",
            fecha_fin=_dia_habil_futuro(5).isoformat(),
        )
    assert exc.value.motivo == "fecha_inicio_invalida"


def test_rango_rechaza_fecha_fin_con_formato_invalido(db_session):
    prof = _profesional(db_session)
    db_session.commit()

    with pytest.raises(ParametrosRangoInvalidosError) as exc:
        listar_disponibilidad_rango(
            db_session,
            profesional_id=prof.id,
            fecha_inicio=_dia_habil_futuro(1).isoformat(),
            fecha_fin="no-es-una-fecha",
        )
    assert exc.value.motivo == "fecha_fin_invalida"


def test_rango_rechaza_mas_de_31_dias(db_session):
    prof = _profesional(db_session)
    db_session.commit()

    inicio = _dia_habil_futuro(1)
    fin = inicio + timedelta(days=MAX_DIAS_RANGO_DISPONIBILIDAD)  # 32 días inclusive

    with pytest.raises(ParametrosRangoInvalidosError) as exc:
        listar_disponibilidad_rango(
            db_session,
            profesional_id=prof.id,
            fecha_inicio=inicio.isoformat(),
            fecha_fin=fin.isoformat(),
        )
    assert exc.value.motivo == "rango_excede_maximo"


def test_rango_acepta_exactamente_el_maximo_de_dias(db_session):
    prof = _profesional(db_session)
    db_session.commit()

    inicio = _dia_habil_futuro(1)
    fin = inicio + timedelta(days=MAX_DIAS_RANGO_DISPONIBILIDAD - 1)  # inclusive => 31 días

    resultado = listar_disponibilidad_rango(
        db_session,
        profesional_id=prof.id,
        fecha_inicio=inicio.isoformat(),
        fecha_fin=fin.isoformat(),
    )
    assert len(resultado["dias"]) == MAX_DIAS_RANGO_DISPONIBILIDAD


def test_rango_profesional_no_encontrado(db_session):
    with pytest.raises(ProfesionalNoEncontradoError):
        listar_disponibilidad_rango(
            db_session,
            profesional_id=99999,
            fecha_inicio=_dia_habil_futuro(1).isoformat(),
            fecha_fin=_dia_habil_futuro(2).isoformat(),
        )


# ──────────────────────────────────────────────────────────
# Contrato de respuesta
# ──────────────────────────────────────────────────────────

def test_rango_contrato_shape_exacto(db_session):
    prof = _profesional(db_session, duracion_min=45)
    db_session.commit()

    inicio = _dia_habil_futuro(1)
    fin = inicio + timedelta(days=1)

    resultado = listar_disponibilidad_rango(
        db_session,
        profesional_id=prof.id,
        fecha_inicio=inicio.isoformat(),
        fecha_fin=fin.isoformat(),
    )

    assert set(resultado.keys()) == {
        "profesional_id", "fecha_inicio", "fecha_fin", "duracion_min", "dias",
    }
    assert resultado["profesional_id"] == prof.id
    assert resultado["fecha_inicio"] == inicio.isoformat()
    assert resultado["fecha_fin"] == fin.isoformat()
    assert resultado["duracion_min"] == 45

    for dia in resultado["dias"]:
        assert set(dia.keys()) == {"fecha", "slots"}
        for slot in dia["slots"]:
            assert set(slot.keys()) == {
                "hora", "disponible", "motivo", "overridable_con_sobrecupo",
            }
            # Nunca datos clínicos ni de paciente en este contrato.
            assert "estudiante" not in slot
            assert "medicamento" not in slot


def test_rango_dias_en_orden_y_cubre_todo_el_rango(db_session):
    prof = _profesional(db_session)
    db_session.commit()

    inicio = _dia_habil_futuro(1)
    fin = inicio + timedelta(days=4)

    resultado = listar_disponibilidad_rango(
        db_session,
        profesional_id=prof.id,
        fecha_inicio=inicio.isoformat(),
        fecha_fin=fin.isoformat(),
    )

    fechas_esperadas = [
        (inicio + timedelta(days=i)).isoformat() for i in range(5)
    ]
    assert [d["fecha"] for d in resultado["dias"]] == fechas_esperadas


# ──────────────────────────────────────────────────────────
# Reglas de disponibilidad dentro del rango
# ──────────────────────────────────────────────────────────

def test_rango_marca_sabado_y_domingo_como_no_disponibles(db_session):
    prof = _profesional(db_session)
    db_session.commit()

    lunes = _proximo_lunes_futuro(1)
    domingo = lunes + timedelta(days=6)

    resultado = listar_disponibilidad_rango(
        db_session,
        profesional_id=prof.id,
        fecha_inicio=lunes.isoformat(),
        fecha_fin=domingo.isoformat(),
    )

    dias_por_fecha = {d["fecha"]: d for d in resultado["dias"]}
    sabado = (lunes + timedelta(days=5)).isoformat()

    for fecha_finde in (sabado, domingo.isoformat()):
        slots = dias_por_fecha[fecha_finde]["slots"]
        assert slots, "la grilla debe generarse igual aunque el día no atienda"
        for slot in slots:
            assert slot["disponible"] is False
            assert slot["motivo"] == "fin_de_semana"
            assert slot["overridable_con_sobrecupo"] is False


def test_rango_respeta_dia_cerrado(db_session):
    prof = _profesional(db_session)
    fecha_cerrada = _dia_habil_futuro(2)
    db_session.add(DiaCerrado(fecha=fecha_cerrada.isoformat(), motivo="Capacitación interna"))
    db_session.commit()

    inicio = _dia_habil_futuro(1)
    fin = _dia_habil_futuro(3)

    resultado = listar_disponibilidad_rango(
        db_session,
        profesional_id=prof.id,
        fecha_inicio=inicio.isoformat(),
        fecha_fin=fin.isoformat(),
    )

    dias_por_fecha = {d["fecha"]: d for d in resultado["dias"]}
    slots_dia_cerrado = dias_por_fecha[fecha_cerrada.isoformat()]["slots"]
    assert slots_dia_cerrado
    for slot in slots_dia_cerrado:
        assert slot["disponible"] is False
        assert slot["motivo"] == "dia_cerrado"
        assert slot["overridable_con_sobrecupo"] is False


def test_rango_profesional_inactivo_todos_los_slots_no_disponibles(db_session):
    prof = _profesional(db_session, estado="licencia")
    db_session.commit()

    inicio = _dia_habil_futuro(1)
    fin = _dia_habil_futuro(2)

    resultado = listar_disponibilidad_rango(
        db_session,
        profesional_id=prof.id,
        fecha_inicio=inicio.isoformat(),
        fecha_fin=fin.isoformat(),
    )

    for dia in resultado["dias"]:
        for slot in dia["slots"]:
            assert slot["disponible"] is False
            assert slot["motivo"] == "profesional_inactivo"
            assert slot["overridable_con_sobrecupo"] is False


def test_evaluar_disponibilidad_slot_conserva_precedencia_profesional_inactivo_de_a2a(db_session):
    """
    Regresión puntual del ajuste "conservar precedencia
    profesional_inactivo de A.2A": profesional_inactivo debe evaluarse
    ANTES que fecha_invalida/hora_invalida en evaluar_disponibilidad_slot
    (el contrato público de A.2A no cambió), aunque internamente ahora
    comparta núcleo con listar_disponibilidad_rango(). Si el orden se
    invirtiera, este caso (profesional inactivo + fecha con formato
    inválido) devolvería "fecha_invalida" en vez de
    "profesional_inactivo".
    """
    prof = _profesional(db_session, estado="licencia")
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session,
        profesional_id=prof.id,
        fecha="07-09-2026",  # formato inválido a propósito
        hora="09:00",
    )
    assert resultado.motivo == "profesional_inactivo"
    assert resultado.disponible is False


def test_rango_slot_disponible_dentro_de_jornada_sin_ocupar(db_session):
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="13:00", duracion_min=30,
    )
    db_session.commit()

    fecha = _dia_habil_futuro(3)
    resultado = listar_disponibilidad_rango(
        db_session,
        profesional_id=prof.id,
        fecha_inicio=fecha.isoformat(),
        fecha_fin=fecha.isoformat(),
    )

    slots = resultado["dias"][0]["slots"]
    slot_0930 = next(s for s in slots if s["hora"] == "09:30")
    assert slot_0930["disponible"] is True
    assert slot_0930["motivo"] is None
    assert slot_0930["overridable_con_sobrecupo"] is False


def test_rango_slot_ocupado_fuera_de_jornada_conserva_prioridad_slot_ocupado(db_session):
    """
    Mismo caso que A.2A corrección v4 (test_slot_ocupado_fuera_de_jornada_
    es_overridable_con_capacidad_disponible en test_a2_disponibilidad_real.py),
    pero pasando por listar_disponibilidad_rango: un slot fuera de
    jornada (normalmente overridable) que además está ocupado por una
    cita real debe seguir devolviendo "slot_ocupado" (motivo con mayor
    precedencia), nunca "fuera_de_jornada" — A.4.4: con 1 sola
    ocupación, overridable_con_sobrecupo es True (capacidad
    disponible), no False.
    """
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="13:00", duracion_min=30,
    )
    est = _estudiante(db_session)
    db_session.commit()

    fecha = _dia_habil_futuro(3)
    _cita(db_session, estudiante_id=est.id, profesional_id=prof.id, fecha=fecha.isoformat(), hora="13:30")
    db_session.commit()

    resultado = listar_disponibilidad_rango(
        db_session,
        profesional_id=prof.id,
        fecha_inicio=fecha.isoformat(),
        fecha_fin=fecha.isoformat(),
    )

    slots = resultado["dias"][0]["slots"]
    slot_ocupado = next(s for s in slots if s["hora"] == "13:30")
    assert slot_ocupado["disponible"] is False
    assert slot_ocupado["motivo"] == "slot_ocupado"
    assert slot_ocupado["overridable_con_sobrecupo"] is True


def test_rango_slot_en_colacion_es_overridable(db_session):
    prof = _profesional(
        db_session,
        horario_inicio="09:00",
        horario_fin="17:00",
        hora_almuerzo_inicio="12:00",
        hora_almuerzo_fin="13:00",
        duracion_min=30,
    )
    db_session.commit()

    fecha = _dia_habil_futuro(3)
    resultado = listar_disponibilidad_rango(
        db_session,
        profesional_id=prof.id,
        fecha_inicio=fecha.isoformat(),
        fecha_fin=fecha.isoformat(),
    )

    slots = resultado["dias"][0]["slots"]
    slot_colacion = next(s for s in slots if s["hora"] == "12:00")
    assert slot_colacion["disponible"] is False
    assert slot_colacion["motivo"] == "en_colacion"
    assert slot_colacion["overridable_con_sobrecupo"] is True


def test_rango_duracion_min_por_defecto_si_profesional_no_tiene_valor_usable(db_session):
    prof = _profesional(db_session, duracion_min=0)
    db_session.commit()

    fecha = _dia_habil_futuro(1)
    resultado = listar_disponibilidad_rango(
        db_session,
        profesional_id=prof.id,
        fecha_inicio=fecha.isoformat(),
        fecha_fin=fecha.isoformat(),
    )
    assert resultado["duracion_min"] == 45  # DURACION_MIN_POR_DEFECTO


def test_rango_duracion_min_negativa_tambien_usa_defecto_y_es_consistente_con_la_grilla(db_session):
    """
    Antes de esta corrección, `duracion_min or DURACION_MIN_POR_DEFECTO`
    dejaba pasar un valor negativo tal cual (-10 es "truthy"), así que
    la grilla se construía con 45 (generar_bloques_jornada lo neutraliza
    puertas adentro) pero el campo "duracion_min" de la respuesta
    reportaba -10: dos números distintos para la misma grilla. Ahora
    ambos vienen de _duracion_efectiva().
    """
    prof = _profesional(db_session, duracion_min=-10, horario_inicio="09:00", horario_fin="13:00")
    db_session.commit()

    fecha = _dia_habil_futuro(1)
    resultado = listar_disponibilidad_rango(
        db_session,
        profesional_id=prof.id,
        fecha_inicio=fecha.isoformat(),
        fecha_fin=fecha.isoformat(),
    )
    assert resultado["duracion_min"] == 45
    horas = [s["hora"] for s in resultado["dias"][0]["slots"]]
    # La grilla nace de HORA_INICIO_CENTRO (08:00) en pasos de 45 min
    # (el valor efectivo reportado), no de -10 ni de 30 (default de
    # otro test): 08:00, 08:45, 09:30, ...
    assert "08:00" in horas and "08:45" in horas and "09:30" in horas


def test_rango_canonicaliza_fechas_no_zero_padded_en_la_respuesta(db_session):
    """
    _parsear_fecha() usa strptime("%Y-%m-%d"), que acepta variantes sin
    cero a la izquierda ("2026-9-7"). El contrato exige fechas
    canónicas, así que fecha_inicio/fecha_fin en la respuesta deben
    normalizarse a partir del date ya parseado (.isoformat()), no
    devolver el string de entrada tal cual.
    """
    prof = _profesional(db_session)
    db_session.commit()

    fecha = _fecha_futura_habil_dia_no_padded()
    fecha_no_padded = f"{fecha.year}-{fecha.month}-{fecha.day}"
    assert fecha_no_padded != fecha.isoformat()

    resultado = listar_disponibilidad_rango(
        db_session,
        profesional_id=prof.id,
        fecha_inicio=fecha_no_padded,
        fecha_fin=fecha_no_padded,
    )

    assert resultado["fecha_inicio"] == fecha.isoformat()
    assert resultado["fecha_fin"] == fecha.isoformat()
    assert resultado["dias"][0]["fecha"] == fecha.isoformat()


def test_rango_canonicaliza_fecha_no_padded_al_consultar_dia_cerrado_real(db_session):
    """
    Canonicalización REAL de consulta (corrección v6, punto 1), no solo
    de la respuesta: se guarda un DiaCerrado con fecha canónica
    ("2026-09-07") y se pide el rango con una entrada no zero-padded
    equivalente ("2026-9-7"). Si la query siguiera comparando contra el
    string crudo de entrada, este DiaCerrado real nunca se
    encontraría (DiaCerrado.fecha == "2026-9-7" no matchea
    "2026-09-07"), y el slot aparecería disponible en vez de
    dia_cerrado.
    """
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="13:00")
    fecha = _fecha_futura_habil_dia_no_padded()
    db_session.add(DiaCerrado(fecha=fecha.isoformat(), motivo="Capacitación"))
    db_session.commit()

    fecha_no_padded = f"{fecha.year}-{fecha.month}-{fecha.day}"
    assert fecha_no_padded != fecha.isoformat()

    resultado = listar_disponibilidad_rango(
        db_session,
        profesional_id=prof.id,
        fecha_inicio=fecha_no_padded,
        fecha_fin=fecha_no_padded,
    )

    slots = resultado["dias"][0]["slots"]
    assert slots
    for slot in slots:
        assert slot["disponible"] is False
        assert slot["motivo"] == "dia_cerrado"


def test_rango_canonicaliza_fecha_no_padded_al_consultar_cita_ocupada_real(db_session):
    """
    Mismo caso que el anterior pero para Cita: se guarda una cita real
    en fecha canónica y se pide el rango con entrada no zero-padded
    equivalente. El slot correspondiente debe verse "slot_ocupado" de
    verdad (no solo que el contrato eco'ee la fecha bonita).
    """
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="13:00", duracion_min=30)
    est = _estudiante(db_session)
    fecha = _fecha_futura_habil_dia_no_padded()
    db_session.commit()

    _cita(db_session, estudiante_id=est.id, profesional_id=prof.id, fecha=fecha.isoformat(), hora="09:30")
    db_session.commit()

    fecha_no_padded = f"{fecha.year}-{fecha.month}-{fecha.day}"
    assert fecha_no_padded != fecha.isoformat()

    resultado = listar_disponibilidad_rango(
        db_session,
        profesional_id=prof.id,
        fecha_inicio=fecha_no_padded,
        fecha_fin=fecha_no_padded,
    )

    slots = resultado["dias"][0]["slots"]
    slot_930 = next(s for s in slots if s["hora"] == "09:30")
    assert slot_930["disponible"] is False
    assert slot_930["motivo"] == "slot_ocupado"


def test_evaluar_disponibilidad_slot_canonicaliza_fecha_no_padded_contra_dia_cerrado_y_cita_reales(db_session):
    """
    Mismo caso, pero para evaluar_disponibilidad_slot() puntual: BD con
    fecha canónica + entrada no zero-padded → debe encontrar el
    DiaCerrado/la Cita real igual, no fallar la comparación por
    formato.
    """
    prof_a = _profesional(db_session, horario_inicio="09:00", horario_fin="13:00")
    fecha_cerrada = _fecha_futura_habil_dia_no_padded()
    db_session.add(DiaCerrado(fecha=fecha_cerrada.isoformat(), motivo="Feriado"))
    db_session.commit()

    resultado_cerrado = evaluar_disponibilidad_slot(
        db_session,
        profesional_id=prof_a.id,
        fecha=f"{fecha_cerrada.year}-{fecha_cerrada.month}-{fecha_cerrada.day}",
        hora="09:00",
    )
    assert resultado_cerrado.motivo == "dia_cerrado"

    prof_b = _profesional(db_session, horario_inicio="09:00", horario_fin="13:00", duracion_min=30)
    est = _estudiante(db_session, correo="est-a2b-2@sesaes.cl", rut="33.333.333-3")
    fecha_ocupada = _fecha_futura_habil_dia_no_padded(desde=fecha_cerrada)
    db_session.commit()
    _cita(db_session, estudiante_id=est.id, profesional_id=prof_b.id, fecha=fecha_ocupada.isoformat(), hora="09:30")
    db_session.commit()

    resultado_ocupado = evaluar_disponibilidad_slot(
        db_session,
        profesional_id=prof_b.id,
        fecha=f"{fecha_ocupada.year}-{fecha_ocupada.month}-{fecha_ocupada.day}",
        hora="09:30",
    )
    assert resultado_ocupado.motivo == "slot_ocupado"


# ──────────────────────────────────────────────────────────
# Consistencia con evaluar_disponibilidad_slot() — no dos algoritmos
# ──────────────────────────────────────────────────────────

def test_rango_coincide_exactamente_con_evaluacion_puntual_para_cada_slot(db_session):
    """
    Para el mismo profesional/fecha/hora, listar_disponibilidad_rango()
    y evaluar_disponibilidad_slot() deben devolver siempre el mismo
    resultado (disponible, motivo, overridable) — ambos caminos comparten
    el mismo núcleo (_evaluar_slot_en_contexto), así que no pueden
    divergir. Se arma un escenario con variedad: días laborales, fin de
    semana, día cerrado, cita ocupando un slot fuera de jornada.
    """
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="13:00", duracion_min=30,
    )
    est = _estudiante(db_session)
    lunes = _proximo_lunes_futuro(1)
    fecha_cerrada = lunes + timedelta(days=2)  # miércoles de esa semana
    db_session.add(DiaCerrado(fecha=fecha_cerrada.isoformat(), motivo="Feriado de prueba"))
    db_session.commit()

    _cita(
        db_session,
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=lunes.isoformat(),
        hora="13:30",  # fuera de jornada, ocupado -> debe primar slot_ocupado
    )
    db_session.commit()

    domingo = lunes + timedelta(days=6)
    resultado = listar_disponibilidad_rango(
        db_session,
        profesional_id=prof.id,
        fecha_inicio=lunes.isoformat(),
        fecha_fin=domingo.isoformat(),
    )

    for dia in resultado["dias"]:
        for slot in dia["slots"]:
            puntual = evaluar_disponibilidad_slot(
                db_session,
                profesional_id=prof.id,
                fecha=dia["fecha"],
                hora=slot["hora"],
            )
            assert slot["disponible"] == puntual.disponible, (dia["fecha"], slot["hora"])
            assert slot["motivo"] == puntual.motivo, (dia["fecha"], slot["hora"])
            assert (
                slot["overridable_con_sobrecupo"] == puntual.overridable_con_sobrecupo
            ), (dia["fecha"], slot["hora"])


# ──────────────────────────────────────────────────────────
# Integración real router + servicio (sin stubs) — DB SQLite real.
#
# Los tests de test_a2b_disponibilidad_rango_rbac.py usan FakeDB y
# monkeypatchean listar_disponibilidad_rango para aislar RBAC/scope.
# Estos de acá llaman al router REAL (app.routers.agenda) contra una
# base real, sin reemplazar el servicio por un stub, para confirmar
# que el cableado router->servicio funciona de punta a punta (única
# pieza monkeypatcheada: obtener_alcance_administrativo_efectivo, que
# requiere contexto RBAC completo ajeno a A.2B para resolverse).
# ──────────────────────────────────────────────────────────

def test_endpoint_real_integra_router_y_servicio_sin_stub(db_session, monkeypatch):
    from app.routers import agenda as agenda_router

    prof = _profesional(
        db_session, especialidad="Nutrición",
        horario_inicio="09:00", horario_fin="13:00", duracion_min=30,
    )
    db_session.commit()

    monkeypatch.setattr(
        agenda_router, "obtener_alcance_administrativo_efectivo",
        lambda db, current_user: AlcanceAdministrativoEfectivo(
            institucional=True, especialidades_normalizadas=frozenset(),
        ),
    )

    fecha = _dia_habil_futuro(1)
    resultado = agenda_router.get_disponibilidad_rango_admin(
        profesional_id=prof.id,
        fecha_inicio=fecha.isoformat(),
        fecha_fin=fecha.isoformat(),
        db=db_session,
        current_user={"id": 1, "rol": "superadmin"},
    )

    assert resultado["profesional_id"] == prof.id
    assert resultado["fecha_inicio"] == fecha.isoformat()
    assert resultado["duracion_min"] == 30
    assert len(resultado["dias"]) == 1
    assert resultado["dias"][0]["fecha"] == fecha.isoformat()
    assert len(resultado["dias"][0]["slots"]) > 0


def test_endpoint_real_403_fuera_de_alcance_sin_stub(db_session, monkeypatch):
    from fastapi import HTTPException

    from app.routers import agenda as agenda_router

    prof = _profesional(db_session, especialidad="Odontología")
    db_session.commit()

    monkeypatch.setattr(
        agenda_router, "obtener_alcance_administrativo_efectivo",
        lambda db, current_user: AlcanceAdministrativoEfectivo(
            institucional=False,
            especialidades_normalizadas=frozenset({"nutricion"}),
        ),
    )

    with pytest.raises(HTTPException) as exc:
        agenda_router.get_disponibilidad_rango_admin(
            profesional_id=prof.id,
            fecha_inicio=_dia_habil_futuro(1).isoformat(),
            fecha_fin=_dia_habil_futuro(2).isoformat(),
            db=db_session,
            current_user={"id": 50, "rol": "admin"},
        )
    assert exc.value.status_code == 403


def test_endpoint_real_400_rango_invertido_sin_stub(db_session, monkeypatch):
    from fastapi import HTTPException

    from app.routers import agenda as agenda_router

    prof = _profesional(db_session, especialidad="Nutrición")
    db_session.commit()

    monkeypatch.setattr(
        agenda_router, "obtener_alcance_administrativo_efectivo",
        lambda db, current_user: AlcanceAdministrativoEfectivo(
            institucional=True, especialidades_normalizadas=frozenset(),
        ),
    )

    with pytest.raises(HTTPException) as exc:
        agenda_router.get_disponibilidad_rango_admin(
            profesional_id=prof.id,
            fecha_inicio=_dia_habil_futuro(5).isoformat(),
            fecha_fin=_dia_habil_futuro(1).isoformat(),
            db=db_session,
            current_user={"id": 1, "rol": "superadmin"},
        )
    assert exc.value.status_code == 400
    # `motivo` (interno, "rango_invertido") no viaja en la respuesta —
    # solo el mensaje humano (corrección v6, punto 4).
    assert "motivo" not in (exc.value.detail or "")
    assert exc.value.detail == "fecha_fin no puede ser anterior a fecha_inicio."


def test_endpoint_real_404_profesional_inexistente_sin_stub(db_session, monkeypatch):
    from fastapi import HTTPException

    from app.routers import agenda as agenda_router

    monkeypatch.setattr(
        agenda_router, "obtener_alcance_administrativo_efectivo",
        lambda db, current_user: AlcanceAdministrativoEfectivo(
            institucional=True, especialidades_normalizadas=frozenset(),
        ),
    )

    with pytest.raises(HTTPException) as exc:
        agenda_router.get_disponibilidad_rango_admin(
            profesional_id=999999,
            fecha_inicio=_dia_habil_futuro(1).isoformat(),
            fecha_fin=_dia_habil_futuro(2).isoformat(),
            db=db_session,
            current_user={"id": 1, "rol": "superadmin"},
        )
    assert exc.value.status_code == 404
