# -*- coding: utf-8 -*-
"""
SESAES — A.2: fuente única de verdad para disponibilidad de Agenda.

Antes de este módulo existían dos implementaciones independientes de
"¿puede este profesional recibir una cita en esta fecha/hora?":

  - GET /disponibilidad/{profesional_id} (app/routers/horarios.py),
    usado principalmente por Estudiante.
  - getBloqueEstado() en el frontend (dashboard-admin.ts), que
    recalculaba las mismas reglas en TypeScript para pintar la grilla
    de Agenda Admin.

Ninguna de las dos era consultada por POST /citas al insertar, así que
era posible crear una cita fuera de jornada, en colación, en una hora
que ni siquiera pertenece a la grilla real del profesional, o para un
profesional inactivo, solo porque el cliente no lo impidió.

Este módulo concentra la regla de negocio en un solo lugar:
  - `evaluar_disponibilidad_slot()`  → evalúa UN profesional+fecha+hora
    puntual contra las reglas ESTRUCTURALES del slot. La usa POST
    /citas antes de insertar una cita normal, y puede usarla cualquier
    otro router que necesite la misma pregunta.
  - `listar_horas_disponibles()`     → evalúa TODOS los bloques de un
    día para un profesional, aplicando además la política de
    agendamiento propia de Estudiante (ver más abajo). La usa GET
    /disponibilidad/{id}.
  - `excede_ventana_agendamiento_estudiante()` → política de
    agendamiento de Estudiante aplicada explícitamente, no una regla
    estructural del slot (ver sección "Reglas estructurales vs.
    política de consumidor").

Nadie más debe reimplementar jornada/colación/grilla/ocupación de un
slot; deben llamar a este módulo.

── Reglas estructurales vs. política de consumidor (corrección v2) ──

`evaluar_disponibilidad_slot()` NO es solo para Estudiante: también la
usa (o usará) cualquier flujo de creación de citas, incluida una futura
Agenda Admin que necesite agendar/navegar semanas más allá de la
ventana de 7 días que hoy limita a Estudiante. Por eso se separan dos
capas:

  1. Reglas ESTRUCTURALES del slot (viven en evaluar_disponibilidad_slot,
     se aplican SIEMPRE, a cualquier consumidor, porque describen algo
     que es objetivamente falso o imposible sin importar quién
     pregunte). El ORDEN importa: los bloqueos ABSOLUTOS (no
     overridable_con_sobrecupo) se evalúan todos antes que cualquier
     motivo overridable, para que un sobrecupo nunca "se cuele" delante
     de un bloqueo absoluto solo porque ese motivo se evaluó primero:
       - profesional existe / está activo
       - fecha y hora tienen formato válido
       - la fecha no es un día ya pasado
       - el día no cae en fin de semana (el centro no atiende entonces)
       - el día no está marcado DiaCerrado
       - la hora no es un horario ya pasado, si la fecha es hoy
       - la hora corresponde a un bloque real de la grilla CRUDA del
         centro según `duracion_min` (NO overridable: no existe tal
         cosa como "media cita"; evaluada ANTES que jornada — ver
         corrección v3 más abajo)
       - el slot no está ocupado por otra cita pendiente/completada
         (comparando hora normalizada, no el string crudo) — NO
         overridable; evaluada ANTES que jornada/colación (ver
         corrección v4 más abajo)
       - la hora cae dentro de jornada y fuera de colación del
         profesional (overridable con sobrecupo autorizado) —
         evaluada AL FINAL, precisamente por ser el único motivo que
         sobrecupo puede superar

  2. Política de CONSUMIDOR, aplicada explícitamente por encima de lo
     estructural, no dentro de evaluar_disponibilidad_slot:
       - la ventana máxima de 7 días de anticipación es una política de
         agendamiento de Estudiante (`excede_ventana_agendamiento_estudiante`),
         no una regla de que el slot en sí sea inválido. `listar_horas_disponibles()`
         la aplica porque es el endpoint que consume Estudiante. POST /citas
         la vuelve a aplicar explícitamente SOLO cuando quien agenda es el
         propio estudiante (sin agenda.gestionar) — así una llamada directa
         a la API no permite saltarse una restricción que la UI de
         disponibilidad ya le oculta. No se aplica a reservas hechas con
         capacidad administrativa, precisamente para no romper la
         capacidad futura de Agenda Admin de navegar/agendar semanas
         posteriores a esa ventana.

Alcance explícito de A.2 (ver auditoría previa de Agenda V2):
  - Esto NO decide permisos, ownership ni scope administrativo — eso
    sigue siendo responsabilidad de cada router (agenda.gestionar,
    alcance por especialidad, etc.).
  - Esto NO es protección de concurrencia/doble-reserva (eso es A.3,
    deliberadamente fuera de este bloque: no existe un
    UniqueConstraint(profesional_id, fecha, hora) porque debe convivir
    con sobrecupo legítimo — ver comentario ya existente en
    app/routers/admin.py sobre crear_cita_urgente).
  - `urgente=True` (POST /admin/citas/urgente) sigue sin pasar por esta
    validación. A.2 conserva la semántica actual de urgencias y no la
    redefine; su relación definitiva con disponibilidad se decidirá en
    la fase específica de urgencias/emergencias.
  - `sobrecupo=True` en una cita normal (POST /citas) SÍ pasa por acá,
    pero se le permite superar únicamente los motivos marcados como
    `overridable_con_sobrecupo=True` (fuera de jornada, en colación) —
    nunca centro cerrado, fin de semana, fecha/hora pasada, profesional
    inactivo/inexistente, un horario fuera de grilla o un slot ya
    ocupado por otra cita. Esto replica la semántica que ya tenía el
    frontend en `clickBloque()`: el flujo de sobrecupo solo se ofrece
    para 'fuera-horario' y 'colacion', nunca para 'bloqueado' ni
    'cerrado-centro'. El diseño *definitivo* de autorización de
    sobrecupo (quién puede, auditoría dedicada, límites, si algún día
    puede forzar también fuera de grilla) queda para la fase A.4, ya
    identificada en la auditoría.

── Corrección v3 ──

  1. Orden de reglas: la pertenencia a la grilla se evalúa antes que
     jornada/colación en evaluar_disponibilidad_slot (ver sección de
     reglas estructurales arriba). Antes de esta corrección, un valor
     fuera de grilla Y fuera de jornada a la vez (p. ej. "08:07" con
     jornada 09:00-17:00) devolvía motivo "fuera_de_jornada"
     (overridable), permitiendo que sobrecupo=True lo autorizara pese
     a no ser un slot real. Ahora, cualquier hora no alineada a la
     grilla se rechaza con "hora_fuera_de_grilla" (no overridable) sin
     importar si además cae fuera de jornada.
  2. `generar_bloques_jornada()` ya no puede recibir una `duracion_min`
     que produzca un bucle sin avance (0) o sin cota (negativa): un
     valor no-entero o <= 0 se reemplaza por DURACION_MIN_POR_DEFECTO
     antes de generar la grilla. Esto es un blindaje de la función en
     sí, no una validación de negocio de Profesional.

── Corrección v4 ──

  Bug de precedencia: `evaluar_disponibilidad_slot()` comprobaba
  jornada/colación ANTES que ocupación. Como "fuera_de_jornada" y
  "en_colacion" son overridable_con_sobrecupo=True, un slot que estaba
  simultáneamente fuera de jornada Y ya ocupado por otra cita devolvía
  primero el motivo overridable — permitiendo que sobrecupo=True lo
  autorizara sin llegar nunca a comprobar que la hora ya estaba
  reservada. La política vigente es "slot ocupado = bloqueo absoluto;
  fuera de jornada/colación = temporalmente overridable", así que el
  orden de evaluación ahora la refleja: ocupación se comprueba antes
  que `_evaluar_reglas_jornada()`. No cambia ninguna otra decisión ya
  tomada (fin de semana, fuera de grilla, urgencias, etc. siguen
  igual).

── Corrección v5 (A.2B) — núcleo común para slot puntual y rango ──

  A.2B necesita evaluar un rango de días (Agenda Admin → Semana) sin
  hacer una query por celda. En vez de escribir un segundo algoritmo
  de precedencia "parecido" al de evaluar_disponibilidad_slot() (que
  hoy coincide pero mañana podría divergir por un cambio hecho en un
  solo lugar), se extrajo la precedencia completa a una función pura
  sin acceso a base de datos: `_evaluar_slot_en_contexto()`. Recibe el
  profesional, el día cerrado (si aplica) y el set de horas ocupadas
  ya precargados por quien llama, y devuelve la decisión para UN
  slot ya parseado.

  Tanto `evaluar_disponibilidad_slot()` (que sigue siendo la función
  pública para un slot puntual, con su mismo contrato — sigue
  encargándose de resolver profesional/fecha/hora desde parámetros
  crudos y hacer sus propias queries puntuales) como
  `listar_disponibilidad_rango()` (que precarga profesional, días
  cerrados y citas del rango en un puñado de queries, y evalúa cada
  slot generado por `generar_bloques_jornada()` contra ese contexto
  ya en memoria) llaman exactamente a `_evaluar_slot_en_contexto()`
  para decidir cada slot. Ningún router debe reimplementar esta
  precedencia ni copiarla: cualquier consumidor nuevo de "¿está este
  slot disponible?" debe pasar por uno de estos dos puntos de
  entrada.

  `listar_disponibilidad_rango()` no es una función de Semana: recibe
  fecha_inicio/fecha_fin genéricos (con un máximo defensivo de
  `MAX_DIAS_RANGO_DISPONIBILIDAD` días inclusive) para poder
  reutilizarse después en Día/Mes sin crear una tercera fuente.

── Corrección v6 (A.2C) — hardening de duración completa ──

  Hasta acá, `_evaluar_slot_en_contexto()` (y por lo tanto todo lo que
  se apoya en él) evaluaba casi exclusivamente la HORA DE INICIO de una
  cita. Como Cita no tiene `duracion_min` ni `hora_fin` propios — la
  única fuente de duración sigue siendo `Profesional.duracion_min`, vía
  `_duracion_efectiva()` — era posible que una cita "empezara" en un
  slot válido pero su intervalo real terminara dentro de la colación,
  después de la jornada del profesional, después del cierre del centro,
  o solapado con otra cita cuyo inicio era distinto al suyo.

  Esta corrección evalúa el intervalo SEMIABIERTO [inicio, fin), con
  fin = inicio + _duracion_efectiva(profesional):

    - `hora_fuera_de_grilla` sigue evaluándose solo sobre el INICIO:
      no existe "media cita", así que el inicio debe seguir siendo un
      bloque real de la grilla cruda del centro (sin cambios).
    - NUEVO motivo `excede_cierre_centro` (no overridable): el
      intervalo completo no puede terminar después de
      HORA_FIN_CENTRO, aunque el inicio sí pertenezca a la grilla. Es
      un límite del CENTRO, no del profesional, así que sobrecupo
      nunca puede superarlo — ver "Cierres absolutos" del bloque de
      hardening. Se evalúa junto a `hora_fuera_de_grilla`, antes que
      ocupación y que jornada/colación, por ser del mismo tipo
      (estructural, del centro).
    - `slot_ocupado` (no overridable) ahora compara superposición de
      INTERVALOS completos entre la cita nueva y cada cita activa
      existente del profesional/fecha, no solo igualdad de hora de
      inicio — una cita existente 10:00–10:45 y una nueva 09:30–10:15
      ahora sí se detectan como conflicto.
    - `fuera_de_jornada` / `en_colacion` (overridable con sobrecupo,
      sin cambio de política) ahora se calculan sobre superposición
      de intervalos en vez de sobre el punto de inicio — ver
      `_evaluar_reglas_jornada()`.

  Todos los límites SEMIABIERTOS: un intervalo que termina exactamente
  cuando empieza colación/jornada/cierre del centro es válido (no hay
  superposición); uno que la excede aunque sea por un minuto, no.

  `_bloques_grilla_profesional()` (que alimenta `listar_horas_disponibles()`,
  consumida por GET /disponibilidad/{id} de Estudiante) recibió el
  mismo criterio de intervalo completo — antes de esta corrección
  quedaba desalineada de `evaluar_disponibilidad_slot()` (POST /citas):
  ambas ya evaluaban jornada/colación, pero una por punto y otra (tras
  v1-v5) seguía también por punto, así que coincidían por construcción;
  esta corrección las mantiene coincidentes ahora que una de las dos
  pasa a intervalo completo. Sin este ajuste, Estudiante podía ver una
  hora como "disponible" en la lista y que POST /citas la rechazara al
  intentar agendarla — una contradicción que este hardening habría
  introducido de no corregirse acá también.

  A.3 (concurrencia/doble-reserva) sigue completamente fuera de este
  bloque: detectar superposición en una lectura no es protección
  transaccional contra dos peticiones simultáneas — eso se resuelve
  explícitamente en A.3, no acá.

── A.3 — concurrencia / doble reserva ──

  Hasta acá, evaluar_disponibilidad_slot() (y su re-implementación
  paralela para urgente, que no llamaba a nada de este módulo) hacían
  SELECT → INSERT sin ninguna protección transaccional: dos peticiones
  concurrentes podían leer ambas "libre" antes de que cualquiera
  insertara, y las dos citas solapadas quedaban activas. El comentario
  en citas.py que decía que un índice único de la base de datos
  atrapaba esto era FALSO — Cita no tiene, ni tuvo nunca, ningún
  UniqueConstraint/Index sobre (profesional_id, fecha, hora); se
  corrigió ese comentario junto con esta implementación.

  Se agregan dos piezas nuevas, ambas reutilizables desde cualquier
  endpoint que cree citas:

    - `adquirir_lock_agenda_profesional_fecha()`: lock exclusivo de
      ámbito de TRANSACCIÓN, vía pg_advisory_xact_lock(profesional_id,
      YYYYMMDD) en PostgreSQL — no-op explícito en SQLite (no ofrece la
      garantía real; ver test de integración marcado
      @pytest.mark.postgres). Debe adquirirse ANTES de re-evaluar
      disponibilidad/ocupación y ANTES de insertar, dentro de la MISMA
      transacción que hará el INSERT — evaluar antes del lock y confiar
      en que el resultado siga vigente es exactamente la carrera que
      esto existe para cerrar.
    - `hay_solapamiento_con_cita_activa()`: extrae SOLO el criterio de
      superposición de intervalos (ya usado internamente por
      _evaluar_slot_en_contexto()) para que POST /admin/citas/urgente
      pueda protegerse de solapar una cita activa sin arrastrar el
      resto de evaluar_disponibilidad_slot() (grilla, jornada,
      colación, cierre de centro) — ese endpoint deliberadamente no
      evalúa esas reglas (decisión histórica de A.2, ver
      crear_cita_urgente en admin.py) y A.3 no amplía esa decisión,
      solo cierra el hueco de ocupación/concurrencia.

  Una carrera perdida sigue sin poder convertirse en sobrecupo=True:
  `slot_ocupado` sigue siendo overridable_con_sobrecupo=False, sin
  cambios — esta sección solo decide CUÁNDO se evalúa (dentro del
  lock, no antes), no QUÉ motivos son superables.

── A.4.2 — analizador estructurado de conflictos ──

  Hasta acá, la precedencia de motivos vivía como una cadena de
  `if`/`return` con cortocircuito dentro de `_evaluar_slot_en_contexto()`:
  apenas aplicaba un motivo, se devolvía de inmediato y ningún otro
  motivo simultáneo del mismo slot quedaba siquiera evaluado. Eso
  bastaba para decidir disponible/no-disponible, pero no alcanza para
  que una fase futura (aprobación de sobrecupo, notificación,
  unificación de urgencia) pueda razonar sobre TODO lo que está
  pasando con un slot a la vez (p. ej. que exceda el cierre del centro
  Y quede fuera de la jornada del profesional al mismo tiempo).

  UNA SOLA FUENTE DE VERDAD (sin dos motores de disponibilidad):
  `analizar_conflictos_slot()` es ahora la ÚNICA implementación de las
  reglas de un slot con contexto ya válido (profesional activo,
  fecha/hora ya parseadas): función PURA que, en un solo recorrido,
  devuelve TODOS los conflictos reales y simultáneos que sean
  técnicamente determinables, como una tupla de `ConflictoSlot` en el
  mismo orden de precedencia histórico (ver su docstring). Sobre esa
  misma lista se reduce todo lo demás:

    - `_evaluar_slot_en_contexto()` pasó a ser un wrapper delgado:
      llama a `analizar_conflictos_slot()` y, si la lista no está
      vacía, toma el PRIMER conflicto (ya viene en orden de
      precedencia) y lo traduce al mismo
      (disponible, motivo, mensaje, overridable_con_sobrecupo) de
      siempre — mismo motivo, mismo mensaje exacto, mismo
      overridable — vía `_mensaje_legacy_para()`. Si la lista viene
      vacía, el slot está disponible, igual que antes. Ya NO
      reimplementa ninguna condición por su cuenta: no hay dos
      motores de disponibilidad, hay un solo análisis y una reducción
      determinista sobre su resultado.
    - `evaluar_disponibilidad_slot()` llama a `analizar_conflictos_slot()`
      UNA sola vez y reduce esa misma tupla con `_legacy_desde_conflictos()`
      (la misma reducción que usa `_evaluar_slot_en_contexto()`) para
      obtener disponible/motivo/mensaje/overridable — nunca hay una
      segunda llamada independiente al analizador para obtener
      `ResultadoDisponibilidad.conflictos` (nuevo campo, default `()`):
      es literalmente la misma tupla.

  Esto no cambia NINGÚN mensaje HTTP, código de estado, ni el
  contrato de `listar_disponibilidad_rango()` — sigue delegando en
  `_evaluar_slot_en_contexto()`, así que hereda la reducción sin
  cambiar su forma de retorno.

  NO incluye en `analizar_conflictos_slot()` (a propósito):
  `profesional_no_encontrado`, `profesional_inactivo`, `fecha_invalida`,
  `hora_invalida` — son precondiciones que impiden construir el
  contexto que la función recibe (profesional válido, fecha/hora ya
  parseadas); siguen resolviéndose en el wrapper público
  `evaluar_disponibilidad_slot()`, exactamente como antes de A.4.2.
  Para esos casos, `ResultadoDisponibilidad.conflictos` queda `()`
  — el análisis estructurado ni siquiera llega a correr.

  RELOJ DETERMINISTA: `analizar_conflictos_slot()` nunca lee el reloj
  global — el chequeo `hora_pasada` recibe la hora "actual" como
  parámetro inyectado (`ahora: time | None`), nunca vía
  `datetime.now()` interno. `evaluar_disponibilidad_slot()` captura
  `datetime.now()` UNA sola vez por evaluación y pasa ese mismo
  snapshot tanto al análisis estructurado como (indirectamente, por
  ser la misma reducción) a la decisión legacy — no hay forma de que
  ambos discrepen porque el minuto cambió entre dos lecturas del
  reloj, porque ya no hay dos lecturas. `listar_disponibilidad_rango()`
  sigue el mismo patrón que ya usaba para `hoy` (una sola captura
  antes del loop de días/slots, no una por slot). Si `ahora` se omite
  (compatibilidad hacia atrás), el chequeo `hora_pasada` simplemente
  no se evalúa — no se sustituye por una lectura de reloj interna.

  EQUIVALENCIA `overridable_con_sobrecupo` / `hay_bloqueo_absoluto()`:
  documentada únicamente para resultados con contexto analizable, es
  decir, cuando `analizar_conflictos_slot()` corrió de verdad (ver
  docstring de `hay_bloqueo_absoluto()`) — NO es una equivalencia
  universal sobre cualquier `ResultadoDisponibilidad`: los cuatro
  motivos terminales de arriba pueden devolver
  `overridable_con_sobrecupo=False` junto con `conflictos=()`, donde
  `hay_bloqueo_absoluto(())` es `False` por vacuidad — ambos valores
  existen ahí pero no son la misma pregunta.

  A.4.2 no cambia la política de sobrecupo en sí (qué motivos son
  overridable sigue siendo exactamente lo mismo que ya decidía A.2),
  no agrega aprobación, notificación, permisos nuevos, ni toca
  `POST /admin/citas/urgente` — sigue completamente fuera de este
  análisis, igual que en A.2/A.3 (ver arriba).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.cita import Cita
from app.models.dia_cerrado import DiaCerrado
from app.models.profesional import Profesional

# Horario general de atención del centro — mismo valor que ya usaba
# GET /disponibilidad/{id} y que el frontend replica como
# CENTRO_HORA_INICIO/CENTRO_HORA_FIN en dashboard-admin.ts.
HORA_INICIO_CENTRO = time(8, 0)
HORA_FIN_CENTRO = time(18, 0)

# Estados de Cita que efectivamente ocupan un slot. Una cita cancelada
# o marcada como inasistencia no debe seguir bloqueando la hora.
ESTADOS_CITA_QUE_OCUPAN_SLOT = ("pendiente", "completada")

# Duración de bloque a usar cuando la del profesional no es utilizable
# (None, o <= 0 — ver generar_bloques_jornada). No es una validación de
# negocio de mantenimiento de Profesional (eso es de otro módulo);
# acá el único objetivo es que la grilla nunca pueda generarse de forma
# insegura, sin importar qué dato llegue.
DURACION_MIN_POR_DEFECTO = 45


def _duracion_efectiva(profesional: Profesional) -> int:
    """
    Duración de bloque efectiva para este profesional: su
    `duracion_min` si es un entero positivo, o
    DURACION_MIN_POR_DEFECTO en cualquier otro caso (None, 0,
    negativo, no-entero) — exactamente el mismo criterio que ya
    aplica generar_bloques_jornada() puertas adentro (ver su
    docstring, "Blindaje corrección v3").

    Existe para que exista un solo lugar que decida "cuál es la
    duración que realmente se usa": antes de A.2B, un profesional con
    duracion_min negativo o 0 pasaba ese valor crudo a
    generar_bloques_jornada() (que lo neutralizaba puertas adentro y
    usaba 45 para construir la grilla), pero el valor crudo (p. ej.
    -10) se seguía reportando como `duracion_min` en la respuesta —
    dos números distintos describiendo la misma grilla. Ahora tanto la
    grilla como el campo `duracion_min` de la respuesta salen de esta
    misma función.
    """
    valor = profesional.duracion_min
    return valor if isinstance(valor, int) and valor > 0 else DURACION_MIN_POR_DEFECTO

# Ventana máxima de anticipación — política de agendamiento de
# Estudiante, NO una regla estructural del slot (ver docstring del
# módulo). Mismo valor que ya usaba GET /disponibilidad/{id}.
VENTANA_AGENDAMIENTO_ESTUDIANTE_DIAS = 7

# Máximo de días (inclusive) que puede pedir un solo llamado a
# listar_disponibilidad_rango(). Defensivo: evita que un rango
# arbitrariamente grande dispare una respuesta enorme o quede abierto
# a abuso. No está acoplado a "7 días de Semana" a propósito, para
# poder servir después a Día/Mes sin cambiar el contrato (A.2B,
# corrección v5).
MAX_DIAS_RANGO_DISPONIBILIDAD = 31


class ParametrosRangoInvalidosError(ValueError):
    """
    Rango de fechas sintácticamente inválido, invertido, o que supera
    MAX_DIAS_RANGO_DISPONIBILIDAD. `motivo` es un código estable
    (no pensado para mostrarse tal cual) para que quien llama (tests,
    código interno) distinga el tipo de error sin tener que parsear
    `mensaje`. Solo `mensaje` llega al cliente: el router lo usa como
    `detail` del HTTP 400 — `motivo` no viaja en la respuesta HTTP.
    """

    def __init__(self, motivo: str, mensaje: str):
        self.motivo = motivo
        self.mensaje = mensaje
        super().__init__(mensaje)


class ProfesionalNoEncontradoError(LookupError):
    """profesional_id no corresponde a ningún Profesional existente."""


@dataclass(frozen=True)
class ConflictoSlot:
    """
    A.4.2 — un motivo estructurado y determinable de por qué un slot no
    está disponible, producido por `analizar_conflictos_slot()` — la
    ÚNICA implementación de las reglas de un slot con contexto ya
    válido (ver docstring del módulo y de esa función). A diferencia
    del `motivo` único que ya devolvía `evaluar_disponibilidad_slot()`
    desde A.2A, `ConflictoSlot` permite describir TODOS los conflictos
    reales y simultáneos de un slot, no solo el primero en precedencia.

    Campos:
      - `codigo`: el mismo código de motivo de siempre (p. ej.
        "en_colacion", "slot_ocupado") — no un vocabulario nuevo.
      - `categoria`: agrupación estable y determinista del tipo de
        conflicto ("precondicion", "calendario_centro",
        "grilla_centro", "ocupacion", "jornada_profesional") — ver
        docstring de `analizar_conflictos_slot()`.
      - `overridable_con_sobrecupo`: mismo criterio ya vigente por
        código — un sobrecupo autorizado solo puede superar conflictos
        con este flag en True.
      - `metadata`: SIEMPRE JSON-serializable — solo strings
        canónicos ("HH:MM" / "YYYY-MM-DD") e integers, nunca objetos
        `datetime.time`/`date` ni IDs técnicos de citas ocupantes.
    """

    codigo: str
    categoria: str
    overridable_con_sobrecupo: bool
    metadata: dict


def hay_bloqueo_absoluto(conflictos: tuple[ConflictoSlot, ...]) -> bool:
    """
    A.4.2 — True si CUALQUIERA de los conflictos estructurados
    presentes es un bloqueo absoluto (`overridable_con_sobrecupo=False`).

    Regla vigente (sin cambios de política respecto a A.4.1, solo
    ahora expresada sobre la lista completa en vez de un único
    motivo): si existe cualquier conflicto absoluto, el sobrecupo
    actual NO puede autorizarse — sin importar cuántos otros
    conflictos overridables lo acompañen. Si TODOS los conflictos
    presentes son overridables, puede continuar aplicándose la
    política de sobrecupo que ya existía (A.4.1/A.2). Esto no habilita
    ningún caso de sobrecupo nuevo.

    Equivalencia con `ResultadoDisponibilidad.overridable_con_sobrecupo`:
    para resultados con CONTEXTO ANALIZABLE (es decir, cuando
    `analizar_conflictos_slot()` corrió de verdad — ver docstring de
    esa función), `overridable_con_sobrecupo=True` y
    `not hay_bloqueo_absoluto(conflictos)` son siempre equivalentes,
    porque la precedencia histórica evalúa todos los motivos absolutos
    antes que cualquier motivo overridable. Esto NO es una equivalencia
    universal: para las precondiciones terminales de
    `evaluar_disponibilidad_slot()` (profesional_no_encontrado,
    profesional_inactivo, fecha_invalida, hora_invalida), el análisis
    ni siquiera corre — ahí `conflictos` es `()`,
    `hay_bloqueo_absoluto(())` es `False` por vacuidad, y sin embargo
    `overridable_con_sobrecupo` sigue siendo `False`. Por eso la
    autorización de sobrecupo en POST /citas comprueba AMBAS
    condiciones explícitamente (ver app/routers/citas.py), no una sola
    asumiendo que son intercambiables en todos los casos.
    """
    return any(not conflicto.overridable_con_sobrecupo for conflicto in conflictos)


@dataclass(frozen=True)
class ResultadoDisponibilidad:
    """Resultado de evaluar un profesional+fecha+hora puntual."""

    disponible: bool
    motivo: str | None
    mensaje: str | None
    # Si True, un sobrecupo autorizado (agenda.gestionar) puede
    # superar este motivo. Si False, es un bloqueo absoluto que ni
    # sobrecupo puede saltarse.
    overridable_con_sobrecupo: bool
    profesional: Profesional | None
    # A.4.2 — TODOS los conflictos estructurados reales y determinables
    # del slot (no solo el motivo ganador de arriba), en el mismo orden
    # de precedencia histórico. Default () para no romper ningún
    # caller/test existente que construya ResultadoDisponibilidad
    # posicionalmente sin este campo. Queda vacío en los casos en los
    # que ni siquiera existe contexto suficiente para analizar
    # (profesional_no_encontrado, profesional_inactivo, fecha_invalida,
    # hora_invalida) — ver analizar_conflictos_slot() y
    # hay_bloqueo_absoluto().
    conflictos: tuple[ConflictoSlot, ...] = ()


def _parsear_hora_24h(hora_str: str | None) -> time | None:
    """'HH:MM' (24h) -> time, o None si es inválida/vacía."""
    if not hora_str:
        return None
    try:
        return datetime.strptime(hora_str, "%H:%M").time()
    except ValueError:
        return None


def _parsear_hora_flexible(hora_str: str | None) -> time | None:
    """
    Igual que _parsear_hora_24h pero además acepta 'HH:MM AM/PM', el
    mismo formato de compatibilidad que ya toleraba
    citas.py:_cita_a_datetime (ver comentario en schemas.CitaCreate).

    Se usa siempre que dos horas deban compararse por su valor real
    (p. ej. "09:30" y "09:30 AM" son el mismo slot) en vez de por
    igualdad de string — corrección v2 del punto 3.
    """
    hora = _parsear_hora_24h(hora_str)
    if hora is not None:
        return hora
    if not hora_str:
        return None
    try:
        return datetime.strptime(hora_str, "%I:%M %p").time()
    except ValueError:
        return None


def _parsear_fecha(fecha_str: str | None) -> date | None:
    if not fecha_str:
        return None
    try:
        return datetime.strptime(fecha_str, "%Y-%m-%d").date()
    except ValueError:
        return None


def _fin_intervalo(hora_obj: time, duracion_min: int) -> tuple[time, bool]:
    """
    Hora de término del intervalo semiabierto [hora_obj, hora_obj +
    duracion_min) — hardening de duración completa (A.2C).

    `time` no sabe sumar minutos directamente, así que el cálculo se
    hace combinando con una fecha arbitraria (hoy) y sumando un
    timedelta; solo se usa el resultado, nunca la fecha de apoyo.

    Devuelve (hora_fin, cruza_medianoche). `cruza_medianoche` es True
    si el intervalo se extendería más allá de las 23:59 del mismo día.
    Ningún profesional real debería alcanzar este caso (el centro
    cierra a HORA_FIN_CENTRO), pero se señala explícitamente para que
    quien llama no compare por error `hora_fin.time()` — que "envuelve"
    a una hora pequeña del día siguiente — contra HORA_FIN_CENTRO como
    si fuera una hora temprana válida. Quien llama debe tratar
    cruza_medianoche=True como "excede el cierre operativo del centro"
    sin más cálculo.
    """
    base = datetime.combine(date.today(), hora_obj)
    fin = base + timedelta(minutes=duracion_min)
    return fin.time(), fin.date() != base.date()


def _intervalos_se_superponen(
    inicio_a: time, fin_a: time, inicio_b: time, fin_b: time,
) -> bool:
    """
    True si los intervalos semiabiertos [inicio_a, fin_a) y
    [inicio_b, fin_b) se superponen en algún punto — hardening de
    duración completa (A.2C).

    Semántica semiabierta: un intervalo que termina exactamente cuando
    el otro empieza NO se considera superposición (p. ej. una cita
    12:15–13:00 no invade una colación 13:00–14:00; ver ejemplos del
    bloque de hardening).
    """
    return inicio_a < fin_b and fin_a > inicio_b


def generar_bloques_jornada(duracion_min: int) -> list[time]:
    """
    Genera la lista de horas posibles entre HORA_INICIO_CENTRO y
    HORA_FIN_CENTRO, según la duración de bloque del profesional.

    Esta es LA única fuente de la grilla real de un profesional — tanto
    la validación de un slot puntual como el listado de horas libres
    para un día deben generar sus candidatos a partir de esta misma
    función (corrección v2 del punto 1: nadie evalúa una hora que no
    haya salido de acá).

    Blindaje (corrección v3, punto 2): una `duracion_min` inválida
    (0, negativa, o no-entera) nunca debe poder producir un bucle que
    no avance — con 0 el bucle jamás terminaría, y con un valor
    negativo `actual` retrocedería en vez de avanzar hacia `fin`, en
    ambos casos sin cota. Se usa DURACION_MIN_POR_DEFECTO en esos
    casos en vez de propagar el valor tal cual. Esto es defensivo a
    nivel de esta función — no reemplaza ninguna validación de datos
    que deba existir en el mantenimiento de Profesional.
    """
    duracion = (
        duracion_min
        if isinstance(duracion_min, int) and duracion_min > 0
        else DURACION_MIN_POR_DEFECTO
    )
    bloques: list[time] = []
    actual = datetime.combine(date.today(), HORA_INICIO_CENTRO)
    fin = datetime.combine(date.today(), HORA_FIN_CENTRO)
    while actual < fin:
        bloques.append(actual.time())
        actual += timedelta(minutes=duracion)
    return bloques


def _evaluar_reglas_jornada(
    *,
    profesional: Profesional,
    hora_obj: time,
    fin_obj: time,
) -> tuple[bool, str | None, str | None, bool]:
    """
    Reglas de jornada/colación contra un profesional y el INTERVALO
    completo [hora_obj, fin_obj) de la cita — hardening de duración
    completa (A.2C). Antes de este hardening solo se comprobaba
    hora_obj (el instante de inicio); ahora una cita que empieza dentro
    de jornada/fuera de colación pero cuya duración la hace terminar
    después de la jornada, o invadir la colación, también se rechaza.
    No toca la base de datos — se puede llamar en loop sin costo de
    queries adicionales.

    - fuera_de_jornada: el intervalo empieza antes de que el
      profesional entre (hora_obj < jornada_inicio) O termina después
      de que sale (fin_obj > jornada_fin). Se generaliza el chequeo
      anterior de "hora_obj fuera de [jornada_inicio, jornada_fin)":
      cualquier hora_obj que antes violaba esa condición sigue
      violando esta (fin_obj > hora_obj siempre, por duración > 0), así
      que ningún caso previamente rechazado pasa a aceptarse.
    - en_colacion: el intervalo se superpone en cualquier punto con
      [almuerzo_inicio, almuerzo_fin), no solo si hora_obj cae dentro.
      Un intervalo que termina justo cuando empieza la colación (o que
      empieza justo cuando esta termina) NO se considera invasión —
      ver _intervalos_se_superponen().

    Ambos motivos siguen siendo overridable_con_sobrecupo=True, sin
    cambios de política respecto a antes del hardening — solo cambia
    QUÉ intervalo se evalúa, no qué motivos existen ni si son
    superables por sobrecupo.

    Devuelve (disponible, motivo, mensaje, overridable_con_sobrecupo).
    """
    jornada_inicio = _parsear_hora_24h(profesional.horario_inicio)
    jornada_fin = _parsear_hora_24h(profesional.horario_fin)
    if jornada_inicio and jornada_fin and (hora_obj < jornada_inicio or fin_obj > jornada_fin):
        return (
            False,
            "fuera_de_jornada",
            "La hora solicitada está fuera del horario habitual del profesional.",
            True,
        )

    almuerzo_inicio = _parsear_hora_24h(profesional.hora_almuerzo_inicio)
    almuerzo_fin = _parsear_hora_24h(profesional.hora_almuerzo_fin)
    if almuerzo_inicio and almuerzo_fin and _intervalos_se_superponen(
        hora_obj, fin_obj, almuerzo_inicio, almuerzo_fin,
    ):
        return (
            False,
            "en_colacion",
            "La hora solicitada cae en el horario de colación del profesional.",
            True,
        )

    return True, None, None, False


def _bloques_grilla_profesional(profesional: Profesional) -> list[time]:
    """
    Bloques de la grilla real de este profesional: los generados por
    generar_bloques_jornada() según su duracion_min, filtrados por sus
    propias reglas de jornada/colación. Esta es la lista de "horas que
    existen" para el profesional — tanto para publicarlas (listar) como
    para validar que una hora puntual pertenezca a ella (evaluar).

    Hardening de duración completa (A.2C): un bloque solo se publica si
    su intervalo COMPLETO [b, b+duracion) cabe dentro del cierre
    operativo del centro y de la jornada/colación del profesional — no
    solo si su hora de inicio lo hace. Antes de este cambio, esta
    función (que alimenta listar_horas_disponibles(), consumida por
    Estudiante) seguía filtrando solo por hora de inicio aunque
    evaluar_disponibilidad_slot() (POST /citas) ya evaluara el
    intervalo completo: Estudiante podía ver una hora como "disponible"
    en la lista y que, al intentar agendarla, POST /citas la rechazara
    por invadir colación/jornada o exceder el cierre del centro. Ambas
    rutas comparten ahora exactamente el mismo criterio de intervalo
    completo (ver _evaluar_reglas_jornada, _fin_intervalo).
    """
    duracion = _duracion_efectiva(profesional)
    bloques = generar_bloques_jornada(duracion)
    disponibles = []
    for b in bloques:
        fin_obj, cruza_medianoche = _fin_intervalo(b, duracion)
        if cruza_medianoche or fin_obj > HORA_FIN_CENTRO:
            continue
        if _evaluar_reglas_jornada(profesional=profesional, hora_obj=b, fin_obj=fin_obj)[0]:
            disponibles.append(b)
    return disponibles


def _horas_ocupadas_normalizadas(
    db: Session,
    *,
    profesional_id: int,
    fecha: str,
) -> set[time]:
    """
    Horas ya ocupadas por una cita pendiente/completada de este
    profesional en esta fecha, normalizadas a `time` — para que
    "09:30" y "09:30 AM" se reconozcan como el mismo slot ocupado
    (corrección v2 del punto 3). Ignora silenciosamente cualquier
    `Cita.hora` que no se pueda parsear en ningún formato conocido, en
    vez de romper la evaluación de disponibilidad por un dato legado
    inválido.
    """
    filas = (
        db.query(Cita.hora)
        .filter(
            Cita.profesional_id == profesional_id,
            Cita.fecha == fecha,
            Cita.estado.in_(ESTADOS_CITA_QUE_OCUPAN_SLOT),
        )
        .all()
    )
    ocupadas = set()
    for (hora_str,) in filas:
        hora_obj = _parsear_hora_flexible(hora_str)
        if hora_obj is not None:
            ocupadas.add(hora_obj)
    return ocupadas


class SlotInvalidoError(Exception):
    """
    A.3 (v2) — señal de dominio explícita: `hay_solapamiento_con_cita_activa()`
    no pudo interpretar `fecha` u `hora`.

    Deliberadamente NO es un `bool False`: False significaría "sí es
    interpretable, y no hay solapamiento" — un fail-open peligroso para
    POST /admin/citas/urgente, que no pasa por
    evaluar_disponibilidad_slot() y por lo tanto no tiene ninguna otra
    red de validación de formato aguas arriba. Quien la atrape debe
    responder 400 (dato inválido, no autorización ni conflicto) y NO
    continuar como si el slot estuviera libre, ni insertar nada.

    Esto NO amplía la validación de urgente a jornada/colación/grilla
    (sigue sin evaluarlas, ver docstring de crear_cita_urgente en
    admin.py) — solo garantiza que fecha/hora sean interpretables antes
    de poder comprobar ocupación con seguridad.
    """

    def __init__(self, mensaje: str):
        super().__init__(mensaje)
        self.mensaje = mensaje


def canonicalizar_fecha_valida(fecha: str) -> str:
    """
    A.3 (v3) — helper compartido de canonicalización: parsea `fecha`
    UNA sola vez y devuelve su forma canónica ("%Y-%m-%d",
    `fecha_obj.isoformat()`), para que cada canal que crea una cita
    (POST /citas, POST /admin/citas/urgente) reutilice exactamente el
    mismo valor en TODA decisión crítica de ese flujo: consulta de
    DiaCerrado, adquirir_lock_agenda_profesional_fecha(),
    evaluar_disponibilidad_slot()/hay_solapamiento_con_cita_activa(), y
    el INSERT de Cita.fecha.

    Corrección del hueco detectado en A.3 (v2): _parsear_fecha() acepta
    con strptime variantes no zero-padded ("2026-9-1") además de la
    forma canónica ("2026-09-01") — ambas representan la MISMA fecha
    real, pero son strings DISTINTOS. adquirir_lock_agenda_profesional_
    fecha() y hay_solapamiento_con_cita_activa()/evaluar_disponibilidad_
    slot() ya canonicalizaban internamente antes de construir su clave
    de lock o su query de ocupación — pero ambos routers seguían
    creando la fila `Cita` con `fecha=cita.fecha` crudo. Dos peticiones
    para la MISMA fecha real, escritas con distinto formato de entrada
    ("2026-9-1" vs "2026-09-01"), adquirían el MISMO advisory lock (las
    claves ya eran canónicas), pero la segunda podía consultar
    ocupación con un string que no coincidía byte a byte con la fila
    que la primera acababa de guardar —un falso "no hay solapamiento"
    que invalidaba la garantía fuerte de A.3 pese al lock compartido.

    Lanza SlotInvalidoError si `fecha` no es interpretable — NUNCA
    normaliza ni "adivina" una fecha inválida en silencio (p. ej. a la
    fecha de hoy) solo para poder seguir. Quien llama debe traducir esa
    excepción a 400 y no continuar con ninguna query ni INSERT.
    """
    fecha_obj = _parsear_fecha(fecha)
    if fecha_obj is None:
        raise SlotInvalidoError("Fecha inválida.")
    return fecha_obj.isoformat()


def _hay_solapamiento_con_ocupadas(
    hora_obj: time,
    fin_obj: time,
    ocupadas: set[time],
    duracion: int,
) -> bool:
    """
    True si el intervalo [hora_obj, fin_obj) se superpone con alguna
    de las horas de `ocupadas` (cada una interpretada como
    [ocupada, ocupada + duracion), la misma duración efectiva del
    profesional — ver diagnóstico de A.2C/A.3: Cita no guarda su
    propia duración).

    Extraída de _evaluar_slot_en_contexto() (A.3) para que
    hay_solapamiento_con_cita_activa() —usada por
    POST /admin/citas/urgente— pueda reutilizar EXACTAMENTE este mismo
    criterio de intervalos sin duplicar la lógica ni arrastrar el
    resto de las reglas de disponibilidad (grilla, jornada, colación,
    cierre de centro), que no le corresponden a ese endpoint.
    """
    for ocupada in ocupadas:
        ocupada_fin, _ = _fin_intervalo(ocupada, duracion)
        if _intervalos_se_superponen(hora_obj, fin_obj, ocupada, ocupada_fin):
            return True
    return False


def hay_solapamiento_con_cita_activa(
    db: Session,
    *,
    profesional: Profesional,
    fecha: str,
    hora: str,
) -> bool:
    """
    A.3 — True si el intervalo [hora, hora+duracion) de `profesional`
    en `fecha` se superpone con alguna cita activa existente (estado
    en ESTADOS_CITA_QUE_OCUPAN_SLOT) de ese mismo profesional/fecha.

    Consulta la base de datos (a diferencia de _evaluar_slot_en_contexto,
    que es puro). Pensada para POST /admin/citas/urgente: ese endpoint
    deliberadamente NO llama a evaluar_disponibilidad_slot() (no evalúa
    grilla, jornada, colación ni cierre de centro — decisión histórica
    de A.2, ver docstring de crear_cita_urgente en admin.py, que A.3 no
    amplía). Pero SÍ debe protegerse de solapar una cita activa
    existente (objetivo mínimo de A.3), así que expone solo esa parte
    de la lógica, reutilizando literalmente las mismas piezas que usa
    el núcleo de disponibilidad (_horas_ocupadas_normalizadas,
    _fin_intervalo, _intervalos_se_superponen, _duracion_efectiva) en
    vez de reimplementarlas.

    Debe llamarse DESPUÉS de adquirir
    adquirir_lock_agenda_profesional_fecha() para la misma
    (profesional_id, fecha), dentro de la misma transacción — de lo
    contrario sigue existiendo la carrera SELECT→INSERT que A.3 busca
    cerrar.

    A.3 (v2) — dos correcciones de seguridad de datos:

    1. `fecha`/`hora` no parseables lanzan SlotInvalidoError, NUNCA
       devuelven False. Un `hora`/`fecha` inválida "resuelta como sin
       solapamiento" sería fail-open: como urgente no pasa por
       evaluar_disponibilidad_slot(), nada más aguas arriba rechazaría
       ese dato, y la cita se insertaría igual. Quien llama (ver
       admin.py) debe traducir esta excepción a 400 y NO insertar.
    2. La consulta de ocupación usa `fecha_obj.isoformat()` (la forma
       canónica "%Y-%m-%d" que produce _parsear_fecha), NUNCA el
       string crudo recibido. `Cita.fecha` es una columna String
       comparada por igualdad exacta; _parsear_fecha() acepta con
       strptime formatos como "2026-9-1" (sin ceros de relleno) además
       de "2026-09-01", y ambos representan la MISMA fecha pero son
       strings distintos. Consultar con el string crudo podría no
       encontrar una cita ya existente guardada en su forma canónica
       (falso "no hay solapamiento"); canonicalizar antes de consultar
       lo evita — mismo principio que la normalización de `hora` que
       ya hace _horas_ocupadas_normalizadas() para "HH:MM" vs
       "HH:MM AM/PM".
    """
    fecha_obj = _parsear_fecha(fecha)
    if fecha_obj is None:
        raise SlotInvalidoError("Fecha inválida.")

    hora_obj = _parsear_hora_flexible(hora)
    if hora_obj is None:
        raise SlotInvalidoError("Hora inválida.")

    duracion = _duracion_efectiva(profesional)
    fin_obj, _cruza_medianoche = _fin_intervalo(hora_obj, duracion)
    ocupadas = _horas_ocupadas_normalizadas(
        db, profesional_id=profesional.id, fecha=fecha_obj.isoformat(),
    )
    return _hay_solapamiento_con_ocupadas(hora_obj, fin_obj, ocupadas, duracion)


def adquirir_lock_agenda_profesional_fecha(
    db: Session,
    *,
    profesional_id: int,
    fecha: str,
) -> None:
    """
    A.3 — concurrencia/doble-reserva: sección crítica compartida por
    TODOS los canales que crean citas (POST /citas y
    POST /admin/citas/urgente).

    Adquiere un lock exclusivo de ámbito de TRANSACCIÓN, identificado
    por (profesional_id, fecha). Se libera automáticamente al hacer
    commit o rollback de `db` — nunca hay que liberarlo a mano. Debe
    llamarse ANTES de re-evaluar ocupación/disponibilidad y ANTES de
    insertar la Cita, dentro de la MISMA sesión que hará ese INSERT: la
    sesión por-request de app.database.get_db() (autocommit=False) ya
    vive exactamente ese ciclo de vida (se abre al primer query del
    request, se cierra en su `finally`), así que basta con no abrir
    una conexión aparte para el lock.

    Patrón obligatorio en cada endpoint (ver A.3):
        adquirir_lock_agenda_profesional_fecha(db, ...)   # 1. lock
        resultado = evaluar_disponibilidad_slot(db, ...)  # 2. RE-evaluar
        ...                                                # 3. INSERT si libre
        db.commit()                                        # 4. libera el lock

    Evaluar disponibilidad ANTES del lock y confiar en que ese
    resultado siga vigente al insertar es exactamente la carrera que
    A.3 existe para cerrar (dos lecturas ven "libre", ambas insertan).

    Claves (int4, sin usar hash() de Python —no es determinista entre
    procesos— ni hashtext(), evitable si se puede construir una clave
    determinista sin hash):
      - key1 = profesional_id: PK autoincremental, cabe holgadamente
        en int4 (máx. 2_147_483_647).
      - key2 = fecha como entero YYYYMMDD (p. ej. "2026-09-11" ->
        20260911); el máximo teórico, 99991231, también cabe
        holgadamente en int4. Si `fecha` no es parseable, se usa 0
        como key2 — un único "balde" para fechas inválidas de ese
        profesional: nunca habrá un INSERT real bajo esa key, porque
        evaluar_disponibilidad_slot() (o la validación propia de
        urgente) ya rechaza una fecha inválida antes de llegar al
        INSERT, así que no hace falta —ni es posible— una clave más
        fina para ese caso.

    Comportamiento por dialecto — explícito, nunca asumido en
    silencio:
      - postgresql: adquiere pg_advisory_xact_lock(key1, key2) real.
        Es la BD de producción; acá es donde la garantía es real.
      - sqlite: no-op. SQLite no tiene locks advisory de sesión, y toda
        la suite de tests actual corre contra sqlite:///:memory:. Un
        no-op EXPLÍCITO dice claramente en el código que SQLite no
        ofrece la garantía real de A.3 — el aislamiento real de
        concurrencia se valida en el test de integración marcado
        @pytest.mark.postgres (ver tests_a3_concurrencia_postgres.py),
        no en la suite rápida.
      - cualquier otro dialecto: falla explícitamente (RuntimeError) en
        vez de dejar pasar en silencio una operación sin ninguna
        protección real — un error ruidoso en desarrollo es preferible
        a una falsa sensación de seguridad en producción bajo un motor
        no contemplado.
    """
    dialecto = db.get_bind().dialect.name

    if dialecto == "postgresql":
        fecha_obj = _parsear_fecha(fecha)
        key2 = int(fecha_obj.strftime("%Y%m%d")) if fecha_obj is not None else 0
        db.execute(
            text("SELECT pg_advisory_xact_lock(:key1, :key2)"),
            {"key1": profesional_id, "key2": key2},
        )
        return

    if dialecto == "sqlite":
        return

    raise RuntimeError(
        f"A.3: no hay estrategia de lock de concurrencia definida para "
        f"el dialecto {dialecto!r}. No se debe asumir en silencio que "
        f"existe protección contra doble reserva en un motor de base "
        f"de datos no contemplado explícitamente."
    )


def excede_ventana_agendamiento_estudiante(
    fecha: str,
    *,
    ventana_dias: int = VENTANA_AGENDAMIENTO_ESTUDIANTE_DIAS,
) -> bool:
    """
    True si `fecha` cae más allá de la ventana de anticipación que se
    le permite agendar a un Estudiante (política de consumidor, no
    regla estructural del slot — ver docstring del módulo).

    Una fecha con formato inválido devuelve False acá: ese caso ya lo
    rechaza evaluar_disponibilidad_slot/listar_horas_disponibles con su
    propio motivo ("fecha inválida"), no corresponde duplicarlo aquí.
    """
    fecha_obj = _parsear_fecha(fecha)
    if fecha_obj is None:
        return False
    return fecha_obj > (date.today() + timedelta(days=ventana_dias))


# Mismo texto exacto que devolvía _evaluar_slot_en_contexto() antes de
# A.4.2 para cada motivo — ver _mensaje_legacy_para(). "dia_cerrado" no
# está acá porque su mensaje depende de dia_cerrado.motivo (se arma
# aparte, a partir de la metadata del propio ConflictoSlot).
_MENSAJES_LEGACY_POR_CODIGO: dict[str, str] = {
    "fecha_pasada": "No es posible agendar en una fecha que ya pasó.",
    "fin_de_semana": "El centro no atiende los fines de semana.",
    "hora_pasada": "Esa hora ya pasó.",
    "hora_fuera_de_grilla": "Esa hora no corresponde a un bloque de atención válido.",
    "excede_cierre_centro": "La duración de la cita excede el horario de atención del centro.",
    "slot_ocupado": "Esa hora ya está reservada.",
    "fuera_de_jornada": "La hora solicitada está fuera del horario habitual del profesional.",
    "en_colacion": "La hora solicitada cae en el horario de colación del profesional.",
}


def _mensaje_legacy_para(conflicto: ConflictoSlot) -> str:
    """
    Traduce un ConflictoSlot al mismo texto EXACTO que devolvía
    _evaluar_slot_en_contexto() para ese motivo antes de A.4.2 — ver
    `_MENSAJES_LEGACY_POR_CODIGO`. "dia_cerrado" es el único caso
    dinámico (depende del motivo del DiaCerrado real), reconstruido
    acá a partir de `conflicto.metadata["motivo_dia_cerrado"]` en vez
    de recibir el objeto DiaCerrado por separado — así el wrapper
    legacy solo necesita el ConflictoSlot, ninguna otra entrada.
    """
    if conflicto.codigo == "dia_cerrado":
        motivo_dia_cerrado = conflicto.metadata.get("motivo_dia_cerrado") or ""
        return f"El centro permanece cerrado ese día. {motivo_dia_cerrado}".strip()
    return _MENSAJES_LEGACY_POR_CODIGO[conflicto.codigo]


def analizar_conflictos_slot(
    *,
    profesional: Profesional,
    fecha_obj: date,
    hora_obj: time,
    hoy: date,
    dia_cerrado: DiaCerrado | None,
    ocupadas: set[time],
    bloques_grilla: list[time] | None = None,
    ahora: time | None = None,
) -> tuple[ConflictoSlot, ...]:
    """
    A.4.2 — ÚNICA implementación de las reglas de UN slot ya parseado
    (profesional, fecha, hora) con contexto ya válido: todo lo que
    necesita (profesional, si el día está cerrado, qué horas están
    ocupadas) ya viene precargado por quien llama, igual que antes
    tomaba `_evaluar_slot_en_contexto()`. Función PURA: no toca base
    de datos NI el reloj global (ver "RELOJ DETERMINISTA" más abajo).

    UNA SOLA FUENTE DE VERDAD: esta función reemplaza por completo la
    cadena de `if`/`return` con cortocircuito que antes vivía en
    `_evaluar_slot_en_contexto()` — esa función ya NO reimplementa
    ninguna condición por su cuenta, es un wrapper delgado que llama
    acá y reduce el resultado (ver `_legacy_desde_conflictos()`). No
    hay dos motores de disponibilidad: cada condición de acá es la
    ÚNICA fuente de ese motivo, tanto para el análisis estructurado
    como para la decisión legacy de siempre.

    A diferencia de la cadena legacy original (que cortaba en el
    primer motivo que aplicaba), acá cada condición se evalúa siempre,
    sin importar si una condición anterior ya aplicó — así un slot
    puede acumular, por ejemplo, `slot_ocupado` + `fuera_de_jornada` +
    `en_colacion` a la vez, o `excede_cierre_centro` +
    `fuera_de_jornada`. Se devuelven TODOS los conflictos reales y
    simultáneos que sean técnicamente determinables, en un orden
    SIEMPRE determinista — el mismo orden de precedencia histórico:

        fecha_pasada, fin_de_semana, dia_cerrado, hora_pasada,
        hora_fuera_de_grilla, excede_cierre_centro, slot_ocupado,
        fuera_de_jornada, en_colacion

    Ese orden es lo que le permite a `_legacy_desde_conflictos()`
    tomar `conflictos[0]` como "el motivo ganador legacy" sin
    necesitar ninguna lógica de precedencia aparte: el primer elemento
    de la tupla YA es, por construcción, el de mayor precedencia
    histórica presente.

    RELOJ DETERMINISTA: el chequeo `hora_pasada` compara `hora_obj`
    contra `ahora` — la hora "actual" INYECTADA por quien llama, nunca
    `datetime.now()` leído acá dentro. Si `ahora` es `None`
    (compatibilidad hacia atrás con callers que no la necesitan, p.
    ej. tests que ya fijan `fecha_obj != hoy`), el chequeo
    `hora_pasada` simplemente no se evalúa — no se sustituye por una
    lectura de reloj interna, porque eso reintroduciría exactamente el
    problema que se corrigió: dos lecturas del reloj en momentos
    distintos podrían discrepar entre sí. Quien necesite este chequeo
    debe pasar `ahora` explícitamente (ver `evaluar_disponibilidad_slot()`
    y `listar_disponibilidad_rango()`, que capturan `datetime.now()`
    UNA sola vez y reutilizan ese mismo snapshot).

    NO incluye (a propósito, ver docstring del módulo):
      - `profesional_no_encontrado`, `profesional_inactivo`,
        `fecha_invalida`, `hora_invalida`: son precondiciones que
        impiden construir el contexto que esta función recibe
        (profesional válido, fecha/hora ya parseadas) — son
        terminales y se resuelven en el wrapper de servicio
        (`evaluar_disponibilidad_slot()`), nunca acá. Esta función no
        parsea strings ni hace queries.

    Categorías (deterministas, estables — ver ConflictoSlot):
      - "precondicion": `fecha_pasada`, `hora_pasada` — validez
        temporal básica del slot.
      - "calendario_centro": `fin_de_semana`, `dia_cerrado` — el
        centro no atiende ese día, sin importar profesional/hora.
      - "grilla_centro": `hora_fuera_de_grilla`, `excede_cierre_centro`
        — límites estructurales del centro (grilla cruda / cierre
        operativo), no de un profesional en particular.
      - "ocupacion": `slot_ocupado` — otra cita activa ya ocupa el
        intervalo.
      - "jornada_profesional": `fuera_de_jornada`, `en_colacion` —
        únicos códigos con `overridable_con_sobrecupo=True`.

    `bloques_grilla`, igual que antes en `_evaluar_slot_en_contexto()`,
    evita recalcular `generar_bloques_jornada()` cuando quien llama ya
    la tiene (`listar_disponibilidad_rango()` la calcula una vez para
    todo el rango); si se omite, se calcula acá.
    """
    conflictos: list[ConflictoSlot] = []

    fecha_iso = fecha_obj.isoformat()
    hora_hhmm = hora_obj.strftime("%H:%M")

    # ── precondicion: fecha_pasada ──
    if fecha_obj < hoy:
        conflictos.append(ConflictoSlot(
            codigo="fecha_pasada",
            categoria="precondicion",
            overridable_con_sobrecupo=False,
            metadata={"fecha_solicitada": fecha_iso},
        ))

    # ── calendario_centro: fin_de_semana ──
    if fecha_obj.weekday() >= 5:
        conflictos.append(ConflictoSlot(
            codigo="fin_de_semana",
            categoria="calendario_centro",
            overridable_con_sobrecupo=False,
            metadata={"fecha_solicitada": fecha_iso},
        ))

    # ── calendario_centro: dia_cerrado ──
    if dia_cerrado is not None:
        conflictos.append(ConflictoSlot(
            codigo="dia_cerrado",
            categoria="calendario_centro",
            overridable_con_sobrecupo=False,
            metadata={
                "fecha_solicitada": fecha_iso,
                "motivo_dia_cerrado": dia_cerrado.motivo or None,
            },
        ))

    # ── precondicion: hora_pasada ──
    # Ver "RELOJ DETERMINISTA" arriba: si ahora es None, este chequeo
    # no se evalúa — nunca se lee el reloj internamente.
    if ahora is not None and fecha_obj == hoy and hora_obj <= ahora:
        conflictos.append(ConflictoSlot(
            codigo="hora_pasada",
            categoria="precondicion",
            overridable_con_sobrecupo=False,
            metadata={"hora_solicitada": hora_hhmm},
        ))

    duracion = _duracion_efectiva(profesional)
    bloques = (
        bloques_grilla
        if bloques_grilla is not None
        else generar_bloques_jornada(duracion)
    )

    # ── grilla_centro: hora_fuera_de_grilla ──
    # Solo sobre el INICIO, igual que el criterio legacy: no existe
    # "media cita", así que esto es independiente de cuánto dure el
    # intervalo.
    if hora_obj not in bloques:
        conflictos.append(ConflictoSlot(
            codigo="hora_fuera_de_grilla",
            categoria="grilla_centro",
            overridable_con_sobrecupo=False,
            metadata={"hora_solicitada": hora_hhmm},
        ))

    # A partir de acá, igual que antes, se necesita el intervalo
    # completo [hora_obj, fin_obj) — ver "Hardening de duración
    # completa (A.2C)" en el docstring del módulo.
    fin_obj, cruza_medianoche = _fin_intervalo(hora_obj, duracion)
    fin_hhmm = fin_obj.strftime("%H:%M")

    # ── grilla_centro: excede_cierre_centro ──
    # Independiente de hora_fuera_de_grilla: el cierre del centro es
    # un límite del intervalo completo, no del punto de inicio.
    if cruza_medianoche or fin_obj > HORA_FIN_CENTRO:
        conflictos.append(ConflictoSlot(
            codigo="excede_cierre_centro",
            categoria="grilla_centro",
            overridable_con_sobrecupo=False,
            metadata={
                "inicio_solicitado": hora_hhmm,
                "fin_solicitado": fin_hhmm,
                "duracion_min": duracion,
                "hora_fin_centro": HORA_FIN_CENTRO.strftime("%H:%M"),
            },
        ))

    # ── ocupacion: slot_ocupado ──
    # Independiente de todo lo anterior: otra cita activa puede ocupar
    # el intervalo sin importar si además excede jornada/cierre.
    if _hay_solapamiento_con_ocupadas(hora_obj, fin_obj, ocupadas, duracion):
        conflictos.append(ConflictoSlot(
            codigo="slot_ocupado",
            categoria="ocupacion",
            overridable_con_sobrecupo=False,
            metadata={
                "inicio_solicitado": hora_hhmm,
                "fin_solicitado": fin_hhmm,
                "duracion_min": duracion,
            },
        ))

    # ── jornada_profesional: fuera_de_jornada ──
    # A diferencia de la cadena legacy original (mutuamente excluyente
    # con en_colacion por cortocircuito), esto NO se cortocircuita:
    # ambos se evalúan siempre, para poder devolver los dos si el
    # intervalo realmente incurre en ambos a la vez.
    jornada_inicio = _parsear_hora_24h(profesional.horario_inicio)
    jornada_fin = _parsear_hora_24h(profesional.horario_fin)
    if jornada_inicio and jornada_fin and (
        hora_obj < jornada_inicio or fin_obj > jornada_fin
    ):
        conflictos.append(ConflictoSlot(
            codigo="fuera_de_jornada",
            categoria="jornada_profesional",
            overridable_con_sobrecupo=True,
            metadata={
                "jornada_inicio": jornada_inicio.strftime("%H:%M"),
                "jornada_fin": jornada_fin.strftime("%H:%M"),
                "inicio_solicitado": hora_hhmm,
                "fin_solicitado": fin_hhmm,
            },
        ))

    # ── jornada_profesional: en_colacion ──
    almuerzo_inicio = _parsear_hora_24h(profesional.hora_almuerzo_inicio)
    almuerzo_fin = _parsear_hora_24h(profesional.hora_almuerzo_fin)
    if almuerzo_inicio and almuerzo_fin and _intervalos_se_superponen(
        hora_obj, fin_obj, almuerzo_inicio, almuerzo_fin,
    ):
        inicio_conflicto = max(hora_obj, almuerzo_inicio)
        fin_conflicto = min(fin_obj, almuerzo_fin)
        minutos_afectados = int(
            (
                datetime.combine(date.today(), fin_conflicto)
                - datetime.combine(date.today(), inicio_conflicto)
            ).total_seconds()
            // 60
        )
        conflictos.append(ConflictoSlot(
            codigo="en_colacion",
            categoria="jornada_profesional",
            overridable_con_sobrecupo=True,
            metadata={
                "inicio_colacion": almuerzo_inicio.strftime("%H:%M"),
                "fin_colacion": almuerzo_fin.strftime("%H:%M"),
                "inicio_conflicto": inicio_conflicto.strftime("%H:%M"),
                "fin_conflicto": fin_conflicto.strftime("%H:%M"),
                "minutos_afectados": minutos_afectados,
            },
        ))

    return tuple(conflictos)


def _legacy_desde_conflictos(
    conflictos: tuple[ConflictoSlot, ...],
) -> tuple[bool, str | None, str | None, bool]:
    """
    Reduce la lista estructurada completa de `analizar_conflictos_slot()`
    (ya en el orden de precedencia histórico — ver su docstring) al
    mismo tuple (disponible, motivo, mensaje, overridable_con_sobrecupo)
    que devolvía `_evaluar_slot_en_contexto()` antes de A.4.2: si la
    lista está vacía, el slot está disponible; si no, el primer
    conflicto de la lista es, por construcción, el de mayor precedencia
    legacy presente, y se traduce a su mensaje exacto de siempre vía
    `_mensaje_legacy_para()`.

    Usada tanto por `_evaluar_slot_en_contexto()` (que llama a
    `analizar_conflictos_slot()` y reduce acá) como por
    `evaluar_disponibilidad_slot()` (que reduce la MISMA tupla que ya
    calculó para `ResultadoDisponibilidad.conflictos`, sin una segunda
    llamada al analizador) — una sola reducción, nunca reimplementada
    dos veces.
    """
    if not conflictos:
        return (True, None, None, False)
    ganador = conflictos[0]
    return (
        False,
        ganador.codigo,
        _mensaje_legacy_para(ganador),
        ganador.overridable_con_sobrecupo,
    )


def _evaluar_slot_en_contexto(
    *,
    profesional: Profesional,
    fecha_obj: date,
    hora_obj: time,
    hoy: date,
    dia_cerrado: DiaCerrado | None,
    ocupadas: set[time],
    bloques_grilla: list[time] | None = None,
    ahora: time | None = None,
) -> tuple[bool, str | None, str | None, bool]:
    """
    Wrapper delgado sobre `analizar_conflictos_slot()` — A.4.2. Ya NO
    reimplementa ninguna condición por su cuenta: llama al analizador
    (la ÚNICA fuente de las reglas de un slot con contexto válido) y
    reduce su resultado con `_legacy_desde_conflictos()`. Se conserva
    esta función (en vez de que cada caller llame directo al
    analizador) porque sigue siendo la usada por
    `listar_disponibilidad_rango()`, que no necesita la lista completa
    de conflictos, solo la decisión reducida de siempre.

    Tanto `evaluar_disponibilidad_slot()` (slot puntual, hace sus
    propias queries) como `listar_disponibilidad_rango()` (precarga un
    rango completo en un puñado de queries) terminan apoyándose en el
    mismo análisis — ver "UNA SOLA FUENTE DE VERDAD" en el docstring
    del módulo. Ningún otro lugar debe reimplementar esta precedencia.

    NO incluye el chequeo de `profesional_inactivo`: en el contrato
    original de evaluar_disponibilidad_slot() (A.2A) ese motivo se
    evalúa ANTES que fecha_invalida/hora_invalida (que son un
    problema de parseo de los parámetros crudos, algo que no existe en
    el camino de rango). Por eso cada llamante lo comprueba una sola
    vez, con el mismo criterio, antes de invocar esta función. Este
    wrapper empieza asumiendo que el profesional ya se sabe activo.

    `bloques_grilla` y `ahora` se reenvían tal cual a
    `analizar_conflictos_slot()` — ver su docstring, en particular
    "RELOJ DETERMINISTA": si `ahora` se omite, `hora_pasada` no se
    evalúa (nunca se sustituye por una lectura de reloj interna).

    Devuelve (disponible, motivo, mensaje, overridable_con_sobrecupo).
    """
    conflictos = analizar_conflictos_slot(
        profesional=profesional,
        fecha_obj=fecha_obj,
        hora_obj=hora_obj,
        hoy=hoy,
        dia_cerrado=dia_cerrado,
        ocupadas=ocupadas,
        bloques_grilla=bloques_grilla,
        ahora=ahora,
    )
    return _legacy_desde_conflictos(conflictos)


def evaluar_disponibilidad_slot(
    db: Session,
    *,
    profesional_id: int,
    fecha: str,
    hora: str,
) -> ResultadoDisponibilidad:
    """
    Evalúa si un profesional puede recibir una cita NORMAL (no
    urgente) en `fecha`+`hora`, contra las reglas ESTRUCTURALES del
    slot (ver docstring del módulo — la ventana de 7 días de Estudiante
    NO se evalúa acá, es política de consumidor aplicada por separado).

    No aplica permisos, ownership ni alcance administrativo — eso es
    responsabilidad de cada router llamante (ya lo hacen citas.py y
    agenda.py antes/después de llamar a esta función).

    Resuelve profesional/fecha/hora desde parámetros crudos y hace sus
    propias queries puntuales; la decisión en sí (a partir de fecha/hora
    ya parseadas) se apoya en analizar_conflictos_slot() — la ÚNICA
    fuente de las reglas de un slot con contexto válido, ver "UNA SOLA
    FUENTE DE VERDAD" en el docstring del módulo — y reduce esa MISMA
    tupla (sin una segunda llamada al analizador) tanto para
    disponible/motivo/mensaje/overridable_con_sobrecupo como para
    `ResultadoDisponibilidad.conflictos`.

    RELOJ DETERMINISTA (A.4.2): `datetime.now()` se captura UNA sola
    vez acá abajo; ese mismo snapshot alimenta el análisis estructurado
    y, por ser la misma tupla, también la decisión legacy — no hay
    forma de que ambos discrepen porque el minuto cambió entre dos
    lecturas del reloj, porque ya no hay dos lecturas.
    """
    profesional = (
        db.query(Profesional)
        .filter(Profesional.id == profesional_id)
        .first()
    )
    if not profesional:
        return ResultadoDisponibilidad(
            disponible=False,
            motivo="profesional_no_encontrado",
            mensaje="Profesional no encontrado.",
            overridable_con_sobrecupo=False,
            profesional=None,
        )

    # profesional_inactivo se comprueba acá, ANTES de parsear fecha/hora
    # (orden original de A.2A) — no es parte de _evaluar_slot_en_contexto
    # porque ese núcleo asume que fecha_obj/hora_obj ya se parsearon con
    # éxito, y aquí el profesional puede estar inactivo incluso cuando
    # fecha/hora vienen inválidas. listar_disponibilidad_rango() hace el
    # mismo chequeo, con el mismo criterio, una sola vez por rango (no
    # cambia por slot) — ver su cuerpo.
    if profesional.estado and profesional.estado != "activo":
        return ResultadoDisponibilidad(
            disponible=False,
            motivo="profesional_inactivo",
            mensaje="El profesional no está disponible (licencia, inasistencia u otro bloqueo).",
            overridable_con_sobrecupo=False,
            profesional=profesional,
        )

    fecha_obj = _parsear_fecha(fecha)
    if fecha_obj is None:
        return ResultadoDisponibilidad(
            disponible=False,
            motivo="fecha_invalida",
            mensaje="Fecha inválida.",
            overridable_con_sobrecupo=False,
            profesional=profesional,
        )

    hora_obj = _parsear_hora_flexible(hora)
    if hora_obj is None:
        return ResultadoDisponibilidad(
            disponible=False,
            motivo="hora_invalida",
            mensaje="Hora inválida.",
            overridable_con_sobrecupo=False,
            profesional=profesional,
        )

    hoy = date.today()
    # A.4.2 — un solo snapshot del reloj para toda esta evaluación (ver
    # "RELOJ DETERMINISTA" arriba); nunca se vuelve a leer
    # datetime.now() más abajo, ni en el análisis estructurado ni en la
    # reducción legacy.
    ahora_actual = datetime.now().time()

    # Canonicalización (corrección v6, punto 1): las queries de fecha
    # deben usar la forma canónica ya parseada (fecha_canon), no el
    # string crudo de entrada. _parsear_fecha() acepta variantes no
    # zero-padded ("2026-9-7") vía strptime, pero DiaCerrado.fecha y
    # Cita.fecha se guardan siempre canónicas ("2026-09-07") — si se
    # compara contra el string crudo, un DiaCerrado o una Cita
    # reales pueden no encontrarse simplemente porque el formato de
    # entrada no coincidía byte a byte con lo guardado.
    fecha_canon = fecha_obj.isoformat()
    dia_cerrado = db.query(DiaCerrado).filter(DiaCerrado.fecha == fecha_canon).first()
    ocupadas = _horas_ocupadas_normalizadas(
        db, profesional_id=profesional_id, fecha=fecha_canon,
    )

    # A.4.2 — ÚNICA llamada al analizador para esta evaluación: la
    # misma tupla alimenta tanto la decisión legacy (reducida acá
    # abajo) como ResultadoDisponibilidad.conflictos. Nunca se vuelve a
    # llamar analizar_conflictos_slot() una segunda vez para obtener
    # una u otra por separado.
    conflictos = analizar_conflictos_slot(
        profesional=profesional,
        fecha_obj=fecha_obj,
        hora_obj=hora_obj,
        hoy=hoy,
        dia_cerrado=dia_cerrado,
        ocupadas=ocupadas,
        ahora=ahora_actual,
    )
    disponible, motivo, mensaje, overridable = _legacy_desde_conflictos(conflictos)
    return ResultadoDisponibilidad(
        disponible=disponible,
        motivo=motivo,
        mensaje=mensaje,
        overridable_con_sobrecupo=overridable,
        profesional=profesional,
        conflictos=conflictos,
    )


def listar_horas_disponibles(
    db: Session,
    *,
    profesional_id: int,
    fecha: str,
    ventana_dias: int = VENTANA_AGENDAMIENTO_ESTUDIANTE_DIAS,
) -> tuple[list[str], str | None]:
    """
    Devuelve (horas_disponibles, mensaje) para un profesional en una
    fecha dada. Es la lógica que consume GET /disponibilidad/{id}.

    Mantiene el contrato de negocio previo de ese endpoint: ventana de
    hoy a `ventana_dias` días, solo días hábiles (lunes a viernes), y
    los mismos mensajes exactos para cada caso de "sin horas" — el
    shape de la respuesta HTTP no cambia (lo arma horarios.py).

    La ventana de `ventana_dias` es la política de agendamiento de
    Estudiante descrita en el docstring del módulo — se aplica acá
    explícitamente porque este es el endpoint que consume Estudiante,
    no porque sea una regla estructural del slot.

    Cambio de comportamiento consciente respecto al código anterior a
    A.2: ahora también respeta `profesional.estado` (antes no se
    comprobaba en este endpoint). Si el profesional está inactivo, no
    se ofrecen horas — igual que ya hacía Agenda Admin en el frontend.
    """
    fecha_obj = _parsear_fecha(fecha)
    if fecha_obj is None:
        return [], "Fecha inválida"

    hoy = date.today()
    ventana_maxima = hoy + timedelta(days=ventana_dias)

    if fecha_obj < hoy or fecha_obj > ventana_maxima or fecha_obj.weekday() >= 5:
        return [], "Sin horas disponibles"

    dia_cerrado = db.query(DiaCerrado).filter(DiaCerrado.fecha == fecha).first()
    if dia_cerrado:
        return [], f"El centro permanece cerrado este día. {dia_cerrado.motivo or ''}".strip()

    profesional = (
        db.query(Profesional)
        .filter(Profesional.id == profesional_id)
        .first()
    )
    if not profesional:
        return [], "Profesional no encontrado"

    if profesional.estado and profesional.estado != "activo":
        return [], "Sin horas disponibles por esta semana"

    # Misma grilla real que usa evaluar_disponibilidad_slot() para
    # validar un slot puntual — ver _bloques_grilla_profesional().
    bloques = _bloques_grilla_profesional(profesional)

    if fecha_obj == hoy:
        ahora = datetime.now().time()
        bloques = [b for b in bloques if b > ahora]

    # Comparación normalizada (24h vs "HH:MM AM/PM") — corrección v2
    # del punto 3, misma función que usa evaluar_disponibilidad_slot().
    ocupadas = _horas_ocupadas_normalizadas(
        db, profesional_id=profesional_id, fecha=fecha,
    )

    # Hardening de duración completa (A.2C): superposición de
    # intervalos completos, no solo igualdad de hora de inicio — mismo
    # criterio que _evaluar_slot_en_contexto() (ver su comentario sobre
    # "Ocupación"), para que una hora que se solapa parcialmente con
    # otra cita ya no aparezca como "disponible" en esta lista.
    duracion = _duracion_efectiva(profesional)
    horas_disponibles = []
    for b in bloques:
        fin_b, _ = _fin_intervalo(b, duracion)
        ocupado = any(
            _intervalos_se_superponen(b, fin_b, o, _fin_intervalo(o, duracion)[0])
            for o in ocupadas
        )
        if not ocupado:
            horas_disponibles.append(b.strftime("%H:%M"))

    if not horas_disponibles:
        return [], "Sin horas disponibles por esta semana"

    return horas_disponibles, None


def listar_disponibilidad_rango(
    db: Session,
    *,
    profesional_id: int,
    fecha_inicio: str,
    fecha_fin: str,
) -> dict:
    """
    Disponibilidad real de un profesional para cada slot de la grilla,
    día por día, en un rango [fecha_inicio, fecha_fin] inclusive.

    Contrato de retorno:
        {
          "profesional_id": int,
          "fecha_inicio": "YYYY-MM-DD",
          "fecha_fin": "YYYY-MM-DD",
          "duracion_min": int,
          "dias": [
            {
              "fecha": "YYYY-MM-DD",
              "slots": [
                {
                  "hora": "HH:MM",
                  "disponible": bool,
                  "motivo": str | None,
                  "overridable_con_sobrecupo": bool
                },
                ...
              ]
            },
            ...
          ]
        }

    No expone ningún dato clínico ni de paciente — solo la decisión de
    disponibilidad por slot, igual que evaluar_disponibilidad_slot().
    No aplica permisos, ownership ni alcance administrativo: eso es
    responsabilidad del router llamante (agenda.py), igual que en el
    resto de este módulo.

    Precarga profesional, días cerrados del rango y citas del rango en
    3 queries fijas (no una por día ni una por slot), y evalúa cada
    slot con _evaluar_slot_en_contexto() — el mismo núcleo que usa
    evaluar_disponibilidad_slot() (ver "Corrección v5" en el docstring
    del módulo), así que ambos caminos jamás pueden divergir en la
    precedencia de reglas.

    Levanta ParametrosRangoInvalidosError si fecha_inicio/fecha_fin
    tienen formato inválido, si fecha_fin es anterior a fecha_inicio, o
    si el rango supera MAX_DIAS_RANGO_DISPONIBILIDAD días. Levanta
    ProfesionalNoEncontradoError si profesional_id no existe. El router
    traduce ambas a HTTP 400/404 respectivamente.
    """
    fecha_inicio_obj = _parsear_fecha(fecha_inicio)
    if fecha_inicio_obj is None:
        raise ParametrosRangoInvalidosError(
            "fecha_inicio_invalida", "fecha_inicio inválida.",
        )

    fecha_fin_obj = _parsear_fecha(fecha_fin)
    if fecha_fin_obj is None:
        raise ParametrosRangoInvalidosError(
            "fecha_fin_invalida", "fecha_fin inválida.",
        )

    if fecha_fin_obj < fecha_inicio_obj:
        raise ParametrosRangoInvalidosError(
            "rango_invertido",
            "fecha_fin no puede ser anterior a fecha_inicio.",
        )

    dias_totales = (fecha_fin_obj - fecha_inicio_obj).days + 1
    if dias_totales > MAX_DIAS_RANGO_DISPONIBILIDAD:
        raise ParametrosRangoInvalidosError(
            "rango_excede_maximo",
            f"El rango no puede superar {MAX_DIAS_RANGO_DISPONIBILIDAD} días.",
        )

    profesional = (
        db.query(Profesional)
        .filter(Profesional.id == profesional_id)
        .first()
    )
    if not profesional:
        raise ProfesionalNoEncontradoError(profesional_id)

    # profesional_inactivo (mismo criterio y mismo motivo/mensaje que
    # evaluar_disponibilidad_slot() — ver su docstring): no es parte de
    # _evaluar_slot_en_contexto() porque no depende de fecha/hora/día
    # cerrado/ocupación, es un atributo del profesional que no cambia
    # slot a slot. Se resuelve UNA sola vez acá, no en el núcleo, para
    # conservar exactamente la precedencia de A.2A (profesional_inactivo
    # antes que cualquier regla de fecha/hora/grilla).
    profesional_inactivo = bool(
        profesional.estado and profesional.estado != "activo",
    )

    # Canonicalización (corrección v6, punto 2): igual que en
    # evaluar_disponibilidad_slot(), toda comparación de fecha contra
    # BD debe usar la forma canónica ya parseada, nunca el string
    # crudo de entrada — acá es aún más importante que en el slot
    # puntual, porque estas no son comparaciones de igualdad sino de
    # rango (>=/<=) sobre una columna de texto: una entrada no
    # zero-padded no solo puede fallar una igualdad, puede ordenar mal
    # lexicográficamente y dejar fuera (o de más) fechas del rango.
    fecha_inicio_canon = fecha_inicio_obj.isoformat()
    fecha_fin_canon = fecha_fin_obj.isoformat()

    # Un solo query para todos los días cerrados del rango, indexado
    # por fecha para lookup O(1) dentro del loop de días.
    dias_cerrados_por_fecha: dict[str, DiaCerrado] = {
        dia_cerrado.fecha: dia_cerrado
        for dia_cerrado in (
            db.query(DiaCerrado)
            .filter(
                DiaCerrado.fecha >= fecha_inicio_canon,
                DiaCerrado.fecha <= fecha_fin_canon,
            )
            .all()
        )
    }

    # Un solo query para todas las citas activas del rango, agrupadas
    # por fecha y ya normalizadas a `time` (misma normalización 24h /
    # "HH:MM AM/PM" que usa el resto del módulo — corrección v2, punto 3).
    ocupadas_por_fecha: dict[str, set[time]] = {}
    filas_citas = (
        db.query(Cita.fecha, Cita.hora)
        .filter(
            Cita.profesional_id == profesional_id,
            Cita.fecha >= fecha_inicio_canon,
            Cita.fecha <= fecha_fin_canon,
            Cita.estado.in_(ESTADOS_CITA_QUE_OCUPAN_SLOT),
        )
        .all()
    )
    for fecha_str, hora_str in filas_citas:
        hora_obj = _parsear_hora_flexible(hora_str)
        if hora_obj is not None:
            ocupadas_por_fecha.setdefault(fecha_str, set()).add(hora_obj)

    # Duración efectiva única (ver _duracion_efectiva): el mismo número
    # que se usa para construir la grilla es el que se reporta en
    # "duracion_min" — nunca dos valores distintos para la misma grilla.
    duracion = _duracion_efectiva(profesional)
    bloques_grilla = generar_bloques_jornada(duracion)

    hoy = date.today()
    # A.4.2 — mismo patrón que `hoy`: un solo snapshot del reloj para
    # todo el rango (no uno por slot ni por día), reutilizado en cada
    # llamada a _evaluar_slot_en_contexto() de abajo — ver "RELOJ
    # DETERMINISTA" en el docstring del módulo.
    ahora_actual = datetime.now().time()
    dias_resultado: list[dict] = []
    fecha_actual = fecha_inicio_obj
    while fecha_actual <= fecha_fin_obj:
        fecha_str = fecha_actual.isoformat()
        dia_cerrado = dias_cerrados_por_fecha.get(fecha_str)
        ocupadas = ocupadas_por_fecha.get(fecha_str, set())

        slots = []
        for hora_obj in bloques_grilla:
            if profesional_inactivo:
                # Mismo motivo/mensaje que evaluar_disponibilidad_slot()
                # para este mismo caso — ver docstring de esta función.
                disponible, motivo, mensaje, overridable = (
                    False,
                    "profesional_inactivo",
                    "El profesional no está disponible (licencia, inasistencia u otro bloqueo).",
                    False,
                )
            else:
                disponible, motivo, mensaje, overridable = _evaluar_slot_en_contexto(
                    profesional=profesional,
                    fecha_obj=fecha_actual,
                    hora_obj=hora_obj,
                    hoy=hoy,
                    dia_cerrado=dia_cerrado,
                    ocupadas=ocupadas,
                    ahora=ahora_actual,
                    bloques_grilla=bloques_grilla,
                )
            slots.append({
                "hora": hora_obj.strftime("%H:%M"),
                "disponible": disponible,
                "motivo": motivo,
                "overridable_con_sobrecupo": overridable,
            })

        dias_resultado.append({"fecha": fecha_str, "slots": slots})
        fecha_actual += timedelta(days=1)

    return {
        "profesional_id": profesional_id,
        # Mismas variables canónicas que ya se usaron para las queries
        # de DiaCerrado/Cita más arriba (fecha_inicio_canon/
        # fecha_fin_canon) — una sola fuente para "cuál es la fecha
        # canónica de este rango", nunca el string crudo de entrada.
        "fecha_inicio": fecha_inicio_canon,
        "fecha_fin": fecha_fin_canon,
        "duracion_min": duracion,
        "dias": dias_resultado,
    }
