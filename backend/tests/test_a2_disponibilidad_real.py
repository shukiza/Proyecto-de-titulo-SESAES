"""
SESAES — A.2: disponibilidad real unificada (correcciones acumuladas
tras revisión, hasta v4).

Cubre:
  - evaluar_disponibilidad_slot() / listar_horas_disponibles()
    (app.services.agenda_disponibilidad_service) de forma aislada,
    con una base SQLite en memoria real (mismo patrón que
    test_sa10_2_citas_lectura.py).
  - Alineación real a la grilla de bloques según duracion_min (v2,
    corrección punto 1): una hora dentro de jornada pero que no
    corresponde a un bloque real debe rechazarse.
  - Reglas estructurales de fecha/hora (fecha pasada, hora pasada hoy,
    fin de semana) vs. política de agendamiento de Estudiante (ventana
    de 7 días) — v2, corrección punto 2.
  - Comparación de hora normalizada (24h vs "HH:MM AM/PM") tanto en
    evaluar_disponibilidad_slot() como en listar_horas_disponibles()
    — v2, corrección punto 3.
  - Orden grilla-antes-que-jornada, para que un valor no alineado no se
    confunda con "fuera de jornada" overridable — v3.
  - Orden ocupación-antes-que-jornada/colación, para que un slot ya
    ocupado sea siempre un bloqueo absoluto aunque además esté fuera de
    jornada o en colación — v4.
  - POST /citas (citas.crear_cita) ahora rechazando slots fuera de
    disponibilidad real, y permitiendo sobrecupo solo para los
    motivos "overridable".
  - POST /admin/citas/urgente sigue sin exigir disponibilidad
    ordinaria (decisión explícita de A.2, no un efecto colateral).

No se tocan aquí los tests de seguridad/scope ya existentes — este
archivo es adicional, no reemplaza cobertura.

Fechas de test: en vez de fechas fijas de calendario (frágiles si el
"hoy" de ejecución cambia y la fecha fija termina cayendo antes de hoy,
o `date.today() + timedelta(days=1)` cae justo en fin de semana), se
usa `_dia_habil_futuro()` para obtener siempre un día hábil futuro
determinístico relativo al momento real de ejecución.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Registrar modelos/relaciones antes de create_all.
import app.models.init  # noqa: F401
import app.models.solicitud_horario  # noqa: F401

from app.database import Base
from app.models.cita import Cita
from app.models.dia_cerrado import DiaCerrado
from app.models.profesional import Profesional
from app.models.usuario import Usuario
from app.rbac.admin_authorization import AlcanceAdministrativoEfectivo
from app.rbac.permissions import Permission
from app.routers import admin, citas
from app.schemas import CitaCreate
import app.services.agenda_disponibilidad_service as agenda_disponibilidad_service
import app.services.sobrecupo_policy_service as sobrecupo_policy_service
from app.services.agenda_disponibilidad_service import (
    VENTANA_AGENDAMIENTO_ESTUDIANTE_DIAS,
    evaluar_disponibilidad_slot,
    excede_ventana_agendamiento_estudiante,
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
    """
    Fecha (YYYY-MM-DD) `dias_calendario` días calendario en el futuro
    respecto de hoy, empujada al siguiente día hábil (lunes-viernes) si
    cae en fin de semana. Determinístico respecto de la fecha real de
    ejecución — evita el problema de una fecha fija de calendario que
    puede volverse pasada, o de `date.today() + timedelta(days=1)`
    cayendo justo sábado/domingo (ver corrección v2, punto 5).
    """
    fecha = date.today() + timedelta(days=dias_calendario)
    while fecha.weekday() >= 5:
        fecha += timedelta(days=1)
    return fecha.isoformat()


def _proximo_fin_de_semana() -> str:
    """Próximo sábado o domingo (YYYY-MM-DD) desde hoy, para probar la
    regla estructural de fin de semana de forma determinística."""
    fecha = date.today() + timedelta(days=1)
    while fecha.weekday() < 5:
        fecha += timedelta(days=1)
    return fecha.isoformat()


def _fecha_fuera_de_ventana_estudiante() -> str:
    """Día hábil más allá de la ventana de agendamiento de Estudiante
    (VENTANA_AGENDAMIENTO_ESTUDIANTE_DIAS), para probar que esa
    política se aplica/no se aplica según quién agenda."""
    return _dia_habil_futuro(VENTANA_AGENDAMIENTO_ESTUDIANTE_DIAS + 3)


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


def _estudiante(db, *, correo="est@sesaes.cl", rut="11.111.111-1"):
    usuario = Usuario(
        correo=correo,
        password="hash-a2",
        rol="estudiante",
        nombre="Estudiante Test",
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


def _current_user_estudiante(est):
    return {"id": est.id, "rol": "estudiante"}


def _monkeypatch_admin_institucional(
    monkeypatch, *modulos, gestionar: bool = True, sobrecupo: bool = True,
):
    """
    A.4.3 — generalizado para poder distinguir explícitamente
    agenda.gestionar de agenda.sobrecupo (la política central de
    sobrecupo resuelve ambos permisos por su cuenta, en su propio
    namespace — patchear solo `citas.tiene_permiso_efectivo` ya no
    alcanza para los tests que crean un sobrecupo efectivo). Nunca
    devuelve True para CUALQUIER permiso: solo resuelve estos dos
    explícitamente, y falla ruidosamente (AssertionError) si algún
    código bajo prueba llegara a consultar un tercer permiso que este
    helper no contempla — mejor eso que un mock demasiado permisivo
    ocultando una regresión.
    """
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
# evaluar_disponibilidad_slot() — caso válido
# ──────────────────────────────────────────────────────────

def test_slot_disponible_cuando_todo_es_correcto(db_session):
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="13:00")
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session,
        profesional_id=prof.id,
        fecha=_dia_habil_futuro(),
        hora="09:30",
    )

    assert resultado.disponible is True
    assert resultado.motivo is None
    assert resultado.overridable_con_sobrecupo is False


# ──────────────────────────────────────────────────────────
# evaluar_disponibilidad_slot() — casos rechazados
# ──────────────────────────────────────────────────────────

def test_hora_antes_de_horario_inicio_es_rechazada_pero_overridable(db_session):
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="13:00")
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=_dia_habil_futuro(), hora="08:00",
    )

    assert resultado.disponible is False
    assert resultado.motivo == "fuera_de_jornada"
    assert resultado.overridable_con_sobrecupo is True


def test_hora_despues_de_horario_fin_es_rechazada_pero_overridable(db_session):
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="13:00")
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=_dia_habil_futuro(), hora="13:00",
    )

    assert resultado.disponible is False
    assert resultado.motivo == "fuera_de_jornada"
    assert resultado.overridable_con_sobrecupo is True


def test_hora_en_colacion_es_rechazada_pero_overridable(db_session):
    prof = _profesional(
        db_session,
        horario_inicio="09:00",
        horario_fin="17:00",
        hora_almuerzo_inicio="12:00",
        hora_almuerzo_fin="13:00",
    )
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=_dia_habil_futuro(), hora="12:30",
    )

    assert resultado.disponible is False
    assert resultado.motivo == "en_colacion"
    assert resultado.overridable_con_sobrecupo is True


def test_hora_dentro_de_jornada_pero_fuera_de_grilla_es_rechazada_y_no_overridable(db_session):
    """Corrección v2, punto 1: 09:07 está dentro de 09:00-13:00, pero con
    duracion_min=30 los únicos bloques reales son 09:00, 09:30, 10:00...
    — no debe aceptarse solo por caer dentro de la jornada."""
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="13:00", duracion_min=30,
    )
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=_dia_habil_futuro(), hora="09:07",
    )

    assert resultado.disponible is False
    assert resultado.motivo == "hora_fuera_de_grilla"
    assert resultado.overridable_con_sobrecupo is False


# ──────────────────────────────────────────────────────────
# Corrección v3, punto 1 — orden grilla ANTES que jornada/colación.
#
# Un profesional con jornada 09:00-17:00 y duracion_min=30 tiene una
# grilla cruda del centro (08:00-18:00, pasos de 30) que incluye 08:00
# y 08:30 (fuera de su jornada, pero SÍ alineados) y excluye cualquier
# minuto que no sea :00 o :30 (alineado o no a la jornada).
# ──────────────────────────────────────────────────────────

def _profesional_v3_orden(db):
    return _profesional(
        db, horario_inicio="09:00", horario_fin="17:00", duracion_min=30,
    )


def test_hora_no_alineada_y_fuera_de_jornada_se_rechaza_por_grilla_no_por_jornada(db_session):
    """08:07 no está alineado a la grilla (pasos de 30 desde 08:00) Y
    además cae fuera de la jornada (09:00-17:00). Debe rechazarse por
    'hora_fuera_de_grilla' (no overridable) — no por 'fuera_de_jornada'
    (que sí sería overridable y dejaría colar un slot inexistente)."""
    prof = _profesional_v3_orden(db_session)
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=_dia_habil_futuro(), hora="08:07",
    )

    assert resultado.disponible is False
    assert resultado.motivo == "hora_fuera_de_grilla"
    assert resultado.overridable_con_sobrecupo is False


def test_hora_no_alineada_dentro_de_jornada_tambien_se_rechaza_por_grilla(db_session):
    """12:07 SÍ está dentro de la jornada (09:00-17:00), pero no es un
    bloque real de la grilla (pasos de 30). Debe rechazarse igual por
    'hora_fuera_de_grilla', no por ocupación ni por jornada."""
    prof = _profesional_v3_orden(db_session)
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=_dia_habil_futuro(), hora="12:07",
    )

    assert resultado.disponible is False
    assert resultado.motivo == "hora_fuera_de_grilla"
    assert resultado.overridable_con_sobrecupo is False


def test_hora_alineada_pero_fuera_de_jornada_sigue_siendo_overridable(db_session):
    """08:00 SÍ está alineado a la grilla cruda del centro, pero cae
    fuera de la jornada del profesional (09:00-17:00). A diferencia de
    los dos casos anteriores, este SÍ debe ser 'fuera_de_jornada' y
    overridable con sobrecupo — el orden grilla-antes-que-jornada no
    debe convertir esto en 'hora_fuera_de_grilla'."""
    prof = _profesional_v3_orden(db_session)
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=_dia_habil_futuro(), hora="08:00",
    )

    assert resultado.disponible is False
    assert resultado.motivo == "fuera_de_jornada"
    assert resultado.overridable_con_sobrecupo is True


# ──────────────────────────────────────────────────────────
# Corrección v4 — orden OCUPACIÓN antes que jornada/colación.
#
# "fuera_de_jornada"/"en_colacion" son overridable_con_sobrecupo=True.
# "slot_ocupado" es un bloqueo absoluto. Si ocupación se evaluara
# DESPUÉS de jornada/colación, un slot que está fuera de jornada (o en
# colación) Y ADEMÁS ya ocupado devolvería el motivo overridable
# primero, dejando que sobrecupo=True lo autorizara sin llegar nunca a
# ver que la hora ya estaba tomada. Estos tests fijan esa jerarquía a
# nivel del servicio; los equivalentes a nivel de POST /citas están
# más abajo, junto al resto de tests de sobrecupo.
# ──────────────────────────────────────────────────────────

def test_slot_ocupado_fuera_de_jornada_es_bloqueo_absoluto_no_overridable(db_session):
    """08:00 está alineado a la grilla pero fuera de jornada (09:00-17:00)
    — por sí solo sería 'fuera_de_jornada' (overridable). Si además ya
    está ocupado por otra cita, debe ganar 'slot_ocupado' (NO
    overridable), no 'fuera_de_jornada'."""
    fecha = _dia_habil_futuro()
    prof = _profesional_v3_orden(db_session)
    otro = _estudiante(db_session, correo="ocupante@sesaes.cl", rut="9-9")
    _cita(db_session, estudiante_id=otro.id, profesional_id=prof.id, fecha=fecha, hora="08:00")
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=fecha, hora="08:00",
    )

    assert resultado.disponible is False
    assert resultado.motivo == "slot_ocupado"
    assert resultado.overridable_con_sobrecupo is False


def test_slot_ocupado_en_colacion_es_bloqueo_absoluto_no_overridable(db_session):
    """12:30 está alineado a la grilla y cae en colación (12:30-13:00)
    — por sí solo sería 'en_colacion' (overridable). Si además ya está
    ocupado, debe ganar 'slot_ocupado' (NO overridable)."""
    fecha = _dia_habil_futuro()
    prof = _profesional(
        db_session,
        horario_inicio="09:00",
        horario_fin="17:00",
        hora_almuerzo_inicio="12:30",
        hora_almuerzo_fin="13:00",
        duracion_min=30,
    )
    otro = _estudiante(db_session, correo="ocupante2@sesaes.cl", rut="9-8")
    _cita(db_session, estudiante_id=otro.id, profesional_id=prof.id, fecha=fecha, hora="12:30")
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=fecha, hora="12:30",
    )

    assert resultado.disponible is False
    assert resultado.motivo == "slot_ocupado"
    assert resultado.overridable_con_sobrecupo is False


def test_crear_cita_sobrecupo_rechaza_hora_no_alineada_aunque_fuera_de_jornada(db_session, monkeypatch):
    """Integración end-to-end del caso que motivó la corrección v3:
    08:07 (ni alineado, ni dentro de jornada) + sobrecupo=True debe
    seguir siendo rechazado por POST /citas."""
    prof = _profesional_v3_orden(db_session)
    est = _estudiante(db_session)
    db_session.commit()

    monkeypatch.setattr(citas, "tiene_permiso_efectivo", lambda db, u, p: True)
    monkeypatch.setattr(
        citas,
        "obtener_alcance_administrativo_efectivo",
        lambda db, u: AlcanceAdministrativoEfectivo(
            institucional=True, especialidades_normalizadas=frozenset(),
        ),
    )

    payload = CitaCreate(
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=_dia_habil_futuro(),
        hora="08:07",
        sobrecupo=True,
    )

    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(cita=payload, db=db_session, current_user={"id": 999, "rol": "admin"})

    assert exc.value.status_code == 400
    assert db_session.query(Cita).count() == 0


def test_crear_cita_sobrecupo_rechaza_hora_no_alineada_dentro_de_jornada(db_session, monkeypatch):
    """12:07 (dentro de jornada, pero no alineado) + sobrecupo=True
    también debe seguir siendo rechazado."""
    prof = _profesional_v3_orden(db_session)
    est = _estudiante(db_session)
    db_session.commit()

    monkeypatch.setattr(citas, "tiene_permiso_efectivo", lambda db, u, p: True)
    monkeypatch.setattr(
        citas,
        "obtener_alcance_administrativo_efectivo",
        lambda db, u: AlcanceAdministrativoEfectivo(
            institucional=True, especialidades_normalizadas=frozenset(),
        ),
    )

    payload = CitaCreate(
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=_dia_habil_futuro(),
        hora="12:07",
        sobrecupo=True,
    )

    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(cita=payload, db=db_session, current_user={"id": 999, "rol": "admin"})

    assert exc.value.status_code == 400
    assert db_session.query(Cita).count() == 0


def test_crear_cita_sobrecupo_permite_hora_alineada_fuera_de_jornada(db_session, monkeypatch):
    """08:00 (alineado a la grilla, fuera de jornada) + sobrecupo=True
    debe permitirse — este es el caso legítimo de sobrecupo que la
    corrección v3 no debe romper."""
    prof = _profesional_v3_orden(db_session)
    est = _estudiante(db_session)
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, citas, sobrecupo_policy_service)

    payload = CitaCreate(
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=_dia_habil_futuro(),
        hora="08:00",
        sobrecupo=True,
        # A.4.3 — sobrecupo efectivo ahora exige motivo humano no vacío.
        sobrecupo_motivo="Paciente con examen médico justo antes",
    )

    resultado = citas.crear_cita(cita=payload, db=db_session, current_user={"id": 999, "rol": "admin"})

    assert resultado["estado"] == "pendiente"
    creada = db_session.query(Cita).one()
    assert creada.sobrecupo is True


# ──────────────────────────────────────────────────────────
# Corrección v3, punto 2 — duracion_min <= 0 no debe generar un bucle
# infinito ni un bucle sin cota.
# ──────────────────────────────────────────────────────────

def test_generar_bloques_jornada_duracion_cero_no_produce_loop_infinito():
    """duracion_min=0 no debe hacer que el bucle nunca avance — la
    sola ejecución de este test (sin colgarse) ya es la prueba; además
    se verifica que cae de vuelta a la duración por defecto del centro."""
    from app.services.agenda_disponibilidad_service import (
        DURACION_MIN_POR_DEFECTO,
        generar_bloques_jornada,
    )

    bloques = generar_bloques_jornada(0)

    assert bloques == generar_bloques_jornada(DURACION_MIN_POR_DEFECTO)
    assert len(bloques) > 0


def test_generar_bloques_jornada_duracion_negativa_no_produce_loop_infinito():
    """duracion_min negativo no debe hacer que `actual` retroceda sin
    cota respecto de `fin` — mismo blindaje que duracion_min=0."""
    from app.services.agenda_disponibilidad_service import (
        DURACION_MIN_POR_DEFECTO,
        generar_bloques_jornada,
    )

    bloques = generar_bloques_jornada(-30)

    assert bloques == generar_bloques_jornada(DURACION_MIN_POR_DEFECTO)
    assert len(bloques) > 0


def test_evaluar_disponibilidad_con_duracion_min_cero_no_bloquea_y_usa_defecto(db_session):
    """Un profesional con duracion_min=0 (dato inválido) no debe hacer
    que evaluar_disponibilidad_slot() se cuelgue ni reviente — debe
    evaluarse con la grilla de duración por defecto del centro (45 min
    desde las 08:00: 08:00, 08:45, 09:30... — por eso se pide 09:30,
    que sí es un bloque real de esa grilla)."""
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="17:00", duracion_min=0,
    )
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=_dia_habil_futuro(), hora="09:30",
    )

    assert resultado.disponible is True


def test_evaluar_disponibilidad_con_duracion_min_negativa_no_bloquea_y_usa_defecto(db_session):
    """Mismo blindaje que duracion_min=0, para un valor negativo."""
    prof = _profesional(
        db_session, horario_inicio="09:00", horario_fin="17:00", duracion_min=-15,
    )
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=_dia_habil_futuro(), hora="09:30",
    )

    assert resultado.disponible is True


def test_profesional_inactivo_es_rechazado_y_no_overridable(db_session):
    prof = _profesional(db_session, estado="licencia")
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=_dia_habil_futuro(), hora="09:30",
    )

    assert resultado.disponible is False
    assert resultado.motivo == "profesional_inactivo"
    assert resultado.overridable_con_sobrecupo is False


def test_dia_cerrado_es_rechazado_y_no_overridable(db_session):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    db_session.add(DiaCerrado(fecha=fecha, motivo="Feriado irrenunciable"))
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=fecha, hora="09:30",
    )

    assert resultado.disponible is False
    assert resultado.motivo == "dia_cerrado"
    assert resultado.overridable_con_sobrecupo is False


def test_fecha_pasada_es_rechazada_y_no_overridable(db_session):
    """Corrección v2, punto 2: regla ESTRUCTURAL — nadie, ni con
    sobrecupo, puede agendar una cita normal en una fecha ya pasada."""
    prof = _profesional(db_session)
    db_session.commit()
    fecha_pasada = (date.today() - timedelta(days=1)).isoformat()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=fecha_pasada, hora="09:30",
    )

    assert resultado.disponible is False
    assert resultado.motivo == "fecha_pasada"
    assert resultado.overridable_con_sobrecupo is False


def test_fin_de_semana_es_rechazado_y_no_overridable(db_session):
    """Corrección v2, punto 2: regla ESTRUCTURAL — el centro no atiende
    fines de semana, sin importar quién pregunte."""
    prof = _profesional(db_session)
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=_proximo_fin_de_semana(), hora="09:30",
    )

    assert resultado.disponible is False
    assert resultado.motivo == "fin_de_semana"
    assert resultado.overridable_con_sobrecupo is False


def test_hora_ya_pasada_hoy_es_rechazada_y_no_overridable(db_session, monkeypatch):
    """Corrección v2, punto 2: regla ESTRUCTURAL de "hoy" — una hora que
    ya pasó no es agendable aunque el resto de las reglas la permitan.

    A.3 (v2) — determinismo: este test antes usaba `date.today()`/
    `datetime.now()` reales, lo que lo hacía fallar cualquier fin de
    semana real (fin_de_semana, de mayor prioridad y sin cambios,
    ganaba antes de llegar a evaluar "hora_pasada"). En vez de saltar
    el test con pytest.skip() los fines de semana, se fija el reloj
    que ve el módulo (date.today()/datetime.now()) a un miércoles
    conocido vía monkeypatch, para que la regla se ejercite siempre,
    cualquier día real en que se corra la suite. No se toca ninguna
    lógica de negocio — solo qué "ahora" ve el módulo durante el test.
    """
    prof = _profesional(db_session, horario_inicio="00:00", horario_fin="23:59")
    db_session.commit()

    hoy_fijo = date(2026, 9, 9)  # miércoles conocido, nunca fin de semana
    ahora_fijo = datetime(2026, 9, 9, 10, 0, 0)
    assert hoy_fijo.weekday() < 5

    class _FechaFija(date):
        @classmethod
        def today(cls):
            return hoy_fijo

    class _DatetimeFijo(datetime):
        @classmethod
        def now(cls, tz=None):
            return ahora_fijo

    monkeypatch.setattr(agenda_disponibilidad_service, "date", _FechaFija)
    monkeypatch.setattr(agenda_disponibilidad_service, "datetime", _DatetimeFijo)

    hoy = hoy_fijo.isoformat()
    hora_pasada = (ahora_fijo - timedelta(minutes=5)).strftime("%H:%M")

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=hoy, hora=hora_pasada,
    )

    assert resultado.disponible is False
    assert resultado.motivo == "hora_pasada"
    assert resultado.overridable_con_sobrecupo is False


def test_slot_ocupado_por_cita_pendiente_es_rechazado_y_no_overridable(db_session):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    est = _estudiante(db_session)
    _cita(
        db_session,
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=fecha,
        hora="09:30",
        estado="pendiente",
    )
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=fecha, hora="09:30",
    )

    assert resultado.disponible is False
    assert resultado.motivo == "slot_ocupado"
    assert resultado.overridable_con_sobrecupo is False


def test_slot_ocupado_por_sobrecupo_existente_tambien_bloquea(db_session):
    """Una cita ya creada como sobrecupo sigue siendo una cita real: debe
    ocupar el slot igual que cualquier otra pendiente/completada. A.2 no
    diseña el sistema definitivo de sobrecupos (A.4); solo confirma que
    no introduce un agujero donde un sobrecupo existente "no cuenta"."""
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="13:00")
    est = _estudiante(db_session)
    _cita(
        db_session,
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=fecha,
        hora="09:00",
        estado="pendiente",
        sobrecupo=True,
    )
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=fecha, hora="09:00",
    )

    assert resultado.disponible is False
    assert resultado.motivo == "slot_ocupado"


def test_slot_libre_si_la_cita_existente_esta_cancelada(db_session):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    est = _estudiante(db_session)
    _cita(
        db_session,
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=fecha,
        hora="09:30",
        estado="cancelada",
    )
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=fecha, hora="09:30",
    )

    assert resultado.disponible is True


# ──────────────────────────────────────────────────────────
# Normalización de hora (24h vs "HH:MM AM/PM") — corrección v2, punto 3
# ──────────────────────────────────────────────────────────

def test_slot_ocupado_detectado_aunque_la_cita_este_guardada_en_formato_ampm(db_session):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="13:00")
    est = _estudiante(db_session)
    _cita(
        db_session,
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=fecha,
        hora="09:30 AM",  # formato legado, mismo slot que "09:30"
        estado="pendiente",
    )
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=fecha, hora="09:30",
    )

    assert resultado.disponible is False
    assert resultado.motivo == "slot_ocupado"


def test_evaluar_disponibilidad_acepta_hora_solicitada_en_formato_ampm(db_session):
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="13:00")
    db_session.commit()

    resultado = evaluar_disponibilidad_slot(
        db_session, profesional_id=prof.id, fecha=_dia_habil_futuro(), hora="09:30 AM",
    )

    assert resultado.disponible is True


def test_listar_horas_disponibles_excluye_hora_ocupada_en_formato_ampm(db_session):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="13:00")
    est = _estudiante(db_session)
    _cita(
        db_session,
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=fecha,
        hora="09:30 AM",
        estado="pendiente",
    )
    db_session.commit()

    horas, _mensaje = listar_horas_disponibles(
        db_session, profesional_id=prof.id, fecha=fecha,
    )

    assert "09:30" not in horas


# ──────────────────────────────────────────────────────────
# listar_horas_disponibles() — contrato de GET /disponibilidad/{id}
# ──────────────────────────────────────────────────────────

def test_listar_horas_disponibles_excluye_profesional_inactivo(db_session):
    prof = _profesional(db_session, estado="licencia")
    db_session.commit()

    horas, _mensaje = listar_horas_disponibles(
        db_session, profesional_id=prof.id, fecha=_dia_habil_futuro(),
    )

    assert horas == []


def test_listar_horas_disponibles_respeta_dia_cerrado_con_mensaje(db_session):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    db_session.add(DiaCerrado(fecha=fecha, motivo="Cierre de prueba"))
    db_session.commit()

    horas, mensaje = listar_horas_disponibles(
        db_session, profesional_id=prof.id, fecha=fecha,
    )

    assert horas == []
    assert "Cierre de prueba" in mensaje


def test_listar_horas_disponibles_rechaza_fin_de_semana(db_session):
    prof = _profesional(db_session)
    db_session.commit()

    horas, mensaje = listar_horas_disponibles(
        db_session, profesional_id=prof.id, fecha=_proximo_fin_de_semana(),
    )

    assert horas == []


def test_listar_horas_disponibles_rechaza_fecha_mas_alla_de_la_ventana(db_session):
    prof = _profesional(db_session)
    db_session.commit()

    horas, mensaje = listar_horas_disponibles(
        db_session, profesional_id=prof.id, fecha=_fecha_fuera_de_ventana_estudiante(),
    )

    assert horas == []


# ──────────────────────────────────────────────────────────
# excede_ventana_agendamiento_estudiante() — política de consumidor
# ──────────────────────────────────────────────────────────

def test_excede_ventana_estudiante_para_fecha_lejana():
    assert excede_ventana_agendamiento_estudiante(_fecha_fuera_de_ventana_estudiante()) is True


def test_no_excede_ventana_estudiante_para_fecha_cercana():
    assert excede_ventana_agendamiento_estudiante(_dia_habil_futuro()) is False


# ──────────────────────────────────────────────────────────
# POST /citas (citas.crear_cita) — integración de A.2
# ──────────────────────────────────────────────────────────

def test_crear_cita_normal_rechaza_fuera_de_jornada(db_session):
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="13:00")
    est = _estudiante(db_session)
    db_session.commit()

    payload = CitaCreate(
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=_dia_habil_futuro(),
        hora="08:00",
    )

    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(
            cita=payload,
            db=db_session,
            current_user=_current_user_estudiante(est),
        )

    assert exc.value.status_code == 400
    assert db_session.query(Cita).count() == 0


def test_crear_cita_normal_rechaza_en_colacion(db_session):
    prof = _profesional(
        db_session,
        horario_inicio="09:00",
        horario_fin="17:00",
        hora_almuerzo_inicio="12:00",
        hora_almuerzo_fin="13:00",
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
            current_user=_current_user_estudiante(est),
        )

    assert exc.value.status_code == 400


def test_crear_cita_normal_rechaza_hora_fuera_de_grilla(db_session):
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="13:00", duracion_min=30)
    est = _estudiante(db_session)
    db_session.commit()

    payload = CitaCreate(
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=_dia_habil_futuro(),
        hora="09:07",
    )

    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(
            cita=payload,
            db=db_session,
            current_user=_current_user_estudiante(est),
        )

    assert exc.value.status_code == 400
    assert db_session.query(Cita).count() == 0


def test_crear_cita_normal_permitida_dentro_de_jornada(db_session):
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="13:00")
    est = _estudiante(db_session)
    db_session.commit()

    payload = CitaCreate(
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=_dia_habil_futuro(),
        hora="09:30",
    )

    resultado = citas.crear_cita(
        cita=payload,
        db=db_session,
        current_user=_current_user_estudiante(est),
    )

    assert resultado["estado"] == "pendiente"
    assert db_session.query(Cita).count() == 1


def test_crear_cita_rechaza_slot_ya_ocupado_por_otra_cita(db_session):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="13:00")
    est = _estudiante(db_session, correo="a@sesaes.cl", rut="1-1")
    otro = _estudiante(db_session, correo="b@sesaes.cl", rut="2-2")
    _cita(
        db_session,
        estudiante_id=otro.id,
        profesional_id=prof.id,
        fecha=fecha,
        hora="09:30",
    )
    db_session.commit()

    payload = CitaCreate(
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=fecha,
        hora="09:30",
    )

    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(
            cita=payload,
            db=db_session,
            current_user=_current_user_estudiante(est),
        )

    # A.3 — slot_ocupado tras la re-evaluación dentro del lock de
    # concurrencia es, por definición, un conflicto (409), no una
    # simple invalidez de datos (400). La lógica de disponibilidad en
    # sí (que ESTA hora está ocupada) no cambió — solo el código HTTP
    # con el que se comunica.
    assert exc.value.status_code == 409


def test_crear_cita_estudiante_rechaza_slot_ocupado_guardado_en_ampm(db_session):
    """Corrección v2, punto 3, a través de la integración completa:
    un estudiante no debe poder reservar "09:30" si ya existe una cita
    pendiente guardada como "09:30 AM"."""
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="13:00")
    est = _estudiante(db_session, correo="a@sesaes.cl", rut="1-1")
    otro = _estudiante(db_session, correo="b@sesaes.cl", rut="2-2")
    _cita(
        db_session,
        estudiante_id=otro.id,
        profesional_id=prof.id,
        fecha=fecha,
        hora="09:30 AM",
    )
    db_session.commit()

    payload = CitaCreate(
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=fecha,
        hora="09:30",
    )

    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(
            cita=payload,
            db=db_session,
            current_user=_current_user_estudiante(est),
        )

    # A.3 — ver nota de test_crear_cita_rechaza_slot_ya_ocupado_por_otra_cita.
    assert exc.value.status_code == 409
    assert db_session.query(Cita).count() == 1  # solo la ya existente


def test_crear_cita_estudiante_no_puede_saltarse_ventana_llamando_directo_a_post_citas(db_session):
    """Corrección v2, punto 2: aunque GET /disponibilidad no le ofrezca
    una fecha más allá de la ventana, un estudiante no debe poder
    conseguirla llamando directo a POST /citas."""
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="13:00")
    est = _estudiante(db_session)
    db_session.commit()

    payload = CitaCreate(
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=_fecha_fuera_de_ventana_estudiante(),
        hora="09:30",
    )

    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(
            cita=payload,
            db=db_session,
            current_user=_current_user_estudiante(est),
        )

    assert exc.value.status_code == 400
    assert db_session.query(Cita).count() == 0


def test_crear_cita_admin_puede_agendar_mas_alla_de_la_ventana_de_estudiante(db_session, monkeypatch):
    """Corrección v2, punto 2: la ventana de 7 días es política de
    Estudiante, no una regla estructural — Agenda Admin (agenda.gestionar)
    no debe quedar bloqueada por ella al crear una cita NORMAL (sin
    urgente ni sobrecupo) más allá de esos días."""
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="13:00")
    est = _estudiante(db_session)
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, citas)

    payload = CitaCreate(
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=_fecha_fuera_de_ventana_estudiante(),
        hora="09:30",
    )

    resultado = citas.crear_cita(
        cita=payload,
        db=db_session,
        current_user={"id": 999, "rol": "admin"},
    )

    assert resultado["estado"] == "pendiente"
    assert db_session.query(Cita).count() == 1


def test_sobrecupo_admin_supera_fuera_de_jornada(db_session, monkeypatch):
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="13:00")
    est = _estudiante(db_session)
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, citas, sobrecupo_policy_service)

    payload = CitaCreate(
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=_dia_habil_futuro(),
        hora="08:00",
        sobrecupo=True,
        # A.4.3 — sobrecupo efectivo ahora exige motivo humano no vacío.
        sobrecupo_motivo="Paciente con examen médico justo antes",
    )

    resultado = citas.crear_cita(
        cita=payload,
        db=db_session,
        current_user={"id": 999, "rol": "admin"},
    )

    assert resultado["estado"] == "pendiente"
    creada = db_session.query(Cita).one()
    assert creada.sobrecupo is True


def test_sobrecupo_admin_no_supera_profesional_inactivo(db_session, monkeypatch):
    prof = _profesional(db_session, estado="licencia")
    est = _estudiante(db_session)
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, citas)

    payload = CitaCreate(
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=_dia_habil_futuro(),
        hora="09:30",
        sobrecupo=True,
    )

    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(
            cita=payload,
            db=db_session,
            current_user={"id": 999, "rol": "admin"},
        )

    assert exc.value.status_code == 400
    assert db_session.query(Cita).count() == 0


def test_sobrecupo_admin_no_supera_slot_ya_ocupado(db_session, monkeypatch):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="13:00")
    est = _estudiante(db_session, correo="a@sesaes.cl", rut="1-1")
    otro = _estudiante(db_session, correo="b@sesaes.cl", rut="2-2")
    _cita(
        db_session,
        estudiante_id=otro.id,
        profesional_id=prof.id,
        fecha=fecha,
        hora="09:30",
    )
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, citas)

    payload = CitaCreate(
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=fecha,
        hora="09:30",
        sobrecupo=True,
    )

    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(
            cita=payload,
            db=db_session,
            current_user={"id": 999, "rol": "admin"},
        )

    # A.3 — ver nota en test_crear_cita_rechaza_slot_ya_ocupado_por_otra_cita:
    # slot_ocupado ahora es 409, no 400. sobrecupo=True sigue sin poder
    # superarlo (no cambia qué motivos son overridable).
    assert exc.value.status_code == 409


def test_sobrecupo_admin_no_supera_slot_ocupado_fuera_de_jornada(db_session, monkeypatch):
    """Corrección v4 — caso exacto reportado: profesional 09:00-17:00,
    el slot 08:00 (fuera de jornada, pero alineado a la grilla) ya está
    ocupado por otra cita, y se intenta un sobrecupo=True sobre esa
    misma hora. Antes de la corrección, 'fuera_de_jornada' (overridable)
    se detectaba primero y el sobrecupo lo autorizaba sin llegar a ver
    la ocupación. Ahora debe rechazarse como slot_ocupado."""
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="17:00", duracion_min=30)
    ocupante = _estudiante(db_session, correo="ocupante-router@sesaes.cl", rut="3-3")
    nuevo = _estudiante(db_session, correo="nuevo-router@sesaes.cl", rut="4-4")
    _cita(db_session, estudiante_id=ocupante.id, profesional_id=prof.id, fecha=fecha, hora="08:00")
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, citas)

    payload = CitaCreate(
        estudiante_id=nuevo.id,
        profesional_id=prof.id,
        fecha=fecha,
        hora="08:00",
        sobrecupo=True,
    )

    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(
            cita=payload,
            db=db_session,
            current_user={"id": 999, "rol": "admin"},
        )

    assert exc.value.status_code == 409
    assert "reservada" in exc.value.detail.lower() or "ocupad" in exc.value.detail.lower()
    # Solo debe existir la cita original — el sobrecupo no se insertó.
    assert db_session.query(Cita).count() == 1


def test_sobrecupo_admin_no_supera_slot_ocupado_en_colacion(db_session, monkeypatch):
    """Mismo caso que el anterior, pero para colación en vez de fuera
    de jornada: el slot alineado 12:30 cae en colación (12:30-13:00) Y
    ya está ocupado. Debe rechazarse como slot_ocupado, no como
    en_colacion."""
    fecha = _dia_habil_futuro()
    prof = _profesional(
        db_session,
        horario_inicio="09:00",
        horario_fin="17:00",
        hora_almuerzo_inicio="12:30",
        hora_almuerzo_fin="13:00",
        duracion_min=30,
    )
    ocupante = _estudiante(db_session, correo="ocupante-router2@sesaes.cl", rut="5-5")
    nuevo = _estudiante(db_session, correo="nuevo-router2@sesaes.cl", rut="6-6")
    _cita(db_session, estudiante_id=ocupante.id, profesional_id=prof.id, fecha=fecha, hora="12:30")
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, citas)

    payload = CitaCreate(
        estudiante_id=nuevo.id,
        profesional_id=prof.id,
        fecha=fecha,
        hora="12:30",
        sobrecupo=True,
    )

    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(
            cita=payload,
            db=db_session,
            current_user={"id": 999, "rol": "admin"},
        )

    assert exc.value.status_code == 409
    assert db_session.query(Cita).count() == 1


def test_sobrecupo_admin_supera_fuera_de_jornada_cuando_slot_libre(db_session, monkeypatch):
    """Contraparte de los dos tests anteriores: el mismo slot 08:00
    (fuera de jornada, alineado a la grilla) pero SIN ninguna cita
    previa debe seguir permitiéndose con sobrecupo=True — la corrección
    v4 no vuelve más estricto el caso legítimo, solo corrige el orden
    quitando el agujero cuando el slot ya estaba ocupado."""
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="17:00", duracion_min=30)
    est = _estudiante(db_session, correo="libre-router@sesaes.cl", rut="7-7")
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, citas, sobrecupo_policy_service)

    payload = CitaCreate(
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=fecha,
        hora="08:00",
        sobrecupo=True,
        # A.4.3 — sobrecupo efectivo ahora exige motivo humano no vacío.
        sobrecupo_motivo="Paciente con examen médico justo antes",
    )

    resultado = citas.crear_cita(
        cita=payload,
        db=db_session,
        current_user={"id": 999, "rol": "admin"},
    )

    assert resultado["estado"] == "pendiente"
    creada = db_session.query(Cita).one()
    assert creada.sobrecupo is True


def test_sobrecupo_admin_no_supera_hora_fuera_de_grilla(db_session, monkeypatch):
    """Corrección v2, punto 1: sobrecupo solo supera fuera-de-jornada y
    colación, nunca un horario que ni siquiera pertenece a la grilla."""
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="13:00", duracion_min=30)
    est = _estudiante(db_session)
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, citas)

    payload = CitaCreate(
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=_dia_habil_futuro(),
        hora="09:07",
        sobrecupo=True,
    )

    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(
            cita=payload,
            db=db_session,
            current_user={"id": 999, "rol": "admin"},
        )

    assert exc.value.status_code == 400
    assert db_session.query(Cita).count() == 0


# ──────────────────────────────────────────────────────────
# POST /admin/citas/urgente — no debe verse afectado por A.2
# ──────────────────────────────────────────────────────────

def test_cita_urgente_sigue_sin_exigir_disponibilidad_ordinaria(db_session, monkeypatch):
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="13:00")
    est = _estudiante(db_session)
    db_session.commit()

    monkeypatch.setattr(
        admin,
        "obtener_alcance_administrativo_efectivo",
        lambda db, u: AlcanceAdministrativoEfectivo(
            institucional=True, especialidades_normalizadas=frozenset(),
        ),
    )

    payload = CitaCreate(
        estudiante_id=est.id,
        profesional_id=prof.id,
        fecha=_dia_habil_futuro(),
        hora="08:00",  # fuera de jornada — una cita normal la rechazaría
    )

    resultado = admin.crear_cita_urgente(
        cita=payload,
        db=db_session,
        current_user={"id": 999, "rol": "admin"},
    )

    assert resultado["estado"] == "pendiente"
    creada = db_session.query(Cita).one()
    assert creada.urgente is True
