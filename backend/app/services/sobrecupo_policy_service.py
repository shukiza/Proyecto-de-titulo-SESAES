# -*- coding: utf-8 -*-
"""
SESAES — A.4.3: política central de sobrecupo + permisos.

Responde UNA sola pregunta, de forma centralizada y reutilizable:

    Dado el usuario que intenta crear la cita, sus permisos
    efectivos, la intención explícita de sobrecupo, el motivo humano
    y los conflictos estructurados detectados por A.4.2 (dentro del
    lock A.3) — ¿está permitido crear este sobrecupo?

Esta política NO:
  - reanaliza disponibilidad ni hace queries de agenda (recibe el
    `ResultadoDisponibilidad` ya calculado DENTRO del lock A.3 — ver
    `evaluar_disponibilidad_slot()`);
  - reimplementa ninguna regla de conflicto de A.4.2 (usa
    `hay_bloqueo_absoluto()` / `ConflictoSlot.overridable_con_sobrecupo`
    tal cual, nunca vuelve a codificar manualmente qué motivo es
    absoluto);
  - conoce FastAPI ni HTTP: `DecisionSobrecupo` es transport-agnostic
    (sin status code), a propósito, para que A.4.4/A.4.5 puedan
    reutilizarla desde otro router sin acoplarse a este. El router
    (`app.routers.citas`) es quien mapea `DecisionSobrecupo.codigo` a
    un `HTTPException` concreto;
  - habilita NINGÚN tipo de sobrecupo nuevo: sigue exigiendo que TODOS
    los conflictos presentes sean `overridable_con_sobrecupo=True`
    (fuera_de_jornada / en_colacion, sin cambios respecto a A.4.1/
    A.4.2). slot_ocupado y el resto de conflictos absolutos siguen
    siendo, sin excepción, un bloqueo (A.4.4 es quien podrá cambiar
    eso).

PERMISOS — SIEMPRE por permiso efectivo, NUNCA por nombre de rol:

    La política jamás hace `if rol == "admin"` ni
    `if rol == "superadmin"`. Toda decisión de capacidad pasa por
    `tiene_permiso_efectivo()` (la misma resolución ya usada en todo
    el resto de RBAC/agenda — ver app.rbac.admin_authorization), tanto
    para `Permission.AGENDA_GESTIONAR` como para la nueva
    `Permission.AGENDA_SOBRECUPO` (A.4.3): SUPERADMIN pasa por la
    MISMA resolución que cualquier otro rol (para SUPERADMIN,
    `tiene_permiso_efectivo()` cae en `has_permission()` contra
    `ROLE_DEFAULT_PERMISSIONS[SUPERADMIN]` — una tabla explícita, no
    un atajo por nombre) y una cuenta ADMIN necesita la fila
    `AccesoAdminPermiso` correspondiente realmente persistida (SA-9),
    no solo que su perfil lo permita en el techo
    (`PERFIL_PERMISOS_PERMITIDOS`) — técho != concesión.

    `Permission.AGENDA_SOBRECUPO` es DISTINTO de
    `Permission.AGENDA_GESTIONAR` a propósito: poder gestionar una
    agenda (crear/mover/cancelar citas normales) no implica
    automáticamente poder forzar un sobrecupo sobre un conflicto real.

ORDEN DE DECISIÓN — diseñado para no filtrar información a quien no
corresponde (ver cada rama abajo para el detalle exacto):

    A) slot realmente disponible (tras la re-evaluación DENTRO del
       lock) -> cita normal, sin importar la intención de sobrecupo.
       NUNCA se consulta ningún permiso ni se exige motivo acá: si no
       hay nada que superar, no hay nada que autorizar.
    B) no disponible, pero sin conflictos estructurados (precondición
       terminal de A.4.2 — profesional inexistente/inactivo, fecha u
       hora inválida) -> rechazo legacy, tal cual siempre.
    C) existe cualquier conflicto ABSOLUTO -> rechazo, siempre, ANTES
       de mirar ningún permiso — para no cambiar el status/mensaje
       observable de siempre ni revelar la existencia de
       agenda.sobrecupo a quien de todas formas no podría usarlo
       (el conflicto ya es infranqueable).
    D) todos los conflictos presentes son overridables, pero no hubo
       intención explícita (`sobrecupo=False`) -> mismo rechazo legacy
       de siempre.
    E) recién ahora se resuelve `agenda.gestionar`. Un actor SIN esta
       capacidad (p. ej. Estudiante en autoservicio, que sí puede
       llegar hasta acá con `sobrecupo=True`) recibe el MISMO rechazo
       legacy que ya recibía antes de A.4.3 — nunca el 403 granular de
       `agenda.sobrecupo`, que revelaría a alguien sin ninguna
       capacidad administrativa que esa capacidad más fina existe.
    F) tiene `agenda.gestionar` pero NO `agenda.sobrecupo` -> acá sí
       corresponde un error nuevo y específico (el actor ya demostró
       tener capacidad administrativa sobre la agenda; no hay nada que
       proteger ocultándole que falta un permiso más).
    G) tiene ambos permisos pero el motivo humano es None/vacío/solo
       espacios -> error nuevo de validación (sin relación con
       permisos).
    H) todo válido -> autorizado, sobrecupo efectivo.

MOTIVO vs INTENCIÓN — preserva A.4.1 al pie de la letra: si
`sobrecupo=True` pero el slot resulta libre al reevaluar dentro del
lock (rama A), la cita se crea NORMAL — `es_sobrecupo_efectivo=False`,
sin exigir motivo, sin `CitaSobrecupo`, sin conflictos, sin auditoría
de sobrecupo. Nunca se persiste un "sobrecupo fantasma".
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.rbac.admin_authorization import tiene_permiso_efectivo
from app.rbac.permissions import Permission
from app.services.agenda_disponibilidad_service import (
    ConflictoSlot,
    ResultadoDisponibilidad,
    hay_bloqueo_absoluto,
)

# ══════════════════════════════════════════════════════════════════
# Códigos de decisión — uno por rama, nunca reutilizados entre
# escenarios semánticamente distintos (ver correcciones acordadas
# antes de implementar).
# ══════════════════════════════════════════════════════════════════

CODIGO_NORMAL_SIN_CONFLICTO = "normal_sin_conflicto"
CODIGO_FLAG_SOBRANTE_SIN_CONFLICTO = "flag_sobrante_sin_conflicto"
CODIGO_DENEGADO_PRECONDICION_LEGACY = "denegado_precondicion_legacy"
CODIGO_DENEGADO_BLOQUEO_ABSOLUTO = "denegado_bloqueo_absoluto"
CODIGO_DENEGADO_CONFLICTO_SIN_INTENCION = "denegado_conflicto_sin_intencion"
CODIGO_DENEGADO_SIN_PERMISO = "denegado_sin_permiso"
CODIGO_DENEGADO_SIN_MOTIVO = "denegado_sin_motivo"
CODIGO_AUTORIZADO = "autorizado"
# Reservado a estados que violen invariantes imposibles del propio
# ResultadoDisponibilidad (ver `_evaluar_disponibilidad_es_coherente`
# más abajo) — NUNCA usado para las precondiciones terminales
# legítimas de A.4.2 (esas son CODIGO_DENEGADO_PRECONDICION_LEGACY).
CODIGO_DENEGADO_ENTRADA_INCOHERENTE = "denegado_entrada_incoherente"

CODIGOS_VALIDOS = frozenset(
    {
        CODIGO_NORMAL_SIN_CONFLICTO,
        CODIGO_FLAG_SOBRANTE_SIN_CONFLICTO,
        CODIGO_DENEGADO_PRECONDICION_LEGACY,
        CODIGO_DENEGADO_BLOQUEO_ABSOLUTO,
        CODIGO_DENEGADO_CONFLICTO_SIN_INTENCION,
        CODIGO_DENEGADO_SIN_PERMISO,
        CODIGO_DENEGADO_SIN_MOTIVO,
        CODIGO_AUTORIZADO,
        CODIGO_DENEGADO_ENTRADA_INCOHERENTE,
    }
)


@dataclass(frozen=True)
class DecisionSobrecupo:
    """
    Resultado de `evaluar_politica_sobrecupo()`. Transport-agnostic a
    propósito (sin status HTTP) — ver docstring del módulo.

    Campos:
      - `permitido`: si la operación (normal o sobrecupo) puede
        continuar. False siempre implica que el router debe rechazar
        la petición.
      - `es_sobrecupo_efectivo`: True SOLO en `CODIGO_AUTORIZADO`.
        Determina si la cita se marca `Cita.sobrecupo=True` y si se
        crean `CitaSobrecupo`/`CitaSobrecupoConflicto`/auditoría de
        sobrecupo.
      - `codigo`: uno de los CODIGO_* de arriba.
      - `conflictos`: la MISMA tupla de `ResultadoDisponibilidad`, sin
        transformar — () cuando no aplica (ramas A/B).
      - `requiere_motivo`: True solo cuando el motivo humano fue
        efectivamente parte de esta decisión (CODIGO_DENEGADO_SIN_MOTIVO
        / CODIGO_AUTORIZADO). No es una promesa de "esto habría
        funcionado con motivo": en CODIGO_DENEGADO_SIN_PERMISO, por
        ejemplo, la evaluación nunca llegó a mirar el motivo.
      - `motivo_normalizado`: el motivo humano ya con `strip()`
        aplicado, SOLO cuando `es_sobrecupo_efectivo=True`. El router
        debe persistir este valor EXACTO en `CitaSobrecupo.motivo` —
        no debe volver a hacer `strip()`/validación por su cuenta.
    """

    permitido: bool
    es_sobrecupo_efectivo: bool
    codigo: str
    conflictos: tuple[ConflictoSlot, ...]
    requiere_motivo: bool
    motivo_normalizado: str | None


def _decision(
    *,
    permitido: bool,
    es_sobrecupo_efectivo: bool,
    codigo: str,
    conflictos: tuple[ConflictoSlot, ...] = (),
    requiere_motivo: bool = False,
    motivo_normalizado: str | None = None,
) -> DecisionSobrecupo:
    assert codigo in CODIGOS_VALIDOS, f"código de decisión desconocido: {codigo!r}"
    return DecisionSobrecupo(
        permitido=permitido,
        es_sobrecupo_efectivo=es_sobrecupo_efectivo,
        codigo=codigo,
        conflictos=conflictos,
        requiere_motivo=requiere_motivo,
        motivo_normalizado=motivo_normalizado,
    )


def evaluar_politica_sobrecupo(
    db: Session,
    current_user: dict,
    *,
    sobrecupo_solicitado: bool,
    sobrecupo_motivo: str | None,
    resultado_disponibilidad: ResultadoDisponibilidad,
) -> DecisionSobrecupo:
    """
    Punto de entrada único de la política central de sobrecupo (A.4.3).

    Debe llamarse con el `ResultadoDisponibilidad` ya obtenido DENTRO
    del lock A.3 (adquirir_lock_agenda_profesional_fecha() ->
    evaluar_disponibilidad_slot() -> ESTA función -> persistir). Nunca
    al revés: analizar antes del lock y decidir después es exactamente
    la carrera que A.3 existe para evitar.

    Determinista y fail-closed: para cualquier combinación de entradas
    exactamente una rama aplica, y ninguna rama asume permiso salvo
    que `tiene_permiso_efectivo()` lo confirme explícitamente.
    """
    conflictos = resultado_disponibilidad.conflictos or ()
    disponible = resultado_disponibilidad.disponible

    # Invariante de ResultadoDisponibilidad (ver
    # _legacy_desde_conflictos() en agenda_disponibilidad_service):
    # conflictos no vacíos SIEMPRE implica disponible=False. Si algún
    # día un caller pasara un ResultadoDisponibilidad construido a
    # mano que violara esto, fail-closed en vez de intentar adivinar
    # qué rama aplicaría.
    if disponible and conflictos:
        return _decision(
            permitido=False,
            es_sobrecupo_efectivo=False,
            codigo=CODIGO_DENEGADO_ENTRADA_INCOHERENTE,
            conflictos=conflictos,
        )

    # A) slot realmente libre tras la re-evaluación dentro del lock —
    # la intención de sobrecupo nunca importa acá: no hay nada real
    # que superar, así que no hay nada que autorizar ni motivo que
    # exigir. Preserva A.4.1: "flag sobrante" nunca se convierte en un
    # sobrecupo fantasma.
    if disponible:
        codigo = (
            CODIGO_FLAG_SOBRANTE_SIN_CONFLICTO
            if sobrecupo_solicitado
            else CODIGO_NORMAL_SIN_CONFLICTO
        )
        return _decision(
            permitido=True,
            es_sobrecupo_efectivo=False,
            codigo=codigo,
        )

    # B) no disponible, pero sin lista estructurada de conflictos: una
    # precondición terminal de evaluar_disponibilidad_slot() (p. ej.
    # profesional_no_encontrado/inactivo, fecha/hora inválida) que
    # nunca llegó a analizar_conflictos_slot() — ver
    # ResultadoDisponibilidad.conflictos. Rechazo legacy legítimo,
    # nunca "entrada incoherente".
    if not conflictos:
        return _decision(
            permitido=False,
            es_sobrecupo_efectivo=False,
            codigo=CODIGO_DENEGADO_PRECONDICION_LEGACY,
        )

    # C) cualquier conflicto real y absoluto bloquea, siempre — ANTES
    # de mirar agenda.sobrecupo, para no cambiar el status/mensaje
    # observable de siempre ni revelar la existencia de esa capacidad
    # a alguien que de todas formas no podría usarla acá (A.4.4 es
    # quien podrá cambiar esto para slot_ocupado en particular).
    if hay_bloqueo_absoluto(conflictos):
        return _decision(
            permitido=False,
            es_sobrecupo_efectivo=False,
            codigo=CODIGO_DENEGADO_BLOQUEO_ABSOLUTO,
            conflictos=conflictos,
        )

    # D) todos los conflictos presentes son overridables, pero no hubo
    # intención explícita de sobrecupo -> mismo rechazo legacy de
    # siempre (p. ej. fuera_de_jornada/en_colacion sin sobrecupo=True).
    if not sobrecupo_solicitado:
        return _decision(
            permitido=False,
            es_sobrecupo_efectivo=False,
            codigo=CODIGO_DENEGADO_CONFLICTO_SIN_INTENCION,
            conflictos=conflictos,
        )

    # E) recién ahora entran en juego las capacidades para FORZAR el
    # sobrecupo. Primero agenda.gestionar — pero OJO: POST /citas
    # permite que cualquier Estudiante cree su propia cita normal, así
    # que un actor sin esta capacidad puede perfectamente llegar hasta
    # acá con sobrecupo=True (p. ej. un cliente que manda el flag sin
    # tener ningún rol administrativo). No se le entrega el 403
    # granular de agenda.sobrecupo — eso revelaría que existe una
    # capacidad administrativa más fina a alguien que nunca pudo
    # ejercer ni siquiera agenda.gestionar. Se conserva EXACTAMENTE el
    # mismo rechazo legacy del conflicto de la rama D.
    puede_gestionar_agenda = tiene_permiso_efectivo(
        db, current_user, Permission.AGENDA_GESTIONAR,
    )
    if not puede_gestionar_agenda:
        return _decision(
            permitido=False,
            es_sobrecupo_efectivo=False,
            codigo=CODIGO_DENEGADO_CONFLICTO_SIN_INTENCION,
            conflictos=conflictos,
        )

    # F) tiene agenda.gestionar pero NO el permiso específico de
    # sobrecupo. Siempre por permiso efectivo — jamás por nombre de
    # rol: SUPERADMIN pasa por esta MISMA llamada, sin atajo.
    puede_autorizar_sobrecupo = tiene_permiso_efectivo(
        db, current_user, Permission.AGENDA_SOBRECUPO,
    )
    if not puede_autorizar_sobrecupo:
        return _decision(
            permitido=False,
            es_sobrecupo_efectivo=False,
            codigo=CODIGO_DENEGADO_SIN_PERMISO,
            conflictos=conflictos,
        )

    # G) tiene ambos permisos — falta el motivo humano obligatorio.
    # trim explícito; nunca acepta cadena vacía o solo espacios.
    motivo_normalizado = (sobrecupo_motivo or "").strip()
    if not motivo_normalizado:
        return _decision(
            permitido=False,
            es_sobrecupo_efectivo=False,
            codigo=CODIGO_DENEGADO_SIN_MOTIVO,
            conflictos=conflictos,
            requiere_motivo=True,
        )

    # H) todo válido: conflictos overridables + intención + ambos
    # permisos + motivo humano no vacío.
    return _decision(
        permitido=True,
        es_sobrecupo_efectivo=True,
        codigo=CODIGO_AUTORIZADO,
        conflictos=conflictos,
        requiere_motivo=True,
        motivo_normalizado=motivo_normalizado,
    )
