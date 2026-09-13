# -*- coding: utf-8 -*-
"""
SESAES — A.4.1: trazabilidad de origen de Cita + persistencia
normalizada de metadata de sobrecupo + auditoría de sobrecupo que
antes faltaba.

Cubre:
  - Cita.creado_por_usuario_id / creado_por_rol / creado_por_perfil,
    poblados por app.services.cita_origen_service.resolver_origen_cita
    en POST /citas y POST /admin/citas/urgente, SIEMPRE desde
    current_user (nunca desde el body).
  - CitaSobrecupo / CitaSobrecupoConflicto: solo se crean cuando
    sobrecupo=True fue efectivamente autorizado (un motivo real
    overridable_con_sobrecupo fue superado) — nunca para una cita
    normal, y nunca inventando un conflicto que no ocurrió.
  - El evento de auditoría que POST /citas no generaba para sobrecupo
    (hallazgo confirmado en el diagnóstico A.4).
  - Que las filas "históricas" (creado_por_* / fecha_creacion NULL,
    sin CitaSobrecupo) siguen siendo válidas de leer.
  - Que registrar los nuevos modelos no rompe la configuración de
    mappers de SQLAlchemy (regresión explícita del incidente
    "SolicitudHorario failed to locate a name").

No repite aquí lo que ya cubren test_a3_concurrencia.py (orden
lock -> re-evaluación, 409 vs sobrecupo) ni los tests de A.2/A.2B/A.2C
(qué motivos existen, cuáles son overridable) — A.4.1 no cambia nada
de eso.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, configure_mappers

import app.models.init  # noqa: F401 — registra TODOS los modelos productivos
import app.models.solicitud_horario  # noqa: F401 — igual que test_a3_concurrencia.py

from app.database import Base
from app.models.acceso_administrativo import AccesoAdministrativo
from app.models.auditoria import Auditoria
from app.models.cita import Cita
from app.models.cita_sobrecupo import CitaSobrecupo, CitaSobrecupoConflicto
from app.models.profesional import Profesional
from app.models.usuario import Usuario
from app.rbac.admin_authorization import (
    AlcanceAdministrativoEfectivo,
    resolver_perfil_administrativo_snapshot,
)
from app.routers import admin, citas
from app.schemas import CitaCreate


# ══════════════════════════════════════════════════════════
# Fixtures / helpers (mismo estilo que test_a3_concurrencia.py)
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


def _profesional(
    db,
    *,
    horario_inicio="09:00",
    horario_fin="17:00",
    hora_almuerzo_inicio=None,
    hora_almuerzo_fin=None,
    duracion_min=45,
    estado="activo",
):
    prof = Profesional(
        nombre="Profesional Test A4.1",
        especialidad="Nutrición",
        iniciales="PA4",
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


def _usuario(db, *, correo, rol, rut="a41-rut"):
    usuario = Usuario(
        correo=correo, password="hash-a41", rol=rol,
        nombre="Usuario Test A4.1", rut=rut, activo=True,
    )
    db.add(usuario)
    db.flush()
    return usuario


def _acceso_administrativo(
    db, usuario, *, perfil="administrador_general", tipo_alcance="institucional",
):
    acceso = AccesoAdministrativo(
        usuario_id=usuario.id, perfil=perfil, tipo_alcance=tipo_alcance,
    )
    db.add(acceso)
    db.flush()
    return acceso


def _current_user(usuario, *, rol=None):
    return {"id": usuario.id, "rol": rol or usuario.rol}


def _monkeypatch_admin_institucional(monkeypatch, modulo):
    """Alcance institucional total — idéntico a
    test_a3_concurrencia.py: la resolución RBAC/alcance en sí ya está
    cubierta por otras suites, acá solo nos importa que el actor
    pueda crear la cita para poder observar la trazabilidad."""
    if hasattr(modulo, "tiene_permiso_efectivo"):
        monkeypatch.setattr(modulo, "tiene_permiso_efectivo", lambda db, u, p: True)
    monkeypatch.setattr(
        modulo,
        "obtener_alcance_administrativo_efectivo",
        lambda db, u: AlcanceAdministrativoEfectivo(
            institucional=True, especialidades_normalizadas=frozenset(),
        ),
    )


# ══════════════════════════════════════════════════════════
# 1) estudiante crea cita: creado_por_usuario_id / rol correctos
# ══════════════════════════════════════════════════════════

def test_estudiante_crea_cita_registra_su_propio_origen(db_session):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    est = _usuario(db_session, correo="est-origen@sesaes.cl", rol="estudiante", rut="a41-1")
    db_session.commit()

    payload = CitaCreate(estudiante_id=est.id, profesional_id=prof.id, fecha=fecha, hora="09:30")
    citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(est))

    creada = db_session.query(Cita).one()
    assert creada.creado_por_usuario_id == est.id
    assert creada.creado_por_rol == "estudiante"
    assert creada.creado_por_perfil is None


# ══════════════════════════════════════════════════════════
# 2) ADMIN crea cita para OTRO estudiante: actor != paciente
# ══════════════════════════════════════════════════════════

def test_admin_crea_cita_para_otro_estudiante_registra_al_admin_no_al_paciente(
    db_session, monkeypatch,
):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    paciente = _usuario(db_session, correo="paciente-a41@sesaes.cl", rol="estudiante", rut="a41-2")
    admin_user = _usuario(db_session, correo="admin-a41@sesaes.cl", rol="admin", rut="a41-3")
    _acceso_administrativo(db_session, admin_user, perfil="administrador_general")
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, citas)

    payload = CitaCreate(estudiante_id=paciente.id, profesional_id=prof.id, fecha=fecha, hora="10:15")
    citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_user))

    creada = db_session.query(Cita).one()
    assert creada.estudiante_id == paciente.id
    assert creada.creado_por_usuario_id == admin_user.id
    assert creada.creado_por_usuario_id != paciente.id
    assert creada.creado_por_rol == "admin"
    assert creada.creado_por_perfil == "administrador_general"


# ══════════════════════════════════════════════════════════
# 3) perfil secretaría: creado_por_perfil correcto
# ══════════════════════════════════════════════════════════

def test_perfil_secretaria_general_queda_registrado_como_creador(db_session, monkeypatch):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    paciente = _usuario(db_session, correo="paciente-secretaria@sesaes.cl", rol="estudiante", rut="a41-4")
    secretaria = _usuario(db_session, correo="secretaria-a41@sesaes.cl", rol="admin", rut="a41-5")
    _acceso_administrativo(db_session, secretaria, perfil="secretaria_general")
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, citas)

    payload = CitaCreate(estudiante_id=paciente.id, profesional_id=prof.id, fecha=fecha, hora="10:15")
    citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(secretaria))

    creada = db_session.query(Cita).one()
    assert creada.creado_por_usuario_id == secretaria.id
    assert creada.creado_por_rol == "admin"
    assert creada.creado_por_perfil == "secretaria_general"


# ══════════════════════════════════════════════════════════
# 4) SUPERADMIN: origen correcto, perfil NULL
# ══════════════════════════════════════════════════════════

def test_superadmin_crea_cita_perfil_queda_null(db_session, monkeypatch):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    paciente = _usuario(db_session, correo="paciente-superadmin@sesaes.cl", rol="estudiante", rut="a41-6")
    superadmin = _usuario(db_session, correo="superadmin-a41@sesaes.cl", rol="superadmin", rut="a41-7")
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, citas)

    payload = CitaCreate(estudiante_id=paciente.id, profesional_id=prof.id, fecha=fecha, hora="11:00")
    citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(superadmin))

    creada = db_session.query(Cita).one()
    assert creada.creado_por_usuario_id == superadmin.id
    assert creada.creado_por_rol == "superadmin"
    # SUPERADMIN no depende de AccesoAdministrativo (ver
    # admin_authorization.py): no hay perfil real que reportar, así
    # que NUNCA se inventa uno — queda NULL, honestamente.
    assert creada.creado_por_perfil is None


# ══════════════════════════════════════════════════════════
# 4b) A.4.1 v2 — guard EXPLÍCITO por rol: aunque exista, por datos
# legacy/stale, una fila AccesoAdministrativo asociada a un usuario
# que YA NO es ADMIN, ese usuario NUNCA debe recibir un
# creado_por_perfil. Se prueba directamente contra el helper (más
# aislado y explícito que forzar el caso vía el endpoint completo).
# ══════════════════════════════════════════════════════════

def test_perfil_snapshot_ignora_acceso_administrativo_stale_de_no_admin(db_session):
    superadmin = _usuario(
        db_session, correo="superadmin-stale-a41@sesaes.cl", rol="superadmin", rut="a41-21",
    )
    # Fila AccesoAdministrativo "residual": simula un usuario que fue
    # ADMIN en el pasado (o un dato corrupto/legacy) y hoy tiene otro
    # rol base. La infraestructura real (obtener_contexto_admin) ya
    # filtraría esto por rol, pero A.4.1 v2 agrega un guard EXPLÍCITO
    # en resolver_perfil_administrativo_snapshot() que no depende de
    # esa implementación interna — este test lo ejercita directamente.
    _acceso_administrativo(db_session, superadmin, perfil="administrador_general")
    db_session.commit()

    perfil = resolver_perfil_administrativo_snapshot(
        db_session, _current_user(superadmin),
    )
    assert perfil is None


def test_perfil_snapshot_es_none_para_estudiante(db_session):
    est = _usuario(db_session, correo="est-snapshot-a41@sesaes.cl", rol="estudiante", rut="a41-22")
    db_session.commit()
    assert resolver_perfil_administrativo_snapshot(db_session, _current_user(est)) is None


def test_perfil_snapshot_es_none_para_profesional_aunque_current_user_diga_admin(db_session):
    """Caso límite explícito del guard: si current_user["rol"] NO es
    exactamente "admin" (aunque el usuario real en BD sea profesional
    y por error alguien pasara un dict con otro valor), el guard debe
    cortar antes de tocar AccesoAdministrativo — no se confía en el
    contenido de la fila BD para decidir esto, se confía en el rol que
    trae el propio current_user ya resuelto."""
    prof_usuario = _usuario(
        db_session, correo="prof-snapshot-a41@sesaes.cl", rol="profesional", rut="a41-23",
    )
    db_session.commit()
    perfil = resolver_perfil_administrativo_snapshot(
        db_session, {"id": prof_usuario.id, "rol": "profesional"},
    )
    assert perfil is None


# ══════════════════════════════════════════════════════════
# 5) urgente también guarda creador
# ══════════════════════════════════════════════════════════

def test_cita_urgente_tambien_registra_origen(db_session, monkeypatch):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    paciente = _usuario(db_session, correo="paciente-urgente-a41@sesaes.cl", rol="estudiante", rut="a41-8")
    admin_user = _usuario(db_session, correo="admin-urgente-a41@sesaes.cl", rol="admin", rut="a41-9")
    _acceso_administrativo(db_session, admin_user, perfil="administrador_general")
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, admin)

    payload = CitaCreate(estudiante_id=paciente.id, profesional_id=prof.id, fecha=fecha, hora="09:00")
    admin.crear_cita_urgente(cita=payload, db=db_session, current_user=_current_user(admin_user))

    creada = db_session.query(Cita).filter(Cita.urgente.is_(True)).one()
    assert creada.creado_por_usuario_id == admin_user.id
    assert creada.creado_por_rol == "admin"
    assert creada.creado_por_perfil == "administrador_general"
    # A.4.1 no toca nada más de este endpoint: sigue sin generar
    # CitaSobrecupo — ver también test #8 (cita normal) y la nota en
    # el diagnóstico A.4 sobre por qué urgente/sobrecupo siguen
    # separados en esta fase.
    assert db_session.query(CitaSobrecupo).count() == 0


# ══════════════════════════════════════════════════════════
# 6) y 7) sobrecupo legacy: en_colacion / fuera_de_jornada
# ══════════════════════════════════════════════════════════

def test_sobrecupo_en_colacion_crea_detalle_y_conflicto_estructurado(db_session, monkeypatch):
    fecha = _dia_habil_futuro()
    prof = _profesional(
        db_session,
        horario_inicio="09:00", horario_fin="17:00",
        hora_almuerzo_inicio="13:00", hora_almuerzo_fin="14:00",
        duracion_min=30,
    )
    paciente = _usuario(db_session, correo="paciente-colacion-a41@sesaes.cl", rol="estudiante", rut="a41-10")
    admin_user = _usuario(db_session, correo="admin-colacion-a41@sesaes.cl", rol="admin", rut="a41-11")
    _acceso_administrativo(db_session, admin_user, perfil="administrador_general")
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
    assert detalle.estado_revision is None  # A.4.1 no implementa aprobación todavía
    conflictos = (
        db_session.query(CitaSobrecupoConflicto)
        .filter(CitaSobrecupoConflicto.cita_sobrecupo_id == detalle.id)
        .all()
    )
    assert [c.codigo for c in conflictos] == ["en_colacion"]


def test_sobrecupo_fuera_de_jornada_crea_detalle_y_conflicto_estructurado(db_session, monkeypatch):
    fecha = _dia_habil_futuro()
    prof = _profesional(
        db_session,
        horario_inicio="09:00", horario_fin="17:00",
        duracion_min=30,
    )
    paciente = _usuario(db_session, correo="paciente-jornada-a41@sesaes.cl", rol="estudiante", rut="a41-12")
    admin_user = _usuario(db_session, correo="admin-jornada-a41@sesaes.cl", rol="admin", rut="a41-13")
    _acceso_administrativo(db_session, admin_user, perfil="administrador_general")
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, citas)

    payload = CitaCreate(
        estudiante_id=paciente.id, profesional_id=prof.id, fecha=fecha, hora="17:00",
        sobrecupo=True,
    )
    citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_user))

    creada = db_session.query(Cita).one()
    detalle = db_session.query(CitaSobrecupo).filter(CitaSobrecupo.cita_id == creada.id).one()
    conflictos = (
        db_session.query(CitaSobrecupoConflicto)
        .filter(CitaSobrecupoConflicto.cita_sobrecupo_id == detalle.id)
        .all()
    )
    assert [c.codigo for c in conflictos] == ["fuera_de_jornada"]


# ══════════════════════════════════════════════════════════
# 8) cita normal NO crea CitaSobrecupo
# ══════════════════════════════════════════════════════════

def test_cita_normal_no_crea_detalle_de_sobrecupo(db_session):
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    est = _usuario(db_session, correo="est-normal-a41@sesaes.cl", rol="estudiante", rut="a41-14")
    db_session.commit()

    payload = CitaCreate(estudiante_id=est.id, profesional_id=prof.id, fecha=fecha, hora="09:30")
    citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(est))

    assert db_session.query(CitaSobrecupo).count() == 0
    assert db_session.query(CitaSobrecupoConflicto).count() == 0


def test_sobrecupo_marcado_sobre_slot_realmente_libre_no_inventa_conflicto(db_session, monkeypatch):
    """A.4.1 v2 — corrección del 'sobrecupo fantasma' de v1: si alguien
    llama a la API con sobrecupo=True sobre un horario que, tras la
    re-evaluación DENTRO del lock A.3, resulta realmente disponible
    (nunca hubo nada que superar — p. ej. porque la cita que antes
    bloqueaba ese slot fue cancelada mientras tanto), la cita se crea
    IGUAL (A.3 nunca rechaza una cita solo porque el conflicto
    desapareció: eso es una mejora legítima de concurrencia), pero
    Cita.sobrecupo debe reflejar sobrecupo EFECTIVO, no la intención
    cruda del cliente — v1 dejaba sobrecupo=True sin ningún
    CitaSobrecupo/conflicto/auditoría que lo respaldara (dos fuentes
    de verdad contradictorias); v2 lo corrige a False."""
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session, horario_inicio="09:00", horario_fin="17:00")
    paciente = _usuario(db_session, correo="paciente-libre-a41@sesaes.cl", rol="estudiante", rut="a41-15")
    admin_user = _usuario(db_session, correo="admin-libre-a41@sesaes.cl", rol="admin", rut="a41-16")
    _acceso_administrativo(db_session, admin_user, perfil="administrador_general")
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, citas)

    payload = CitaCreate(
        estudiante_id=paciente.id, profesional_id=prof.id, fecha=fecha, hora="10:15",
        sobrecupo=True,
    )
    citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_user))

    creada = db_session.query(Cita).one()
    # NO se rechaza la cita solo porque el conflicto que motivaba el
    # sobrecupo ya no existe — se crea normalmente.
    assert creada.estado == "pendiente"
    # Pero la marca persistida es "sobrecupo efectivo", no la
    # intención cruda: acá no hubo ningún conflicto real que superar.
    assert creada.sobrecupo is False
    assert db_session.query(CitaSobrecupo).count() == 0
    assert db_session.query(CitaSobrecupoConflicto).count() == 0
    assert (
        db_session.query(Auditoria)
        .filter(Auditoria.accion == "Creó cita con sobrecupo")
        .count()
    ) == 0


# ══════════════════════════════════════════════════════════
# 9) y 10) auditoría de sobrecupo, actor viene de current_user
# ══════════════════════════════════════════════════════════

def test_sobrecupo_genera_evento_de_auditoria_con_actor_de_current_user(db_session, monkeypatch):
    fecha = _dia_habil_futuro()
    prof = _profesional(
        db_session,
        horario_inicio="09:00", horario_fin="17:00",
        hora_almuerzo_inicio="13:00", hora_almuerzo_fin="14:00",
        duracion_min=30,
    )
    paciente = _usuario(db_session, correo="paciente-auditoria-a41@sesaes.cl", rol="estudiante", rut="a41-17")
    admin_user = _usuario(db_session, correo="admin-auditoria-a41@sesaes.cl", rol="admin", rut="a41-18")
    _acceso_administrativo(db_session, admin_user, perfil="administrador_general")
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, citas)

    payload = CitaCreate(
        estudiante_id=paciente.id, profesional_id=prof.id, fecha=fecha, hora="13:00",
        sobrecupo=True, sobrecupo_motivo="Paciente con turno de práctica que termina justo antes",
    )
    citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_user))

    creada = db_session.query(Cita).one()
    eventos = db_session.query(Auditoria).filter(Auditoria.entidad_id == creada.id).all()
    assert len(eventos) == 1
    evento = eventos[0]

    # El actor del evento es SIEMPRE current_user, NUNCA el paciente
    # de la cita (que puede ser una persona completamente distinta).
    assert evento.usuario_id == admin_user.id
    assert evento.usuario_id != paciente.id
    assert evento.actor_rol == "admin"
    assert evento.resultado == "exito"
    assert evento.entidad == "cita"
    assert "en_colacion" in evento.detalle
    assert "turno de práctica" in evento.detalle


def test_cita_normal_no_genera_evento_de_auditoria(db_session):
    """A.4.1 corrige puntualmente el hueco de sobrecupo — no amplía el
    alcance a auditar toda cita normal (fuera de alcance del ticket)."""
    fecha = _dia_habil_futuro()
    prof = _profesional(db_session)
    est = _usuario(db_session, correo="est-sinauditoria-a41@sesaes.cl", rol="estudiante", rut="a41-19")
    db_session.commit()

    payload = CitaCreate(estudiante_id=est.id, profesional_id=prof.id, fecha=fecha, hora="09:30")
    citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(est))

    assert db_session.query(Auditoria).count() == 0


# ══════════════════════════════════════════════════════════
# 11) filas históricas NULL siguen siendo válidas
# ══════════════════════════════════════════════════════════

def test_fila_historica_sin_columnas_a41_sigue_siendo_legible(db_session):
    """Simula una Cita creada ANTES de A.4.1: ninguna de las columnas
    nuevas se fija explícitamente. Deben quedar en NULL y la fila debe
    seguir siendo perfectamente consultable — nada de A.4.1 exige que
    existan valores para leer una cita vieja."""
    prof = _profesional(db_session)
    est = _usuario(db_session, correo="est-historica-a41@sesaes.cl", rol="estudiante", rut="a41-20")
    historica = Cita(
        estudiante_id=est.id, profesional_id=prof.id,
        fecha="2020-01-15", hora="09:00 AM", estado="completada",
    )
    db_session.add(historica)
    db_session.commit()
    db_session.refresh(historica)

    releida = db_session.query(Cita).filter(Cita.id == historica.id).one()
    assert releida.creado_por_usuario_id is None
    assert releida.creado_por_rol is None
    assert releida.creado_por_perfil is None
    # fecha_creacion SÍ recibe el DEFAULT del motor al insertar en un
    # esquema fresco (create_all/server_default) — el caso realmente
    # NULL en producción es el de una fila que YA existía en la BD
    # ANTES de que la columna se agregara (ver migración), que no se
    # puede recrear insertando en un esquema nuevo. Lo que sí importa
    # acá, y es lo que este test verifica, es que ninguna columna
    # nueva es NOT NULL a nivel de esquema — de lo contrario ni
    # siquiera este INSERT (sin fijarlas) habría podido completarse.
    assert releida.sobrecupo_detalle is None


# ══════════════════════════════════════════════════════════
# 6) transacción: Cita + CitaSobrecupo + Conflicto + Auditoria se
# confirman o se deshacen juntos — sin gestor transaccional nuevo,
# apoyado en la garantía que ya da la sesión de SQLAlchemy (ningún
# commit() intermedio entre el flush de Cita y el commit final).
# ══════════════════════════════════════════════════════════

def test_falla_tardia_en_sobrecupo_no_deja_metadata_parcial(db_session, monkeypatch):
    """Si algo falla DESPUÉS de insertar Cita/CitaSobrecupo/Conflicto
    pero ANTES del commit final (p. ej. registrar_evento_auditoria
    lanza), nada de eso debe quedar persistido: no hay ningún
    db.commit() intermedio en citas.crear_cita entre el flush de Cita
    y el commit único al final — así que un rollback() deshace TODO,
    exactamente como haría get_db() al cerrar la sesión ante una
    excepción no controlada que se propaga fuera del endpoint."""
    fecha = _dia_habil_futuro()
    prof = _profesional(
        db_session,
        horario_inicio="09:00", horario_fin="17:00",
        hora_almuerzo_inicio="13:00", hora_almuerzo_fin="14:00",
        duracion_min=30,
    )
    paciente = _usuario(db_session, correo="paciente-rollback-a41@sesaes.cl", rol="estudiante", rut="a41-24")
    admin_user = _usuario(db_session, correo="admin-rollback-a41@sesaes.cl", rol="admin", rut="a41-25")
    _acceso_administrativo(db_session, admin_user, perfil="administrador_general")
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, citas)
    monkeypatch.setattr(
        citas,
        "registrar_evento_auditoria",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("falla simulada tardía")),
    )

    payload = CitaCreate(
        estudiante_id=paciente.id, profesional_id=prof.id, fecha=fecha, hora="13:00",
        sobrecupo=True,
    )
    with pytest.raises(RuntimeError):
        citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_user))

    # Nadie llamó a db.commit() antes de la falla — un rollback() acá
    # simula exactamente lo que get_db() provoca al cerrar la sesión
    # cuando la excepción se propaga sin manejar.
    db_session.rollback()

    assert db_session.query(Cita).count() == 0
    assert db_session.query(CitaSobrecupo).count() == 0
    assert db_session.query(CitaSobrecupoConflicto).count() == 0
    assert db_session.query(Auditoria).count() == 0


# ══════════════════════════════════════════════════════════
# 13) el modelo se configura sin errores de mapper
# ══════════════════════════════════════════════════════════

def test_configure_mappers_no_falla_con_los_modelos_nuevos():
    """Regresión explícita del incidente previo ('SolicitudHorario
    failed to locate a name'): registrar CitaSobrecupo /
    CitaSobrecupoConflicto no debe dejar ninguna relación con un
    nombre de clase que SQLAlchemy no pueda resolver."""
    configure_mappers()


def test_cita_sobrecupo_conflicto_quedan_registrados_en_metadata():
    assert "cita_sobrecupo" in Base.metadata.tables
    assert "cita_sobrecupo_conflicto" in Base.metadata.tables
    columnas_cita = {c.name for c in Base.metadata.tables["cita"].columns}
    assert {
        "creado_por_usuario_id", "creado_por_rol", "creado_por_perfil", "fecha_creacion",
    } <= columnas_cita


# ══════════════════════════════════════════════════════════
# A.4.1 v3 — invariantes NOT NULL: nullable=True solo donde hay
# compatibilidad histórica real (Cita), nullable=False en las tablas
# COMPLETAMENTE NUEVAS que no tienen ningún antecedente que
# compatibilizar (CitaSobrecupo.fecha_creacion/cita_id,
# CitaSobrecupoConflicto.cita_sobrecupo_id/codigo).
# ══════════════════════════════════════════════════════════

def test_invariantes_not_null_de_esquema_a41():
    columnas_cita = Base.metadata.tables["cita"].columns
    columnas_sobrecupo = Base.metadata.tables["cita_sobrecupo"].columns
    columnas_conflicto = Base.metadata.tables["cita_sobrecupo_conflicto"].columns

    # Cita: compatibilidad histórica real -> nullable=True.
    assert columnas_cita["fecha_creacion"].nullable is True
    assert columnas_cita["creado_por_usuario_id"].nullable is True
    assert columnas_cita["creado_por_rol"].nullable is True
    assert columnas_cita["creado_por_perfil"].nullable is True

    # CitaSobrecupo: tabla nueva, sin filas históricas que
    # compatibilizar -> NOT NULL en lo que todo sobrecupo SIEMPRE
    # tiene (a qué cita pertenece, cuándo se creó realmente).
    assert columnas_sobrecupo["cita_id"].nullable is False
    assert columnas_sobrecupo["fecha_creacion"].nullable is False
    # motivo/estado_revision SÍ siguen nullable (ver docstring del
    # modelo: compatibilidad temporal con el frontend / preparación
    # para una fase futura de aprobación que A.4.1 no implementa).
    assert columnas_sobrecupo["motivo"].nullable is True
    assert columnas_sobrecupo["estado_revision"].nullable is True

    # CitaSobrecupoConflicto: igual criterio, tabla nueva.
    assert columnas_conflicto["cita_sobrecupo_id"].nullable is False
    assert columnas_conflicto["codigo"].nullable is False


def test_cita_sobrecupo_fecha_creacion_la_genera_el_server_default(db_session, monkeypatch):
    """No se inventa manualmente ninguna fecha: al no fijar
    fecha_creacion explícitamente, el server_default (func.now(), ya
    aplicado por create_all() en este esquema de test) la genera
    sola — y, al ser NOT NULL, la fila no podría existir sin ella."""
    fecha = _dia_habil_futuro()
    prof = _profesional(
        db_session,
        horario_inicio="09:00", horario_fin="17:00",
        hora_almuerzo_inicio="13:00", hora_almuerzo_fin="14:00",
        duracion_min=30,
    )
    paciente = _usuario(db_session, correo="paciente-fecha-a41@sesaes.cl", rol="estudiante", rut="a41-26")
    admin_user = _usuario(db_session, correo="admin-fecha-a41@sesaes.cl", rol="admin", rut="a41-27")
    _acceso_administrativo(db_session, admin_user, perfil="administrador_general")
    db_session.commit()

    _monkeypatch_admin_institucional(monkeypatch, citas)

    payload = CitaCreate(
        estudiante_id=paciente.id, profesional_id=prof.id, fecha=fecha, hora="13:00",
        sobrecupo=True,
    )
    citas.crear_cita(cita=payload, db=db_session, current_user=_current_user(admin_user))

    creada = db_session.query(Cita).one()
    detalle = db_session.query(CitaSobrecupo).filter(CitaSobrecupo.cita_id == creada.id).one()
    assert detalle.fecha_creacion is not None
