from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from datetime import datetime
import io
import os

from app.database import get_db
from app.models.cita import Cita
from app.models.cita_sobrecupo import CitaSobrecupo, CitaSobrecupoConflicto
from app.models.profesional import Profesional
from app.models.usuario import Usuario
from app.models.historial_paciente import HistorialPaciente
from app.models.notificacion import Notificacion
from app.schemas import CitaCreate
from app.auditoria import registrar_evento_auditoria
from app.auth_dependencies import get_current_user, verificar_acceso
from app.rbac.permissions import Permission
from app.rbac.admin_authorization import (
    tiene_permiso_efectivo,
    obtener_alcance_administrativo_efectivo,
    especialidad_permitida_por_alcance,
)
from app.services.agenda_disponibilidad_service import (
    evaluar_disponibilidad_slot,
    excede_ventana_agendamiento_estudiante,
    adquirir_lock_agenda_profesional_fecha,
    canonicalizar_fecha_valida,
    SlotInvalidoError,
)
from app.services.cita_origen_service import resolver_origen_cita

router = APIRouter(tags=["citas"])


def _cita_a_datetime(cita: Cita) -> datetime:
    """Combina fecha (YYYY-MM-DD) y hora (HH:MM AM/PM) de la cita en un datetime."""
    return datetime.strptime(f"{cita.fecha} {cita.hora}", "%Y-%m-%d %I:%M %p")


def _puede_gestionar_agenda(
    current_user: dict,
    db: Session,
) -> bool:
    """
    Resuelve agenda.gestionar contra el estado actual de la BD.

    ADMIN depende de su configuracion administrativa efectiva.
    SUPERADMIN conserva sus permisos explicitos de rol a traves
    del mismo resolver.
    """
    return tiene_permiso_efectivo(
        db,
        current_user,
        Permission.AGENDA_GESTIONAR,
    )


def _verificar_propietario_o_agenda(
    current_user: dict,
    estudiante_id: int,
    db: Session,
) -> bool:
    """
    Permite:
      - estudiante operando sobre su propio Usuario.id, o
      - usuario con agenda.gestionar efectivo.

    Devuelve True cuando la operacion usa capacidad administrativa.
    """
    puede_gestionar = _puede_gestionar_agenda(
        current_user,
        db,
    )

    if puede_gestionar:
        return True

    verificar_acceso(
        current_user,
        id_esperado=estudiante_id,
        roles_permitidos=["estudiante"],
    )

    return False



def _verificar_alcance_profesional_agenda(
    db: Session,
    current_user: dict,
    profesional: Profesional | None,
) -> None:
    """
    Comprueba el alcance administrativo sobre el profesional objetivo.

    Un profesional inexistente o fuera del alcance se trata como 404
    para no revelar recursos pertenecientes a otro alcance.
    """
    alcance = obtener_alcance_administrativo_efectivo(
        db,
        current_user,
    )

    if (
        alcance is None
        or profesional is None
        or not especialidad_permitida_por_alcance(
            alcance,
            profesional.especialidad,
        )
    ):
        raise HTTPException(
            status_code=404,
            detail="Profesional no encontrado",
        )


def _verificar_acceso_a_cita(
    cita: Cita,
    current_user: dict,
    db: Session,
) -> None:
    """
    Acceso privado a una cita: estudiante dueño o profesional asignado.

    ADMIN y SUPERADMIN no reciben bypass clínico por este helper.
    agenda.gestionar se evalúa solamente en operaciones administrativas
    concretas, como crear o cancelar una cita.
    """
    rol = current_user["rol"]
    uid = current_user["id"]

    if rol == "estudiante" and cita.estudiante_id == uid:
        return

    if rol == "profesional":
        prof = (
            db.query(Profesional)
            .filter(Profesional.id == cita.profesional_id)
            .first()
        )
        if prof and prof.usuario_id == uid:
            return

    raise HTTPException(
        status_code=403,
        detail="No tienes permiso para acceder a esta cita.",
    )


@router.get("/citas/estudiante/{estudiante_id}")
def get_citas_estudiante(
    estudiante_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    _verificar_propietario_o_agenda(current_user, estudiante_id)
    citas = db.query(Cita).filter(
        Cita.estudiante_id == estudiante_id,
        Cita.estado == "pendiente"
    ).all()
    result = []
    for c in citas:
        prof = db.query(Profesional).filter(Profesional.id == c.profesional_id).first()
        result.append({
            "id":           c.id,
            "iniciales":    prof.iniciales    if prof else "??",
            "especialidad": prof.especialidad if prof else "",
            "profesional":  prof.nombre       if prof else "",
            "fecha":        c.fecha,
            "hora":         c.hora,
            "urgente":      False,
            "aviso":        "Cancelación hasta 5 horas antes",
            "estado":       c.estado
        })
    return result


@router.get("/historial/estudiante/{estudiante_id}")
def get_historial(
    estudiante_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    verificar_acceso(
        current_user,
        id_esperado=estudiante_id,
        roles_permitidos=["estudiante"],
    )
    # Solo estados ya resueltos: completada, cancelada, inasistencia
    citas = db.query(Cita).filter(
        Cita.estudiante_id == estudiante_id,
        Cita.estado.in_(["completada", "cancelada", "inasistencia"])
    ).all()
    result = []
    for c in citas:
        prof = db.query(Profesional).filter(Profesional.id == c.profesional_id).first()
        result.append({
            "id":                     c.id,
            "fechaRaw":               c.fecha,
            "fecha":                  c.fecha,
            "hora":                   c.hora,
            "profesional":            prof.nombre       if prof else "",
            "iniciales":              prof.iniciales    if prof else "??",
            "especialidad":           prof.especialidad if prof else "",
            "estado":                 c.estado,
            "tiene_pdf":              c.estado == "completada",
            "motivo_consulta":        c.observaciones,
            "medicamento":            c.medicamento            if c.estado == "completada" else None,
            "observaciones_atencion": c.observaciones_atencion if c.estado == "completada" else None,
            "motivo_cancelacion":     c.motivo_cancelacion,
        })
    return result


@router.post("/citas")
def crear_cita(
    cita: CitaCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    # Un estudiante solo puede agendar para sí mismo; un admin puede agendar
    # a nombre de cualquier estudiante (ej. citas urgentes desde recepción).
    puede_gestionar_agenda = _verificar_propietario_o_agenda(
        current_user,
        cita.estudiante_id,
        db,
    )

    # A.3 (v3) — canonicalizar la fecha UNA sola vez, antes de CUALQUIER
    # query que dependa de ella, y reutilizar exactamente ese mismo
    # valor en todo el resto del flujo (DiaCerrado, ventana de
    # agendamiento, lock, evaluar_disponibilidad_slot, INSERT de
    # Cita.fecha) — ver canonicalizar_fecha_valida() en
    # agenda_disponibilidad_service.py. Una fecha no interpretable
    # nunca se mutasilenciosamente ni se deja pasar: se rechaza acá
    # mismo con 400, antes de la consulta de DiaCerrado que existía
    # antes de esta corrección y que comparaba contra el string crudo.
    try:
        fecha_canon = canonicalizar_fecha_valida(cita.fecha)
    except SlotInvalidoError as exc:
        raise HTTPException(status_code=400, detail=exc.mensaje)

    # Un día marcado como "cerrado" (centro completo sin atención) bloquea
    # el agendamiento sin excepción, incluso para el admin — ningún
    # profesional trabaja ese día, así que no existe sobrecupo posible ahí.
    from app.models.dia_cerrado import DiaCerrado
    if db.query(DiaCerrado).filter(DiaCerrado.fecha == fecha_canon).first():
        raise HTTPException(status_code=400, detail="El centro permanece cerrado ese día. Elige otra fecha.")

    prof = (
        db.query(Profesional)
        .filter(
            Profesional.id == cita.profesional_id
        )
        .first()
    )

    if puede_gestionar_agenda:
        _verificar_alcance_profesional_agenda(
            db,
            current_user,
            prof,
        )

    # El "paciente" de una cita SIEMPRE debe ser una cuenta con rol estudiante.
    # Sin este chequeo, un admin podría —por error, ej. escribiendo mal un
    # RUT o reutilizando un id— crear una cita donde el "estudiante" en
    # realidad sea la cuenta de un profesional o del propio admin.
    paciente = db.query(Usuario).filter(Usuario.id == cita.estudiante_id).first()
    if not paciente or paciente.rol != "estudiante":
        raise HTTPException(
            status_code=400,
            detail="El paciente seleccionado no corresponde a una cuenta de estudiante."
        )

    # A.2 (corrección v2, punto 2) — la ventana de 7 días es una
    # política de agendamiento propia de Estudiante, no una regla
    # estructural del slot (ver docstring de agenda_disponibilidad_service).
    # Se re-aplica acá explícitamente SOLO cuando quien agenda es el
    # propio estudiante (sin agenda.gestionar): así una llamada directa
    # a POST /citas no le permite saltarse una restricción que
    # GET /disponibilidad ya le oculta en la UI. No se aplica cuando
    # quien agenda tiene capacidad administrativa, para no romper la
    # futura Agenda Admin, que necesita poder navegar/agendar semanas
    # posteriores a esta ventana.
    if not puede_gestionar_agenda and excede_ventana_agendamiento_estudiante(fecha_canon):
        raise HTTPException(
            status_code=400,
            detail="Esa fecha está fuera del rango de agendamiento disponible.",
        )

    # A.2 — antes de esto, fecha/hora no se validaban contra jornada,
    # colación, grilla real, DiaCerrado ni ocupación real: bastaba con
    # que el cliente enviara cualquier valor. evaluar_disponibilidad_slot()
    # es la misma fuente que usa GET /disponibilidad/{id} (Estudiante)
    # y que debería usar la grilla de Agenda Admin.
    #
    # sobrecupo=True (solo posible si puede_gestionar_agenda; ver
    # arriba) puede superar únicamente los motivos marcados como
    # overridable_con_sobrecupo — fuera de jornada o en colación.
    # Nunca supera centro cerrado, fin de semana, fecha/hora pasada,
    # profesional inactivo/inexistente, un horario fuera de grilla ni
    # un slot ya ocupado por otra cita: eso replica la semántica que
    # ya tenía el frontend (clickBloque() solo ofrece sobrecupo para
    # 'fuera-horario' y 'colacion'). El diseño definitivo de
    # autorización/auditoría de sobrecupo queda para A.4.
    #
    # A.3 — concurrencia/doble-reserva: el lock DEBE adquirirse ANTES
    # de esta re-evaluación, no después. evaluar_disponibilidad_slot()
    # aquí NO es una simple validación — es la re-evaluación dentro de
    # la sección crítica que cierra la carrera SELECT→INSERT: dos
    # peticiones concurrentes para el mismo profesional/fecha se
    # serializan en esta línea (la segunda espera hasta que la primera
    # haga commit/rollback), y para cuando la segunda continúa, ya ve
    # la cita que la primera insertó. Evaluar antes del lock y confiar
    # en que el resultado siga vigente al insertar es exactamente la
    # carrera que esto existe para cerrar — ver
    # adquirir_lock_agenda_profesional_fecha().
    adquirir_lock_agenda_profesional_fecha(
        db,
        profesional_id=cita.profesional_id,
        fecha=fecha_canon,
    )
    resultado_disponibilidad = evaluar_disponibilidad_slot(
        db,
        profesional_id=cita.profesional_id,
        fecha=fecha_canon,
        hora=cita.hora,
    )
    # A.4.1 — motivo estructurado REALMENTE superado, si lo hay. Solo se
    # llena cuando evaluar_disponibilidad_slot() encontró un motivo
    # overridable y sobrecupo lo autorizó — nunca se inventa un
    # "conflicto" para el caso (posible, si alguien llama a la API
    # directamente con sobrecupo=True sobre un slot que ya estaba
    # libre) en que no hubo nada real que superar. Ver
    # app.models.cita_sobrecupo.CitaSobrecupo.
    motivo_conflicto_superado: str | None = None
    if not resultado_disponibilidad.disponible:
        sobrecupo_autoriza = (
            puede_gestionar_agenda
            and bool(cita.sobrecupo)
            and resultado_disponibilidad.overridable_con_sobrecupo
        )
        if not sobrecupo_autoriza:
            # A.3 — "slot_ocupado" tras la re-evaluación DENTRO del
            # lock es, por definición, perder la carrera (alguien más
            # ocupó ese intervalo, ya sea justo ahora o antes de que
            # esta petición llegara): 409 Conflict, no 400. El resto de
            # los motivos (fuera de jornada, en colación, fuera de
            # grilla, día cerrado, etc.) son problemas de validez del
            # slot en sí, no de concurrencia, y mantienen 400 — no se
            # convierte automáticamente en sobrecupo en ningún caso.
            status_code = (
                409
                if resultado_disponibilidad.motivo == "slot_ocupado"
                else 400
            )
            raise HTTPException(
                status_code=status_code,
                detail=resultado_disponibilidad.mensaje or "Esa hora no está disponible.",
            )
        motivo_conflicto_superado = resultado_disponibilidad.motivo

    if prof:
        # Una cita "pendiente" solo debe bloquear un nuevo agendamiento si
        # todavía está por venir. Si quedó "pendiente" con fecha ya pasada
        # (el profesional nunca la cerró como completada/inasistencia), no
        # debe impedir que el estudiante agende una hora nueva.
        candidatas = (
            db.query(Cita)
            .join(Profesional, Cita.profesional_id == Profesional.id)
            .filter(
                Cita.estudiante_id == cita.estudiante_id,
                Profesional.especialidad == prof.especialidad,
                Cita.estado == "pendiente"
            ).all()
        )
        ahora = datetime.now()
        duplicada = None
        for c in candidatas:
            try:
                fh = _cita_a_datetime(c)
            except ValueError:
                fh = None
            if fh is None or fh >= ahora:
                duplicada = c
                break
        if duplicada:
            raise HTTPException(status_code=400,
                detail="Ya tienes una cita pendiente en esta especialidad")

    # A.4.1 — origen de la cita: SIEMPRE el actor autenticado de esta
    # request (current_user), nunca un valor del body. Ver
    # app.services.cita_origen_service — misma función usada por
    # POST /admin/citas/urgente, para que ambos canales registren el
    # origen con exactamente el mismo criterio.
    origen = resolver_origen_cita(db, current_user)

    nueva = Cita(
        estudiante_id  = cita.estudiante_id,
        profesional_id = cita.profesional_id,
        # A.3 (v3) — se guarda fecha_canon, NUNCA cita.fecha crudo: es
        # la misma forma canónica ya usada arriba para DiaCerrado, el
        # lock y evaluar_disponibilidad_slot (ver
        # canonicalizar_fecha_valida()). Guardar el string crudo aquí
        # era precisamente el hueco que A.3 (v2) dejaba abierto.
        fecha          = fecha_canon,
        hora           = cita.hora,
        observaciones  = cita.observaciones,
        # Estas marcas son administrativas: el estudiante no puede
        # elevar prioridad ni crear sobrecupo manipulando el body.
        urgente        = (
            bool(cita.urgente)
            if puede_gestionar_agenda
            else False
        ),
        # A.4.1 v2 — "sobrecupo efectivo": Cita.sobrecupo representa la
        # intención del cliente autorizada Y CONFIRMADA por la
        # re-evaluación DENTRO del lock A.3, no la intención cruda que
        # llegó en el body. Si el cliente pidió sobrecupo=True pero,
        # al re-evaluar dentro del lock, el slot resultó realmente
        # disponible (p. ej. la cita que antes lo bloqueaba fue
        # cancelada mientras tanto), NO hay ningún conflicto que
        # "sobrecupo" esté superando — persistir True ahí crearía dos
        # fuentes de verdad contradictorias (Cita.sobrecupo=True sin
        # ningún CitaSobrecupo/conflicto/auditoría que lo respalde,
        # como pasaba en A.4.1 v1). motivo_conflicto_superado ya es
        # None en ese caso (ver arriba), así que basta con usarlo como
        # única fuente de verdad — nunca se rechaza la cita solo
        # porque el conflicto desapareció; eso es una mejora legítima
        # de la concurrencia, no un error.
        sobrecupo      = motivo_conflicto_superado is not None,
        estado         = "pendiente",
        creado_por_usuario_id = origen.creado_por_usuario_id,
        creado_por_rol        = origen.creado_por_rol,
        creado_por_perfil     = origen.creado_por_perfil,
    )
    db.add(nueva)
    try:
        # A.4.1 — flush (no commit todavía): necesitamos nueva.id para
        # poder crear, en la MISMA transacción, el detalle de
        # sobrecupo y su evento de auditoría cuando corresponda (ver
        # abajo). Mismo patrón que ya usa POST /admin/citas/urgente
        # (admin.py) — Cita + CitaSobrecupo + CitaSobrecupoConflicto +
        # Auditoria se confirman o se deshacen juntos con un único
        # commit al final.
        db.flush()
    except IntegrityError:
        # A.3 — esta red de seguridad NO es (nunca lo fue) la
        # protección real contra doble reserva: Cita no tiene, ni tuvo
        # nunca, ningún UniqueConstraint/Index único sobre
        # (profesional_id, fecha, hora) que este INSERT pudiera violar
        # (confirmado en el diagnóstico de A.3 — antes este comentario
        # afirmaba lo contrario, era falso). La protección real es
        # adquirir_lock_agenda_profesional_fecha() + la re-evaluación
        # de evaluar_disponibilidad_slot() DENTRO de ese lock, arriba.
        # Este except se conserva solo como red de seguridad genérica
        # ante cualquier violación de integridad real (p. ej. FK), no
        # como mecanismo de concurrencia.
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Esa hora acaba de ser reservada por otra persona. Por favor elige otra."
        )

    # A.4.1 — auditoría de sobrecupo (hallazgo confirmado en el
    # diagnóstico A.4: POST /citas hoy NO auditaba la creación de
    # sobrecupo, a diferencia de POST /admin/citas/urgente). Se
    # registra si y solo si hubo sobrecupo EFECTIVO
    # (motivo_conflicto_superado no es None, ver arriba) — nunca
    # cuando cita.sobrecupo=True llegó marcado sobre un slot que, tras
    # la re-evaluación dentro del lock, resultó realmente libre.
    if motivo_conflicto_superado is not None:
        detalle_sobrecupo = CitaSobrecupo(
            cita_id=nueva.id,
            # sobrecupo_motivo todavía no lo envía el frontend actual
            # (ver CitaCreate.sobrecupo_motivo) — queda NULL hasta que
            # A.4.7 actualice el modal de Angular. No se exige acá
            # para no romper el flujo existente.
            motivo=cita.sobrecupo_motivo,
            estado_revision=None,
        )
        db.add(detalle_sobrecupo)
        db.flush()
        db.add(CitaSobrecupoConflicto(
            cita_sobrecupo_id=detalle_sobrecupo.id,
            codigo=motivo_conflicto_superado,
        ))
        registrar_evento_auditoria(
            db, current_user, "Creó cita con sobrecupo",
            entidad="cita", entidad_id=nueva.id,
            detalle=(
                f"Estudiante id {cita.estudiante_id} — Profesional id "
                f"{cita.profesional_id} ({nueva.fecha} {nueva.hora}). "
                f"Conflicto superado: {motivo_conflicto_superado}. "
                f"Motivo informado: {cita.sobrecupo_motivo or '(no informado por el cliente)'}. "
                f"Perfil administrativo del actor: {origen.creado_por_perfil or '-'}."
            ),
        )

    db.commit()
    db.refresh(nueva)

    # Si es la primera vez que este estudiante agenda con este profesional
    # (todavía no existe su ficha de antecedentes), se le notifica para que
    # complete el cuestionario de primera atención antes de la cita.
    if prof:
        ya_tiene_ficha = db.query(HistorialPaciente).filter(
            HistorialPaciente.profesional_id == prof.id,
            HistorialPaciente.estudiante_id  == cita.estudiante_id
        ).first()
        if not ya_tiene_ficha:
            db.add(Notificacion(
                usuario_id=cita.estudiante_id,
                mensaje=f"Antes de tu cita con {prof.nombre}, completa tu cuestionario de antecedentes.",
                tipo="cuestionario_pendiente"
            ))
            db.commit()

    return {
        "id":           nueva.id,
        "iniciales":    prof.iniciales    if prof else "??",
        "especialidad": prof.especialidad if prof else "",
        "profesional":  prof.nombre       if prof else "",
        "fecha":        nueva.fecha,
        "hora":         nueva.hora,
        "urgente":      nueva.urgente or False,
        "aviso":        "Cancelación hasta 5 horas antes",
        "estado":       nueva.estado
    }


@router.delete("/citas/{cita_id}")
def cancelar_cita(
    cita_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cita = db.query(Cita).filter(Cita.id == cita_id).first()
    if not cita:
        raise HTTPException(status_code=404, detail="Cita no encontrada")

    puede_gestionar_agenda = _puede_gestionar_agenda(
        current_user,
        db,
    )

    if puede_gestionar_agenda:
        prof = (
            db.query(Profesional)
            .filter(
                Profesional.id == cita.profesional_id
            )
            .first()
        )

        _verificar_alcance_profesional_agenda(
            db,
            current_user,
            prof,
        )
    else:
        _verificar_acceso_a_cita(
            cita,
            current_user,
            db,
        )

    if cita.estado != "pendiente":
        raise HTTPException(status_code=400, detail="Esta cita no se puede cancelar")

    try:
        fecha_hora_cita = _cita_a_datetime(cita)
    except ValueError:
        fecha_hora_cita = None

    # Un admin puede cancelar sin la restricción de las 5 horas (ej. por
    # ausencia del profesional o motivos operativos).
    if fecha_hora_cita and not puede_gestionar_agenda:
        horas_restantes = (fecha_hora_cita - datetime.now()).total_seconds() / 3600
        # La restricción de "mínimo 5 horas antes" solo tiene sentido si la
        # cita todavía está por venir. Si ya pasó la fecha/hora (horas_restantes
        # negativo) y el profesional nunca la cerró, el estudiante debe poder
        # cancelarla igual — de lo contrario queda atrapado sin poder ni
        # cancelar ni agendar una hora nueva en esa especialidad.
        if 0 <= horas_restantes < 5:
            raise HTTPException(
                status_code=400,
                detail="No se puede cancelar: faltan menos de 5 horas para la cita"
            )

    cita.estado = "cancelada"
    db.commit()
    return {"message": "Cita cancelada correctamente"}


@router.get("/citas/{cita_id}/pdf")
def descargar_pdf_cita(
    cita_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    cita = db.query(Cita).filter(Cita.id == cita_id).first()
    if not cita:
        raise HTTPException(status_code=404, detail="Cita no encontrada")

    _verificar_acceso_a_cita(cita, current_user, db)

    if cita.estado != "completada":
        raise HTTPException(status_code=400, detail="Solo se puede descargar el resumen de una cita completada")

    prof = db.query(Profesional).filter(Profesional.id == cita.profesional_id).first()

    from fpdf import FPDF

    LOGO_PATH = os.path.join(os.path.dirname(__file__), "..", "static", "temporal.jpg")

    pdf = FPDF()
    pdf.add_page()

    # ── Encabezado con logo institucional ──
    if os.path.exists(LOGO_PATH):
        pdf.image(LOGO_PATH, x=10, y=8, w=20)
        pdf.set_xy(35, 10)
    else:
        pdf.set_xy(10, 10)

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 8, "SESAES - Resumen de Atención", ln=True)
    pdf.set_x(35 if os.path.exists(LOGO_PATH) else 10)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 6, "Universidad Tecnológica Metropolitana - Salud Estudiantil", ln=True)

    pdf.ln(10)
    pdf.set_draw_color(200, 200, 200)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    # ── Datos de la atención (etiqueta en negrita + valor normal) ──
    def campo(pdf, etiqueta, valor):
        pdf.set_font("Helvetica", "B", 12)
        ancho_etiqueta = pdf.get_string_width(etiqueta) + 2
        pdf.cell(ancho_etiqueta, 8, etiqueta)
        pdf.set_font("Helvetica", "", 12)
        pdf.cell(0, 8, valor, ln=True)

    campo(pdf, "Profesional:", f" {prof.nombre if prof else '-'}")
    campo(pdf, "Especialidad:", f" {prof.especialidad if prof else '-'}")
    campo(pdf, "Fecha:", f" {cita.fecha}")
    campo(pdf, "Hora:", f" {cita.hora}")
    campo(pdf, "Motivo de consulta:", f" {cita.observaciones or 'No especificado'}")

    # ── Registro de atención (NO es una receta; ver SA-11.3B) ──
    pdf.ln(6)
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "Registro de atención", ln=True)
    pdf.set_draw_color(230, 230, 230)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(4)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, "Medicamento suministrado:", ln=True)
    pdf.set_font("Helvetica", "", 11)
    pdf.multi_cell(0, 7, cita.medicamento or "No se suministró medicamento.")

    pdf.ln(2)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, "Observaciones e indicaciones:", ln=True)
    pdf.set_font("Helvetica", "", 11)
    pdf.multi_cell(0, 7, cita.observaciones_atencion or "Sin observaciones adicionales.")

    # ── Firma / sello del profesional ──
    pdf.ln(20)
    y_firma = pdf.get_y()
    pdf.set_draw_color(0, 0, 0)
    pdf.line(120, y_firma, 195, y_firma)
    pdf.set_xy(120, y_firma + 2)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(75, 6, prof.nombre if prof else "-", ln=True, align="C")
    pdf.set_x(120)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(75, 5, prof.especialidad if prof else "-", ln=True, align="C")
    pdf.set_x(120)
    pdf.cell(75, 5, "SESAES - UTEM", ln=True, align="C")

    buffer = io.BytesIO(pdf.output())
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=resumen_cita_{cita_id}.pdf"}
    )
