# -*- coding: utf-8 -*-
"""
SESAES — A.4.3: política central de sobrecupo + permisos.

Dos niveles de test, deliberadamente separados:

  1. UNITARIOS de `evaluar_politica_sobrecupo()` en sí — sin DB real,
     construyendo `ResultadoDisponibilidad`/`ConflictoSlot` a mano y
     monkeypatcheando `tiene_permiso_efectivo` con control fino sobre
     agenda.gestionar / agenda.sobrecupo por separado (nunca un
     blanket `True` para cualquier permiso). Cubren el orden A–H
     completo, los códigos de decisión, y los casos límite del motivo
     humano.

  2. INTEGRACIÓN a través de `app.routers.citas.crear_cita()` con una
     base SQLite en memoria real — para el mapeo a status/detail HTTP,
     la persistencia de Cita/CitaSobrecupo/CitaSobrecupoConflicto, la
     auditoría, y el rollback atómico ante falla tardía.

No repite lo que ya cubren test_a4_2_conflictos.py (qué conflictos
existen, cuáles son overridable) ni test_a3_concurrencia.py (orden
lock -> re-evaluación) — A.4.3 no cambia nada de eso, solo consume sus
resultados.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models.init  # noqa: F401 — registra TODOS los modelos productivos
import app.models.solicitud_horario  # noqa: F401

from app.database import Base
from app.models.auditoria import Auditoria
from app.models.cita import Cita
from app.models.cita_sobrecupo import CitaSobrecupo, CitaSobrecupoConflicto
from app.models.profesional import Profesional
from app.models.usuario import Usuario
from app.rbac.admin_authorization import AlcanceAdministrativoEfectivo
from app.rbac.permissions import Permission
from app.routers import citas
from app.schemas import CitaCreate
import app.services.sobrecupo_policy_service as sobrecupo_policy_service
from app.services.agenda_disponibilidad_service import ConflictoSlot, ResultadoDisponibilidad
from app.services.sobrecupo_policy_service import (
    CODIGO_AUTORIZADO,
    CODIGO_DENEGADO_BLOQUEO_ABSOLUTO,
    CODIGO_DENEGADO_CONFLICTO_SIN_INTENCION,
    CODIGO_DENEGADO_ENTRADA_INCOHERENTE,
    CODIGO_DENEGADO_PRECONDICION_LEGACY,
    CODIGO_DENEGADO_SIN_MOTIVO,
    CODIGO_DENEGADO_SIN_PERMISO,
    CODIGO_FLAG_SOBRANTE_SIN_CONFLICTO,
    CODIGO_NORMAL_SIN_CONFLICTO,
    evaluar_politica_sobrecupo,
)


# ══════════════════════════════════════════════════════════
# Parte 1 — unitarios de evaluar_politica_sobrecupo()
# ══════════════════════════════════════════════════════════

def _conflicto(codigo, *, overridable, categoria="jornada_profesional"):
    return ConflictoSlot(
        codigo=codigo, categoria=categoria,
        overridable_con_sobrecupo=overridable, metadata={},
    )


_FUERA_DE_JORNADA = _conflicto("fuera_de_jornada", overridable=True)
_EN_COLACION = _conflicto("en_colacion", overridable=True)
_SLOT_OCUPADO = _conflicto("slot_ocupado", overridable=False, categoria="ocupacion")
_EXCEDE_CIERRE = _conflicto("excede_cierre_centro", overridable=False, categoria="grilla_centro")


def _resultado(
    *, disponible, conflictos=(), motivo=None, mensaje=None, overridable_con_sobrecupo=False,
):
    return ResultadoDisponibilidad(
        disponible=disponible,
        motivo=motivo,
        mensaje=mensaje,
        overridable_con_sobrecupo=overridable_con_sobrecupo,
        profesional=None,
        conflictos=conflictos,
    )


def _monkeypatch_permisos(monkeypatch, *, gestionar: bool, sobrecupo: bool):
    """
    Control fino y explícito: nunca un blanket `True` para cualquier
    permiso. Distingue agenda.gestionar de agenda.sobrecupo, que es
    exactamente lo que A.4.3 necesita poder probar por separado.
    """
    def _resolver(db, current_user, permiso):
        if permiso == Permission.AGENDA_GESTIONAR:
            return gestionar
        if permiso == Permission.AGENDA_SOBRECUPO:
            return sobrecupo
        raise AssertionError(f"permiso no contemplado por este test: {permiso!r}")

    monkeypatch.setattr(sobrecupo_policy_service, "tiene_permiso_efectivo", _resolver)


def _evaluar(monkeypatch, *, gestionar=True, sobrecupo=True, **kwargs):
    _monkeypatch_permisos(monkeypatch, gestionar=gestionar, sobrecupo=sobrecupo)
    kwargs.setdefault("sobrecupo_solicitado", False)
    kwargs.setdefault("sobrecupo_motivo", None)
    kwargs.setdefault("resultado_disponibilidad", _resultado(disponible=True))
    return evaluar_politica_sobrecupo(
        db=None, current_user={"id": 1, "rol": "admin"}, **kwargs,
    )


# --- A: normal sin conflicto -------------------------------------

def test_a_normal_sin_conflicto(monkeypatch):
    decision = _evaluar(
        monkeypatch,
        sobrecupo_solicitado=False,
        resultado_disponibilidad=_resultado(disponible=True),
    )
    assert decision.permitido is True
    assert decision.es_sobrecupo_efectivo is False
    assert decision.codigo == CODIGO_NORMAL_SIN_CONFLICTO
    assert decision.conflictos == ()
    assert decision.motivo_normalizado is None


# --- B: flag sobrante sin conflicto -------------------------------

def test_b_flag_sobrante_sin_conflicto(monkeypatch):
    """slot realmente libre + sobrecupo=True: cita normal, sin exigir
    permiso NI motivo — ni siquiera se consultan (gestionar=False,
    sobrecupo=False acá, y aun así pasa)."""
    decision = _evaluar(
        monkeypatch,
        gestionar=False, sobrecupo=False,
        sobrecupo_solicitado=True,
        sobrecupo_motivo=None,
        resultado_disponibilidad=_resultado(disponible=True),
    )
    assert decision.permitido is True
    assert decision.es_sobrecupo_efectivo is False
    assert decision.codigo == CODIGO_FLAG_SOBRANTE_SIN_CONFLICTO
    assert decision.requiere_motivo is False
    assert decision.motivo_normalizado is None


def test_slot_libre_sobrecupo_true_motivo_vacio_sigue_siendo_normal(monkeypatch):
    decision = _evaluar(
        monkeypatch,
        gestionar=False, sobrecupo=False,
        sobrecupo_solicitado=True,
        sobrecupo_motivo="",
        resultado_disponibilidad=_resultado(disponible=True),
    )
    assert decision.permitido is True
    assert decision.es_sobrecupo_efectivo is False
    assert decision.codigo == CODIGO_FLAG_SOBRANTE_SIN_CONFLICTO


# --- C: 1 conflicto overridable autorizado ------------------------

def test_c_un_conflicto_overridable_autorizado(monkeypatch):
    decision = _evaluar(
        monkeypatch,
        gestionar=True, sobrecupo=True,
        sobrecupo_solicitado=True,
        sobrecupo_motivo="Paciente con examen médico justo antes",
        resultado_disponibilidad=_resultado(
            disponible=False, motivo="fuera_de_jornada",
            conflictos=(_FUERA_DE_JORNADA,),
        ),
    )
    assert decision.permitido is True
    assert decision.es_sobrecupo_efectivo is True
    assert decision.codigo == CODIGO_AUTORIZADO
    assert decision.conflictos == (_FUERA_DE_JORNADA,)
    assert decision.requiere_motivo is True
    assert decision.motivo_normalizado == "Paciente con examen médico justo antes"


# --- D: 2 conflictos overridables autorizados ---------------------

def test_d_dos_conflictos_overridables_autorizados(monkeypatch):
    decision = _evaluar(
        monkeypatch,
        gestionar=True, sobrecupo=True,
        sobrecupo_solicitado=True,
        sobrecupo_motivo="Paciente con dos exámenes seguidos",
        resultado_disponibilidad=_resultado(
            disponible=False, motivo="fuera_de_jornada",
            conflictos=(_FUERA_DE_JORNADA, _EN_COLACION),
        ),
    )
    assert decision.permitido is True
    assert decision.es_sobrecupo_efectivo is True
    assert decision.codigo == CODIGO_AUTORIZADO
    assert len(decision.conflictos) == 2
    assert {c.codigo for c in decision.conflictos} == {"fuera_de_jornada", "en_colacion"}


# --- E: conflicto sin intención -----------------------------------

def test_e_conflicto_sin_intencion(monkeypatch):
    decision = _evaluar(
        monkeypatch,
        gestionar=True, sobrecupo=True,
        sobrecupo_solicitado=False,
        resultado_disponibilidad=_resultado(
            disponible=False, motivo="fuera_de_jornada",
            conflictos=(_FUERA_DE_JORNADA,),
        ),
    )
    assert decision.permitido is False
    assert decision.es_sobrecupo_efectivo is False
    assert decision.codigo == CODIGO_DENEGADO_CONFLICTO_SIN_INTENCION


# --- F: admin con gestionar pero sin sobrecupo --------------------

def test_f_gestionar_sin_sobrecupo_es_denegado_sin_permiso(monkeypatch):
    decision = _evaluar(
        monkeypatch,
        gestionar=True, sobrecupo=False,
        sobrecupo_solicitado=True,
        sobrecupo_motivo="Motivo válido",
        resultado_disponibilidad=_resultado(
            disponible=False, motivo="fuera_de_jornada",
            conflictos=(_FUERA_DE_JORNADA,),
        ),
    )
    assert decision.permitido is False
    assert decision.es_sobrecupo_efectivo is False
    assert decision.codigo == CODIGO_DENEGADO_SIN_PERMISO


# --- G: permiso correcto pero falta motivo ------------------------

@pytest.mark.parametrize("motivo_crudo", [None, "", "   ", "\t\n  "])
def test_g_permisos_ok_pero_motivo_vacio(monkeypatch, motivo_crudo):
    decision = _evaluar(
        monkeypatch,
        gestionar=True, sobrecupo=True,
        sobrecupo_solicitado=True,
        sobrecupo_motivo=motivo_crudo,
        resultado_disponibilidad=_resultado(
            disponible=False, motivo="fuera_de_jornada",
            conflictos=(_FUERA_DE_JORNADA,),
        ),
    )
    assert decision.permitido is False
    assert decision.es_sobrecupo_efectivo is False
    assert decision.codigo == CODIGO_DENEGADO_SIN_MOTIVO
    assert decision.motivo_normalizado is None


def test_motivo_con_espacios_alrededor_persiste_trimeado(monkeypatch):
    decision = _evaluar(
        monkeypatch,
        gestionar=True, sobrecupo=True,
        sobrecupo_solicitado=True,
        sobrecupo_motivo="  Motivo válido  ",
        resultado_disponibilidad=_resultado(
            disponible=False, motivo="fuera_de_jornada",
            conflictos=(_FUERA_DE_JORNADA,),
        ),
    )
    assert decision.codigo == CODIGO_AUTORIZADO
    assert decision.motivo_normalizado == "Motivo válido"


# --- H: absoluto + overridable mezclados --------------------------

def test_h_bloqueo_absoluto_mezclado_con_overridable_deniega_siempre(monkeypatch):
    decision = _evaluar(
        monkeypatch,
        gestionar=True, sobrecupo=True,
        sobrecupo_solicitado=True,
        sobrecupo_motivo="Motivo válido",
        resultado_disponibilidad=_resultado(
            disponible=False, motivo="excede_cierre_centro",
            conflictos=(_FUERA_DE_JORNADA, _EXCEDE_CIERRE),
        ),
    )
    assert decision.permitido is False
    assert decision.es_sobrecupo_efectivo is False
    assert decision.codigo == CODIGO_DENEGADO_BLOQUEO_ABSOLUTO


# --- J: SUPERADMIN sin permiso efectivo no tiene bypass -----------

def test_j_superadmin_sin_permiso_efectivo_no_bypass(monkeypatch):
    """rol=superadmin en current_user NO cambia el resultado: la
    política solo confía en tiene_permiso_efectivo(), acá forzado a
    False para agenda.sobrecupo — nunca `if rol == 'superadmin'`."""
    _monkeypatch_permisos(monkeypatch, gestionar=True, sobrecupo=False)
    decision = evaluar_politica_sobrecupo(
        db=None,
        current_user={"id": 1, "rol": "superadmin"},
        sobrecupo_solicitado=True,
        sobrecupo_motivo="Motivo válido",
        resultado_disponibilidad=_resultado(
            disponible=False, motivo="fuera_de_jornada",
            conflictos=(_FUERA_DE_JORNADA,),
        ),
    )
    assert decision.permitido is False
    assert decision.codigo == CODIGO_DENEGADO_SIN_PERMISO


# --- K: ADMIN autorizado ------------------------------------------

def test_k_admin_autorizado(monkeypatch):
    decision = _evaluar(
        monkeypatch,
        gestionar=True, sobrecupo=True,
        sobrecupo_solicitado=True,
        sobrecupo_motivo="Motivo válido",
        resultado_disponibilidad=_resultado(
            disponible=False, motivo="en_colacion",
            conflictos=(_EN_COLACION,),
        ),
    )
    assert decision.permitido is True
    assert decision.codigo == CODIGO_AUTORIZADO


# --- precondición legacy vs entrada incoherente -------------------

def test_precondicion_terminal_es_legacy_no_incoherente(monkeypatch):
    """disponible=False + conflictos=() (profesional_inactivo,
    fecha/hora inválida, etc.) es un rechazo legacy legítimo — jamás
    'entrada_incoherente', aunque gestionar/sobrecupo estén
    concedidos."""
    decision = _evaluar(
        monkeypatch,
        gestionar=True, sobrecupo=True,
        sobrecupo_solicitado=True,
        sobrecupo_motivo="Motivo válido",
        resultado_disponibilidad=_resultado(
            disponible=False, motivo="profesional_inactivo", conflictos=(),
        ),
    )
    assert decision.permitido is False
    assert decision.codigo == CODIGO_DENEGADO_PRECONDICION_LEGACY


def test_entrada_incoherente_es_fail_closed_defensivo(monkeypatch):
    """Estado imposible en la práctica (disponible=True con
    conflictos no vacíos, que viola el invariante de
    ResultadoDisponibilidad) — fail-closed explícito, nunca se
    interpreta como "normal"."""
    decision = _evaluar(
        monkeypatch,
        sobrecupo_solicitado=False,
        resultado_disponibilidad=_resultado(
            disponible=True, conflictos=(_FUERA_DE_JORNADA,),
        ),
    )
    assert decision.permitido is False
    assert decision.codigo == CODIGO_DENEGADO_ENTRADA_INCOHERENTE


# --- estudiante/profesional sin agenda.gestionar: sin 403 granular -

def test_sin_agenda_gestionar_no_recibe_el_403_granular(monkeypatch):
    """Un actor SIN agenda.gestionar (estudiante en autoservicio,
    profesional) que de todas formas manda sobrecupo=True sobre un
    conflicto real recibe el MISMO código legacy que sin intención —
    nunca CODIGO_DENEGADO_SIN_PERMISO, que revelaría la existencia de
    agenda.sobrecupo a quien no tiene ninguna capacidad administrativa."""
    decision = _evaluar(
        monkeypatch,
        gestionar=False, sobrecupo=True,  # sobrecupo=True es irrelevante: nunca se consulta
        sobrecupo_solicitado=True,
        sobrecupo_motivo="Motivo cualquiera",
        resultado_disponibilidad=_resultado(
            disponible=False, motivo="fuera_de_jornada",
            conflictos=(_FUERA_DE_JORNADA,),
        ),
    )
    assert decision.permitido is False
    assert decision.codigo == CODIGO_DENEGADO_CONFLICTO_SIN_INTENCION


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


def _dia_habil_futuro(dias_calendario: int = 1) -> str:
    fecha = date.today() + timedelta(days=dias_calendario)
    while fecha.weekday() >= 5:
        fecha += timedelta(days=1)
    return fecha.isoformat()


def _profesional(db, *, horario_inicio="09:00", horario_fin="17:00", duracion_min=30, estado="activo"):
    prof = Profesional(
        nombre="Profesional Test A4.3", especialidad="Nutrición", iniciales="PA43",
        estado=estado, horario_inicio=horario_inicio, horario_fin=horario_fin,
        duracion_min=duracion_min,
    )
    db.add(prof)
    db.flush()
    return prof


def _usuario(db, *, correo, rol, rut="a43-rut"):
    usuario = Usuario(correo=correo, password="hash-a43", rol=rol, nombre="Usuario Test A4.3", rut=rut, activo=True)
    db.add(usuario)
    db.flush()
    return usuario


def _current_user(usuario):
    return {"id": usuario.id, "rol": usuario.rol}


def _monkeypatch_router_permisos(monkeypatch, *, gestionar: bool, sobrecupo: bool):
    """Mismo criterio explícito que la parte unitaria, pero patchea
    AMBOS namespaces (citas y sobrecupo_policy_service) — necesario
    porque cada uno importó su propia referencia a
    tiene_permiso_efectivo."""
    def _resolver(db, current_user, permiso):
        if permiso == Permission.AGENDA_GESTIONAR:
            return gestionar
        if permiso == Permission.AGENDA_SOBRECUPO:
            return sobrecupo
        raise AssertionError(f"permiso no contemplado por este test: {permiso!r}")

    for modulo in (citas, sobrecupo_policy_service):
        monkeypatch.setattr(modulo, "tiene_permiso_efectivo", _resolver)
    monkeypatch.setattr(
        citas, "obtener_alcance_administrativo_efectivo",
        lambda db, u: AlcanceAdministrativoEfectivo(
            institucional=True, especialidades_normalizadas=frozenset(),
        ),
    )


def test_i_slot_ocupado_capacidad_agotada_sigue_409_pese_a_sobrecupo_permiso_y_motivo(db_session, monkeypatch):
    """A.4.4 — con la capacidad máxima ya alcanzada (2 citas activas
    coexistiendo: 1 normal + 1 sobrecupo previo), slot_ocupado vuelve
    a ser absoluto: ni permiso ni motivo lo superan. Antes de A.4.4,
    UNA sola cita existente ya bastaba para este rechazo — con
    exactamente 1 cita existente, este mismo escenario ahora SÍ se
    autoriza (ver test_a4_4_sobrecupo_slot_ocupado.py), así que este
    test se ajusta a 2 citas existentes para seguir probando lo que
    realmente le importa: que el límite de capacidad no se puede
    saltar con permisos ni motivo."""
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    otro_paciente = _usuario(db_session, correo="ocupante-a43@sesaes.cl", rol="estudiante", rut="a43-1")
    otro_paciente_2 = _usuario(db_session, correo="ocupante-a43-2@sesaes.cl", rol="estudiante", rut="a43-1b")
    db_session.add(Cita(
        estudiante_id=otro_paciente.id, profesional_id=prof.id,
        fecha=fecha, hora="09:00", estado="pendiente",
    ))
    db_session.add(Cita(
        estudiante_id=otro_paciente_2.id, profesional_id=prof.id,
        fecha=fecha, hora="09:00", estado="pendiente", sobrecupo=True,
    ))
    paciente = _usuario(db_session, correo="paciente-a43-ocupado@sesaes.cl", rol="estudiante", rut="a43-2")
    admin_user = _usuario(db_session, correo="admin-a43-ocupado@sesaes.cl", rol="admin", rut="a43-3")
    db_session.commit()

    _monkeypatch_router_permisos(monkeypatch, gestionar=True, sobrecupo=True)

    payload = CitaCreate(
        estudiante_id=paciente.id, profesional_id=prof.id, fecha=fecha, hora="09:00",
        sobrecupo=True, sobrecupo_motivo="Motivo válido",
    )
    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_user))

    assert exc.value.status_code == 409
    assert db_session.query(Cita).filter(Cita.estudiante_id == paciente.id).count() == 0
    assert db_session.query(CitaSobrecupo).count() == 0


def test_estudiante_self_booking_conflicto_sobrecupo_true_es_rechazo_legacy(db_session, monkeypatch):
    """El estudiante NUNCA tiene agenda.gestionar en este entorno real
    (ROLE_DEFAULT_PERMISSIONS no se lo da) — no hace falta monkeypatch
    para simular esto, basta con no parchear tiene_permiso_efectivo y
    dejar que la resolución real (sin AccesoAdminPermiso para nadie)
    corra. Debe recibir el 400 legacy, NUNCA el 403 granular."""
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    est = _usuario(db_session, correo="estudiante-selfbooking-a43@sesaes.cl", rol="estudiante", rut="a43-4")
    db_session.commit()

    payload = CitaCreate(
        estudiante_id=est.id, profesional_id=prof.id, fecha=fecha, hora="17:00",  # fuera de jornada
        sobrecupo=True, sobrecupo_motivo="Motivo cualquiera",
    )
    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(est))

    assert exc.value.status_code == 400
    assert "sobrecupo" not in (exc.value.detail or "").lower()


def test_profesional_sin_agenda_gestionar_es_rechazo_legacy(db_session, monkeypatch):
    """Un actor con rol=profesional nunca pasa siquiera la
    verificación de propietario/agenda previa (solo estudiante-dueño o
    agenda.gestionar pueden) — recibe el 403 legacy de esa capa, que
    ya existía antes de A.4.3 y no menciona agenda.sobrecupo en
    absoluto. La política de sobrecupo nunca llega a evaluarse."""
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    est = _usuario(db_session, correo="paciente-prof-a43@sesaes.cl", rol="estudiante", rut="a43-5")
    profesional_actor = _usuario(db_session, correo="profesional-actor-a43@sesaes.cl", rol="profesional", rut="a43-6")
    db_session.commit()

    payload = CitaCreate(
        estudiante_id=est.id, profesional_id=prof.id, fecha=fecha, hora="17:00",
        sobrecupo=True, sobrecupo_motivo="Motivo cualquiera",
    )
    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(profesional_actor))

    assert exc.value.status_code == 403
    assert "agenda.sobrecupo" not in (exc.value.detail or "").lower()


def test_admin_con_gestionar_sin_sobrecupo_recibe_403_especifico(db_session, monkeypatch):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    est = _usuario(db_session, correo="paciente-403-a43@sesaes.cl", rol="estudiante", rut="a43-7")
    admin_user = _usuario(db_session, correo="admin-403-a43@sesaes.cl", rol="admin", rut="a43-8")
    db_session.commit()

    _monkeypatch_router_permisos(monkeypatch, gestionar=True, sobrecupo=False)

    payload = CitaCreate(
        estudiante_id=est.id, profesional_id=prof.id, fecha=fecha, hora="17:00",
        sobrecupo=True, sobrecupo_motivo="Motivo válido",
    )
    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_user))

    assert exc.value.status_code == 403
    assert "agenda.sobrecupo" in exc.value.detail
    assert db_session.query(CitaSobrecupo).count() == 0


def test_admin_con_gestionar_y_sobrecupo_pero_sin_motivo_recibe_400(db_session, monkeypatch):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    est = _usuario(db_session, correo="paciente-400motivo-a43@sesaes.cl", rol="estudiante", rut="a43-9")
    admin_user = _usuario(db_session, correo="admin-400motivo-a43@sesaes.cl", rol="admin", rut="a43-10")
    db_session.commit()

    _monkeypatch_router_permisos(monkeypatch, gestionar=True, sobrecupo=True)

    payload = CitaCreate(
        estudiante_id=est.id, profesional_id=prof.id, fecha=fecha, hora="17:00",
        sobrecupo=True, sobrecupo_motivo="   ",
    )
    with pytest.raises(HTTPException) as exc:
        citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_user))

    assert exc.value.status_code == 400
    assert exc.value.detail == "Debes indicar el motivo del sobrecupo."
    assert db_session.query(CitaSobrecupo).count() == 0


def test_slot_libre_sobrecupo_true_sin_permisos_crea_cita_normal(db_session, monkeypatch):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="17:00")
    est = _usuario(db_session, correo="paciente-libre-sinperm-a43@sesaes.cl", rol="estudiante", rut="a43-11")
    db_session.commit()

    payload = CitaCreate(
        estudiante_id=est.id, profesional_id=prof.id, fecha=fecha, hora="10:00",
        sobrecupo=True,
    )
    citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(est))

    creada = db_session.query(Cita).one()
    assert creada.sobrecupo is False
    assert db_session.query(CitaSobrecupo).count() == 0


def test_slot_libre_sobrecupo_true_motivo_vacio_crea_cita_normal(db_session, monkeypatch):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="17:00")
    est = _usuario(db_session, correo="paciente-libre-sinmotivo-a43@sesaes.cl", rol="estudiante", rut="a43-12")
    admin_user = _usuario(db_session, correo="admin-libre-sinmotivo-a43@sesaes.cl", rol="admin", rut="a43-13")
    db_session.commit()

    _monkeypatch_router_permisos(monkeypatch, gestionar=True, sobrecupo=True)

    payload = CitaCreate(
        estudiante_id=est.id, profesional_id=prof.id, fecha=fecha, hora="10:00",
        sobrecupo=True, sobrecupo_motivo="",
    )
    citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_user))

    creada = db_session.query(Cita).one()
    assert creada.sobrecupo is False
    assert db_session.query(CitaSobrecupo).count() == 0


# --- L: auditoría solo en sobrecupo efectivo ----------------------

def test_l_auditoria_solo_se_genera_para_sobrecupo_efectivo(db_session, monkeypatch):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="17:00")
    est_libre = _usuario(db_session, correo="paciente-auditoria-l-libre-a43@sesaes.cl", rol="estudiante", rut="a43-14")
    est_efectivo = _usuario(db_session, correo="paciente-auditoria-l-efectivo-a43@sesaes.cl", rol="estudiante", rut="a43-14b")
    admin_user = _usuario(db_session, correo="admin-auditoria-l-a43@sesaes.cl", rol="admin", rut="a43-15")
    db_session.commit()

    _monkeypatch_router_permisos(monkeypatch, gestionar=True, sobrecupo=True)

    # (1) flag sobrante — slot realmente libre: NO debe auditar sobrecupo.
    payload_libre = CitaCreate(
        estudiante_id=est_libre.id, profesional_id=prof.id, fecha=fecha, hora="10:00",
        sobrecupo=True, sobrecupo_motivo="Motivo válido",
    )
    citas.crear_cita(cita=payload_libre, db=db_session, current_user=_current_user(admin_user))
    assert db_session.query(Auditoria).filter(Auditoria.accion == "Creó cita con sobrecupo").count() == 0

    # (2) sobrecupo efectivo — SÍ debe auditar, exactamente una vez.
    # Estudiante DISTINTO al de (1), para no chocar con la regla
    # (ajena a A.4.3) de "una cita pendiente por especialidad".
    payload_efectivo = CitaCreate(
        estudiante_id=est_efectivo.id, profesional_id=prof.id, fecha=fecha, hora="17:00",
        sobrecupo=True, sobrecupo_motivo="Motivo válido",
    )
    citas.crear_cita(cita=payload_efectivo, db=db_session, current_user=_current_user(admin_user))
    eventos = db_session.query(Auditoria).filter(Auditoria.accion == "Creó cita con sobrecupo").all()
    assert len(eventos) == 1
    assert eventos[0].usuario_id == admin_user.id


# --- M: motivo normalizado persistido -----------------------------

def test_m_motivo_normalizado_persistido_exacto(db_session, monkeypatch):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="17:00")
    est = _usuario(db_session, correo="paciente-motivo-m-a43@sesaes.cl", rol="estudiante", rut="a43-16")
    admin_user = _usuario(db_session, correo="admin-motivo-m-a43@sesaes.cl", rol="admin", rut="a43-17")
    db_session.commit()

    _monkeypatch_router_permisos(monkeypatch, gestionar=True, sobrecupo=True)

    payload = CitaCreate(
        estudiante_id=est.id, profesional_id=prof.id, fecha=fecha, hora="17:00",
        sobrecupo=True, sobrecupo_motivo="  Motivo válido  ",
    )
    citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_user))

    creada = db_session.query(Cita).one()
    detalle = db_session.query(CitaSobrecupo).filter(CitaSobrecupo.cita_id == creada.id).one()
    assert detalle.motivo == "Motivo válido"


# --- N: rollback atómico -------------------------------------------

def test_n_falla_tardia_no_deja_metadata_parcial(db_session, monkeypatch):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="17:00")
    est = _usuario(db_session, correo="paciente-rollback-a43@sesaes.cl", rol="estudiante", rut="a43-18")
    admin_user = _usuario(db_session, correo="admin-rollback-a43@sesaes.cl", rol="admin", rut="a43-19")
    db_session.commit()

    _monkeypatch_router_permisos(monkeypatch, gestionar=True, sobrecupo=True)
    monkeypatch.setattr(
        citas, "registrar_evento_auditoria",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("falla simulada tardía")),
    )

    payload = CitaCreate(
        estudiante_id=est.id, profesional_id=prof.id, fecha=fecha, hora="17:00",
        sobrecupo=True, sobrecupo_motivo="Motivo válido",
    )
    with pytest.raises(RuntimeError):
        citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_user))
    db_session.rollback()

    assert db_session.query(Cita).count() == 0
    assert db_session.query(CitaSobrecupo).count() == 0
    assert db_session.query(CitaSobrecupoConflicto).count() == 0
