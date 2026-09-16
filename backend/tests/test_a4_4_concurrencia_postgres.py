# -*- coding: utf-8 -*-
"""
SESAES — A.4.4: prueba de integración REAL contra PostgreSQL para el
sobrecupo intencional sobre slot_ocupado (máximo 2 citas activas
simultáneas).

Mismo criterio que test_a3_concurrencia_postgres.py (actor real en
BD, nunca un dict con id fabricado; pg_advisory_xact_lock real; se
salta explícitamente si TEST_POSTGRES_URL no está definida — nunca se
simula la garantía). Acá se ejercita el flujo REAL completo
(citas.crear_cita(), no una réplica manual de lock->evaluar->insertar)
porque A.4.4 depende de la política de sobrecupo (permisos + motivo),
no solo de la re-evaluación de disponibilidad dentro del lock.

Cuatro escenarios pedidos:

  1. normal vs normal (mismo slot, sin ocupación previa) — A.3 se
     conserva sin cambios: exactamente una gana, la otra 409.
  2. normal-first vs sobrecupo (sobre la cita que dejó la primera) —
     resultado: 2 citas activas. Orden DETERMINISTA por diseño (ver
     `_orden_determinista_del_lock()` más abajo): dos hilos reales,
     el segundo NUNCA se lanza hasta que un `threading.Event` confirma
     que el primero ya adquirió el pg_advisory_xact_lock real — nunca
     se depende del scheduler del SO, y el único `time.sleep()`
     involucrado es secundario (solo garantiza contención real del
     segundo hilo, no decide el orden).
  3. sobrecupo-flag-first sobre slot inicialmente libre vs normal —
     resultado: la primera queda como cita NORMAL (flag sobrante,
     A.4.1 sin cambios), la segunda ve 409. Mismo mecanismo
     determinista que el escenario 2.
  4. dos sobrecupos concurrentes sobre 1 cita ya existente — CARRERA
     real con barrera: exactamente uno autorizado, el otro 409; al
     final, exactamente 2 citas activas, nunca 3. Además (punto 5 de
     la corrección), una NUEVA solicitud de sobrecupo secuencial
     posterior a la carrera debe seguir viendo 409 — la capacidad
     máxima queda cerrada, no se reabre por haber corrido una carrera.

Cómo correrlo:
    TEST_POSTGRES_URL="postgresql+psycopg2://..." \
        pytest tests/test_a4_4_concurrencia_postgres.py -m postgres -q
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
import uuid
from datetime import date, timedelta

import pytest

pytestmark = pytest.mark.postgres

TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL")

if not TEST_POSTGRES_URL:
    pytest.skip(
        "A.4.4: TEST_POSTGRES_URL no está definida — la garantía de "
        "capacidad máxima (2) bajo concurrencia real NO se validó "
        "contra PostgreSQL en esta ejecución. Los tests SQLite de "
        "test_a4_4_sobrecupo_slot_ocupado.py prueban el contrato "
        "(qué decide la política, qué se persiste), no la concurrencia "
        "real del advisory lock. Define TEST_POSTGRES_URL apuntando a "
        "un Postgres de test para ejercitar esta garantía.",
        allow_module_level=True,
    )

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from fastapi import HTTPException

from app.database import Base
from app.models.cita import Cita
from app.models.cita_sobrecupo import CitaSobrecupo
from app.models.profesional import Profesional
from app.models.usuario import Usuario
from app.routers import citas
from app.schemas import CitaCreate


def _dia_habil_futuro(dias_calendario: int = 1) -> str:
    fecha = date.today() + timedelta(days=dias_calendario)
    while fecha.weekday() >= 5:
        fecha += timedelta(days=1)
    return fecha.isoformat()


@pytest.fixture(scope="module")
def engine_postgres():
    try:
        engine = create_engine(TEST_POSTGRES_URL, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - depende del entorno
        pytest.skip(
            f"A.4.4: no se pudo conectar a TEST_POSTGRES_URL ({exc!r}). "
            f"La garantía transaccional real quedó sin validar en esta "
            f"ejecución."
        )
        return
    assert engine.dialect.name == "postgresql", (
        "TEST_POSTGRES_URL no apunta a un dialecto postgresql; este "
        "test existe específicamente para ejercitar "
        "pg_advisory_xact_lock real."
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def datos_base(engine_postgres):
    """Profesional + actor SUPERADMIN real + 4 estudiantes reales de
    este test run. SUPERADMIN (no un dict con rol="admin" fabricado):
    sus permisos AGENDA_GESTIONAR/AGENDA_SOBRECUPO se resuelven vía
    has_permission() contra ROLE_DEFAULT_PERMISSIONS sin depender de
    ninguna fila AccesoAdminPermiso — mismo criterio de "actor real"
    que test_a3_concurrencia_postgres.py."""
    SessionLocal = sessionmaker(bind=engine_postgres, autoflush=False, autocommit=False)
    setup = SessionLocal()
    sufijo = uuid.uuid4().hex[:12]
    prof = Profesional(
        nombre="A4.4 Postgres IT", especialidad="Nutrición", iniciales="A44",
        estado="activo", horario_inicio="09:00", horario_fin="18:00",
        duracion_min=45,
    )
    actor_superadmin = Usuario(
        correo=f"a44-postgres-superadmin-{sufijo}@sesaes.cl", password="x",
        rol="superadmin", nombre="A4.4 Superadmin", rut=f"a44-pg-sa-{sufijo}", activo=True,
    )
    estudiantes = [
        Usuario(
            correo=f"a44-postgres-est{i}-{sufijo}@sesaes.cl", password="x",
            rol="estudiante", nombre=f"A4.4 Est {i}", rut=f"a44-pg-e{i}-{sufijo}", activo=True,
        )
        for i in range(4)
    ]
    setup.add_all([prof, actor_superadmin, *estudiantes])
    setup.commit()
    setup.refresh(prof)
    setup.refresh(actor_superadmin)
    for est in estudiantes:
        setup.refresh(est)
    ids = {
        "profesional_id": prof.id,
        "actor_superadmin_id": actor_superadmin.id,
        "estudiante_ids": [est.id for est in estudiantes],
    }
    setup.close()

    yield ids

    limpieza = SessionLocal()
    try:
        limpieza.query(CitaSobrecupo).filter(
            CitaSobrecupo.cita_id.in_(
                limpieza.query(Cita.id).filter(Cita.profesional_id == ids["profesional_id"])
            )
        ).delete(synchronize_session=False)
        limpieza.query(Cita).filter(Cita.profesional_id == ids["profesional_id"]).delete()
        limpieza.query(Profesional).filter(Profesional.id == ids["profesional_id"]).delete()
        limpieza.query(Usuario).filter(
            Usuario.id.in_([ids["actor_superadmin_id"], *ids["estudiante_ids"]])
        ).delete(synchronize_session=False)
        limpieza.commit()
    except Exception:
        limpieza.rollback()
    finally:
        limpieza.close()


def _intentar_crear_cita(
    *,
    engine,
    current_user: dict,
    estudiante_id: int,
    profesional_id: int,
    fecha: str,
    hora: str,
    sobrecupo: bool = False,
    sobrecupo_motivo: str | None = None,
    resultado: dict,
    barrera: threading.Barrier | None = None,
):
    """Réplica de una request HTTP real a POST /citas: llama
    directamente a citas.crear_cita() (nunca una reimplementación
    manual de lock->evaluar->insertar) con su propia Session/conexión,
    como haría cada request real. crear_cita() ya maneja su propio
    commit/rollback internamente."""
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = SessionLocal()
    try:
        if barrera is not None:
            barrera.wait(timeout=15)
        payload = CitaCreate(
            estudiante_id=estudiante_id, profesional_id=profesional_id,
            fecha=fecha, hora=hora, sobrecupo=sobrecupo, sobrecupo_motivo=sobrecupo_motivo,
        )
        creada = citas.crear_cita(cita=payload, db=session, current_user=current_user)
        resultado.update(ok=True, cita_id=creada["id"])
    except HTTPException as exc:
        resultado.update(ok=False, status_code=exc.status_code, detail=exc.detail)
    except Exception as exc:  # pragma: no cover - diagnóstico de fallos reales
        resultado.update(ok=False, error=repr(exc))
    finally:
        session.close()


def _citas_activas(engine, *, profesional_id, fecha):
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    verificacion = SessionLocal()
    try:
        return (
            verificacion.query(Cita)
            .filter(
                Cita.profesional_id == profesional_id,
                Cita.fecha == fecha,
                Cita.estado.in_(("pendiente", "completada")),
            )
            .all()
        )
    finally:
        verificacion.close()


@contextlib.contextmanager
def _orden_determinista_del_lock(pausa_segundos: float = 0.8):
    """
    A.4.4 (corrección v2, punto 4) — fuerza el ORDEN real de
    adquisición del advisory lock entre dos hilos sin depender del
    scheduler del SO ni de un sleep como mecanismo PRINCIPAL.

    Parchea temporalmente `citas.adquirir_lock_agenda_profesional_fecha`
    — el nombre real que importa y usa `app.routers.citas.crear_cita()`
    (`from app.services.agenda_disponibilidad_service import
    adquirir_lock_agenda_profesional_fecha`, ver ese archivo), no una
    reimplementación — para que, la PRIMERA vez que CUALQUIER hilo la
    invoque dentro de este bloque:

      1. adquiera el `pg_advisory_xact_lock` REAL, llamando a la
         función original sin alterar su comportamiento ni su
         resultado;
      2. señale `lock_adquirido` (el `threading.Event` que este
         context manager entrega) — el hilo que orquesta el test usa
         ESTE evento, no un `time.sleep()`, para saber cuándo es
         seguro lanzar el segundo hilo. Esto garantiza que el segundo
         hilo ni siquiera EXISTE hasta que el primero ya adquirió el
         lock real — el orden queda establecido por construcción, sin
         ninguna dependencia del scheduler;
      3. mantenga la transacción abierta `pausa_segundos` antes de
         continuar — el ÚNICO `time.sleep()` de todo el mecanismo, y
         es deliberadamente secundario: NO decide el orden (el Event
         ya lo hizo), solo le da tiempo al segundo hilo —que arranca
         apenas se señala el evento— para alcanzar su propio intento
         de adquirir el MISMO lock antes de que el primero libere vía
         commit/rollback, de forma que experimente contención REAL de
         Postgres en vez de encontrar el lock ya libre por casualidad
         de timing.

    Cualquier invocación posterior a la primera (la del segundo hilo,
    o cualquier llamada interna subsecuente) pasa sin pausa ni señal
    adicional — se restaura la función original al salir del bloque
    `with`, incluso si algo falla dentro.
    """
    original = citas.adquirir_lock_agenda_profesional_fecha
    lock_adquirido = threading.Event()
    candado_interno = threading.Lock()
    estado = {"ya_ocurrio_la_primera": False}

    def _envoltorio(*args, **kwargs):
        resultado = original(*args, **kwargs)
        with candado_interno:
            es_la_primera = not estado["ya_ocurrio_la_primera"]
            estado["ya_ocurrio_la_primera"] = True
        if es_la_primera:
            lock_adquirido.set()
            if pausa_segundos:
                time.sleep(pausa_segundos)
        return resultado

    citas.adquirir_lock_agenda_profesional_fecha = _envoltorio
    try:
        yield lock_adquirido
    finally:
        citas.adquirir_lock_agenda_profesional_fecha = original


# ══════════════════════════════════════════════════════════
# 1) normal vs normal — A.3 se conserva sin cambios.
# ══════════════════════════════════════════════════════════

def test_1_normal_vs_normal_solo_una_gana(engine_postgres, datos_base):
    fecha = _dia_habil_futuro()
    profesional_id = datos_base["profesional_id"]
    est_a, est_b = datos_base["estudiante_ids"][0], datos_base["estudiante_ids"][1]
    hora = "10:15"  # bloque real de la grilla (duracion_min=45 desde 08:00... 09:00 jornada)

    barrera = threading.Barrier(2)
    resultado_a: dict = {}
    resultado_b: dict = {}

    hilo_a = threading.Thread(
        target=_intentar_crear_cita,
        kwargs=dict(
            engine=engine_postgres,
            current_user={"id": est_a, "rol": "estudiante"},
            estudiante_id=est_a, profesional_id=profesional_id,
            fecha=fecha, hora=hora, barrera=barrera, resultado=resultado_a,
        ),
    )
    hilo_b = threading.Thread(
        target=_intentar_crear_cita,
        kwargs=dict(
            engine=engine_postgres,
            current_user={"id": est_b, "rol": "estudiante"},
            estudiante_id=est_b, profesional_id=profesional_id,
            fecha=fecha, hora=hora, barrera=barrera, resultado=resultado_b,
        ),
    )
    hilo_a.start(); hilo_b.start()
    hilo_a.join(timeout=30); hilo_b.join(timeout=30)
    assert not hilo_a.is_alive() and not hilo_b.is_alive(), "posible deadlock del advisory lock"

    resultados = [resultado_a, resultado_b]
    exitosas = [r for r in resultados if r.get("ok")]
    rechazadas = [r for r in resultados if not r.get("ok")]
    assert len(exitosas) == 1, f"se esperaba exactamente 1 éxito: {resultados}"
    assert len(rechazadas) == 1, f"se esperaba exactamente 1 rechazo: {resultados}"
    assert rechazadas[0].get("status_code") == 409

    activas = _citas_activas(engine_postgres, profesional_id=profesional_id, fecha=fecha)
    assert len(activas) == 1


# ══════════════════════════════════════════════════════════
# 2) normal-first vs sobrecupo — orden determinista (ESCENARIO 1 de
#    la aprobación): 2 citas activas al final.
# ══════════════════════════════════════════════════════════

def test_2_normal_primero_luego_sobrecupo_intencional_resultan_2_citas(engine_postgres, datos_base):
    fecha = _dia_habil_futuro()
    profesional_id = datos_base["profesional_id"]
    est_normal, est_sobrecupo = datos_base["estudiante_ids"][0], datos_base["estudiante_ids"][1]
    actor_superadmin = datos_base["actor_superadmin_id"]
    hora = "11:00"

    resultado_normal: dict = {}
    resultado_sobrecupo: dict = {}

    with _orden_determinista_del_lock(pausa_segundos=0.8) as lock_adquirido:
        hilo_normal = threading.Thread(
            target=_intentar_crear_cita,
            kwargs=dict(
                engine=engine_postgres,
                current_user={"id": est_normal, "rol": "estudiante"},
                estudiante_id=est_normal, profesional_id=profesional_id,
                fecha=fecha, hora=hora, resultado=resultado_normal,
            ),
        )
        hilo_normal.start()
        assert lock_adquirido.wait(timeout=10), (
            "el hilo NORMAL nunca llegó a adquirir el advisory lock real"
        )

        hilo_sobrecupo = threading.Thread(
            target=_intentar_crear_cita,
            kwargs=dict(
                engine=engine_postgres,
                current_user={"id": actor_superadmin, "rol": "superadmin"},
                estudiante_id=est_sobrecupo, profesional_id=profesional_id,
                fecha=fecha, hora=hora, sobrecupo=True,
                sobrecupo_motivo="Paciente con indicación urgente del profesional",
                resultado=resultado_sobrecupo,
            ),
        )
        hilo_sobrecupo.start()
        hilo_normal.join(timeout=30)
        hilo_sobrecupo.join(timeout=30)

    assert not hilo_normal.is_alive() and not hilo_sobrecupo.is_alive(), "posible deadlock del advisory lock"
    assert resultado_normal.get("ok") is True, resultado_normal
    assert resultado_sobrecupo.get("ok") is True, resultado_sobrecupo

    activas = _citas_activas(engine_postgres, profesional_id=profesional_id, fecha=fecha)
    assert len(activas) == 2
    sobrecupos_reales = [c for c in activas if c.sobrecupo]
    assert len(sobrecupos_reales) == 1


# ══════════════════════════════════════════════════════════
# 3) sobrecupo-flag-first sobre slot inicialmente libre vs normal —
#    mismo mecanismo determinista (ESCENARIO 2 de la aprobación): la
#    primera queda NORMAL (flag sobrante, A.4.1), la segunda ve 409.
# ══════════════════════════════════════════════════════════

def test_3_flag_sobrecupo_sobre_slot_libre_luego_normal_la_segunda_es_409(engine_postgres, datos_base):
    fecha = _dia_habil_futuro()
    profesional_id = datos_base["profesional_id"]
    est_primero, est_segundo = datos_base["estudiante_ids"][0], datos_base["estudiante_ids"][1]
    actor_superadmin = datos_base["actor_superadmin_id"]
    hora = "12:30"

    resultado_primero: dict = {}
    resultado_segundo: dict = {}

    with _orden_determinista_del_lock(pausa_segundos=0.8) as lock_adquirido:
        hilo_primero = threading.Thread(
            target=_intentar_crear_cita,
            kwargs=dict(
                engine=engine_postgres,
                current_user={"id": actor_superadmin, "rol": "superadmin"},
                estudiante_id=est_primero, profesional_id=profesional_id,
                fecha=fecha, hora=hora, sobrecupo=True,
                sobrecupo_motivo="Motivo válido",
                resultado=resultado_primero,
            ),
        )
        hilo_primero.start()
        assert lock_adquirido.wait(timeout=10), (
            "el hilo con sobrecupo=True nunca llegó a adquirir el advisory lock real"
        )

        hilo_segundo = threading.Thread(
            target=_intentar_crear_cita,
            kwargs=dict(
                engine=engine_postgres,
                current_user={"id": est_segundo, "rol": "estudiante"},
                estudiante_id=est_segundo, profesional_id=profesional_id,
                fecha=fecha, hora=hora,
                resultado=resultado_segundo,
            ),
        )
        hilo_segundo.start()
        hilo_primero.join(timeout=30)
        hilo_segundo.join(timeout=30)

    assert not hilo_primero.is_alive() and not hilo_segundo.is_alive(), "posible deadlock del advisory lock"
    assert resultado_primero.get("ok") is True, resultado_primero
    assert resultado_segundo.get("ok") is False
    assert resultado_segundo.get("status_code") == 409

    activas = _citas_activas(engine_postgres, profesional_id=profesional_id, fecha=fecha)
    assert len(activas) == 1
    # A.4.1 — flag sobrante nunca se convierte en sobrecupo fantasma:
    # la única cita activa quedó como NORMAL, no como sobrecupo.
    assert activas[0].sobrecupo is False


# ══════════════════════════════════════════════════════════
# 4) dos sobrecupos concurrentes sobre 1 cita existente — CARRERA
#    real: exactamente uno autorizado, el otro 409; 2 citas al final,
#    nunca 3.
# ══════════════════════════════════════════════════════════

def test_4_dos_sobrecupos_concurrentes_y_capacidad_sigue_cerrada_despues(
    engine_postgres, datos_base,
):
    fecha = _dia_habil_futuro()
    profesional_id = datos_base["profesional_id"]
    est_original, est_sobrecupo_a, est_sobrecupo_b = datos_base["estudiante_ids"][0:3]
    actor_superadmin = datos_base["actor_superadmin_id"]
    hora = "14:00"

    # 1 cita normal ya activa (fuera de la carrera, secuencial).
    resultado_original: dict = {}
    _intentar_crear_cita(
        engine=engine_postgres,
        current_user={"id": est_original, "rol": "estudiante"},
        estudiante_id=est_original, profesional_id=profesional_id,
        fecha=fecha, hora=hora, resultado=resultado_original,
    )
    assert resultado_original.get("ok") is True, resultado_original

    barrera = threading.Barrier(2)
    resultado_a: dict = {}
    resultado_b: dict = {}

    hilo_a = threading.Thread(
        target=_intentar_crear_cita,
        kwargs=dict(
            engine=engine_postgres,
            current_user={"id": actor_superadmin, "rol": "superadmin"},
            estudiante_id=est_sobrecupo_a, profesional_id=profesional_id,
            fecha=fecha, hora=hora, sobrecupo=True,
            sobrecupo_motivo="Sobrecupo A",
            barrera=barrera, resultado=resultado_a,
        ),
    )
    hilo_b = threading.Thread(
        target=_intentar_crear_cita,
        kwargs=dict(
            engine=engine_postgres,
            current_user={"id": actor_superadmin, "rol": "superadmin"},
            estudiante_id=est_sobrecupo_b, profesional_id=profesional_id,
            fecha=fecha, hora=hora, sobrecupo=True,
            sobrecupo_motivo="Sobrecupo B",
            barrera=barrera, resultado=resultado_b,
        ),
    )
    hilo_a.start(); hilo_b.start()
    hilo_a.join(timeout=30); hilo_b.join(timeout=30)
    assert not hilo_a.is_alive() and not hilo_b.is_alive(), "posible deadlock del advisory lock"

    resultados = [resultado_a, resultado_b]
    exitosas = [r for r in resultados if r.get("ok")]
    rechazadas = [r for r in resultados if not r.get("ok")]
    assert len(exitosas) == 1, f"se esperaba exactamente 1 sobrecupo autorizado: {resultados}"
    assert len(rechazadas) == 1, f"se esperaba exactamente 1 rechazo: {resultados}"
    assert rechazadas[0].get("status_code") == 409

    activas = _citas_activas(engine_postgres, profesional_id=profesional_id, fecha=fecha)
    assert len(activas) == 2, (
        f"debían quedar EXACTAMENTE 2 citas activas (1 normal + 1 "
        f"sobrecupo autorizado), nunca 3: se encontraron {len(activas)}"
    )
    assert sum(1 for c in activas if c.sobrecupo) == 1

    # Punto 5 de la corrección — después de la carrera, la capacidad
    # máxima debe seguir CERRADA: una nueva solicitud de sobrecupo
    # secuencial (sin carrera, ya con el lock libre) debe seguir
    # viendo max_concurrencia_existente=2 y ser rechazada; el total
    # de citas activas no debe subir a 3.
    est_sobrecupo_c = datos_base["estudiante_ids"][3]
    resultado_c: dict = {}
    _intentar_crear_cita(
        engine=engine_postgres,
        current_user={"id": actor_superadmin, "rol": "superadmin"},
        estudiante_id=est_sobrecupo_c, profesional_id=profesional_id,
        fecha=fecha, hora=hora, sobrecupo=True,
        sobrecupo_motivo="Sobrecupo C — posterior a la carrera",
        resultado=resultado_c,
    )
    assert resultado_c.get("ok") is False, resultado_c
    assert resultado_c.get("status_code") == 409

    activas_finales = _citas_activas(engine_postgres, profesional_id=profesional_id, fecha=fecha)
    assert len(activas_finales) == 2, (
        f"la capacidad máxima debía seguir cerrada tras la carrera: "
        f"se encontraron {len(activas_finales)} citas activas"
    )
