import re
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from datetime import datetime, date, timedelta
import io

from app.database import get_db
from app.security import hash_password
from app.models.cita import Cita
from app.models.profesional import Profesional
from app.models.usuario import Usuario
from app.models.notificacion import Notificacion
from app.models.configuracion import ConfiguracionSistema
from app.models.auditoria import Auditoria
from app.auditoria import registrar_evento_auditoria
from app.models.historial_estado_profesional import HistorialEstadoProfesional
from app.models.correo_log import CorreoLog
from app.models.dia_cerrado import DiaCerrado
from app.routers.correos import simular_envio_correo
from app.schemas import (
    ProfesionalCreate, ProfesionalUpdate, ProfesionalOut,
    ConfiguracionOut, ConfiguracionUpdate, CitaCreate,
    color_identificador_es_valido
)
from app.auth_dependencies import get_current_user
from app.rbac.dependencies import (
    require_permission,
    require_effective_permission,
)
from app.rbac.permissions import Permission
from app.rbac.admin_authorization import (
    obtener_alcance_administrativo_efectivo,
    especialidad_permitida_por_alcance,
)
from app.services.agenda_disponibilidad_service import (
    adquirir_lock_agenda_profesional_fecha,
    hay_solapamiento_con_cita_activa,
    SlotInvalidoError,
    canonicalizar_fecha_valida,
)
from app.services.cita_origen_service import resolver_origen_cita



router = APIRouter(prefix="/admin", tags=["administrador"])

RUTS_EXCLUIDOS_CGR = {"16.458.880-7", "19.741.131-7"}


def _ids_profesionales_en_alcance(
    db: Session,
    alcance,
) -> list[int]:
    """
    Devuelve los IDs de profesionales visibles para un alcance limitado.

    Para alcance institucional no se utiliza este helper: el endpoint
    conserva la consulta global sin introducir un IN innecesario.

    La comparaci?n usa la misma normalizaci?n SA-8/SA-9 del resto
    de la autorizaci?n administrativa.
    """
    if alcance is None or alcance.institucional:
        return []

    return [
        profesional.id
        for profesional in db.query(Profesional).all()
        if especialidad_permitida_por_alcance(
            alcance,
            profesional.especialidad,
        )
    ]


def validar_rut(rut: str) -> bool:
    if not rut: return False
    rut_limpio = rut.replace(".", "").replace("-", "")
    if len(rut_limpio) < 2: return False
    cuerpo = rut_limpio[:-1]
    dv = rut_limpio[-1].upper()
    if not cuerpo.isdigit(): return False
    suma = 0; multi = 2
    for c in reversed(cuerpo):
        suma += int(c) * multi
        multi = 2 if multi == 7 else multi + 1
    dv_esperado = 11 - (suma % 11)
    dv_calc = "0" if dv_esperado == 11 else "K" if dv_esperado == 10 else str(dv_esperado)
    return dv == dv_calc


# SA-2: el helper local registrar_auditoria(...) se eliminó. Todo este
# router usa ahora app.auditoria.registrar_evento_auditoria, que deriva el
# actor (usuario_id, actor_rol) EXCLUSIVAMENTE de current_user y nunca
# hace commit/rollback por sí mismo — ver docstring de app/auditoria.py.


# ══════════════════════════════════════
# ESTADÍSTICAS
# ══════════════════════════════════════

@router.get("/estadisticas")
def get_estadisticas(db: Session = Depends(get_db), current_user: dict = Depends(require_effective_permission(Permission.REPORTES_VER))):
    hoy = date.today().isoformat()

    alcance = obtener_alcance_administrativo_efectivo(
        db,
        current_user,
    )

    if alcance is None:
        raise HTTPException(
            status_code=403,
            detail="No tienes acceso al alcance solicitado.",
        )

    ids_profesionales = None

    if not alcance.institucional:
        ids_profesionales = _ids_profesionales_en_alcance(
            db,
            alcance,
        )

        if not ids_profesionales:
            return {
                "reservas_hoy": 0,
                "profesionales_activos": 0,
                "horas_disponibles": 0,
                "urgentes": 0,
            }

    reservas_query = db.query(Cita).filter(
        Cita.fecha == hoy,
        Cita.estado == "pendiente",
    )

    profesionales_query = db.query(Profesional).filter(
        Profesional.estado == "activo"
    )

    citas_hoy_query = db.query(Cita).filter(
        Cita.fecha == hoy,
        Cita.estado.in_(["pendiente", "completada"]),
    )

    urgentes_query = db.query(Cita).filter(
        Cita.urgente == True,  # noqa: E712
        Cita.estado == "pendiente",
    )

    if ids_profesionales is not None:
        reservas_query = reservas_query.filter(
            Cita.profesional_id.in_(ids_profesionales)
        )

        profesionales_query = profesionales_query.filter(
            Profesional.id.in_(ids_profesionales)
        )

        citas_hoy_query = citas_hoy_query.filter(
            Cita.profesional_id.in_(ids_profesionales)
        )

        urgentes_query = urgentes_query.filter(
            Cita.profesional_id.in_(ids_profesionales)
        )

    reservas_hoy = reservas_query.count()
    profesionales_activos = profesionales_query.count()
    citas_hoy = citas_hoy_query.count()

    horas_disponibles = max(
        0,
        (profesionales_activos * 10) - citas_hoy,
    )

    urgentes = urgentes_query.count()

    return {
        "reservas_hoy": reservas_hoy,
        "profesionales_activos": profesionales_activos,
        "horas_disponibles": horas_disponibles,
        "urgentes": urgentes,
    }


# ══════════════════════════════════════
# RESUMEN DEL DÍA
# ══════════════════════════════════════

@router.get("/resumen-dia")
def get_resumen_dia(db: Session = Depends(get_db), current_user: dict = Depends(require_effective_permission(Permission.AGENDA_VER))):
    hoy = date.today().isoformat()

    alcance = obtener_alcance_administrativo_efectivo(
        db,
        current_user,
    )

    if alcance is None:
        raise HTTPException(
            status_code=403,
            detail="No tienes acceso al alcance solicitado.",
        )

    profs = [
        p
        for p in db.query(Profesional).all()
        if especialidad_permitida_por_alcance(
            alcance,
            p.especialidad,
        )
    ]

    result = []
    for p in profs:
        citas_hoy = db.query(Cita).filter(
            Cita.profesional_id == p.id,
            Cita.fecha == hoy,
            Cita.estado.in_(["pendiente","completada"])
        ).count()
        result.append({
            "profesional_id": p.id,
            "nombre": p.nombre,
            "tratamiento": p.tratamiento,
            "especialidad": p.especialidad,
            "color_identificador": p.color_identificador,
            "estado": p.estado or "activo",
            "citas_hoy": citas_hoy
        })
    return result


# ══════════════════════════════════════
# PRÓXIMAS CITAS — con fecha
# ══════════════════════════════════════

@router.get("/proximas-citas")
def get_proximas_citas(db: Session = Depends(get_db), current_user: dict = Depends(require_effective_permission(Permission.AGENDA_VER))):
    hoy = date.today().isoformat()

    alcance = obtener_alcance_administrativo_efectivo(
        db,
        current_user,
    )

    if alcance is None:
        raise HTTPException(
            status_code=403,
            detail="No tienes acceso al alcance solicitado.",
        )

    query = db.query(Cita).filter(
        Cita.fecha >= hoy,
        Cita.estado == "pendiente",
    )

    if not alcance.institucional:
        ids_profesionales = _ids_profesionales_en_alcance(
            db,
            alcance,
        )

        if not ids_profesionales:
            return []

        # El alcance se aplica ANTES de order_by/limit para que el
        # limite de 20 corresponda a citas realmente visibles.
        query = query.filter(
            Cita.profesional_id.in_(ids_profesionales)
        )

    citas = (
        query
        .order_by(Cita.fecha, Cita.hora)
        .limit(20)
        .all()
    )

    result = []
    for c in citas:
        prof = db.query(Profesional).filter(Profesional.id == c.profesional_id).first()
        est  = db.query(Usuario).filter(Usuario.id == c.estudiante_id).first()
        result.append({
            "id":             c.id,
            "estudiante":     est.nombre        if est  else "—",
            "rut":            est.rut           if est  else "—",
            "especialidad":   prof.especialidad if prof else "—",
            "profesional":    prof.nombre       if prof else "—",
            "profesional_id": c.profesional_id,
            "fecha":          c.fecha,
            "hora":           c.hora,
            "urgente":        c.urgente or False,
            "estado":         c.estado
        })
    return result


# ══════════════════════════════════════
# BUSCAR ESTUDIANTE
# ══════════════════════════════════════

@router.get("/estudiantes")
def buscar_estudiantes(
    q: str = "",
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_effective_permission(Permission.USUARIOS_VER)
    ),
):
    alcance = obtener_alcance_administrativo_efectivo(
        db,
        current_user,
    )

    if alcance is None:
        raise HTTPException(
            status_code=403,
            detail="No tienes acceso al alcance solicitado.",
        )

    if len(q) < 2:
        return []

    query = db.query(Usuario).filter(
        Usuario.rol == "estudiante",
        (
            Usuario.nombre.ilike(f"%{q}%")
            | Usuario.rut.ilike(f"%{q}%")
        ),
    )

    if not alcance.institucional:
        ids_profesionales = _ids_profesionales_en_alcance(
            db,
            alcance,
        )

        if not ids_profesionales:
            return []

        query = (
            query
            .join(
                Cita,
                Cita.estudiante_id == Usuario.id,
            )
            .filter(
                Cita.profesional_id.in_(
                    ids_profesionales
                )
            )
            .distinct()
        )

    estudiantes = (
        query
        .limit(10)
        .all()
    )

    return [
        {
            "id": e.id,
            "nombre": e.nombre or "?",
            "rut": e.rut or "?",
            "carrera": e.carrera or "?",
            "correo": e.correo,
        }
        for e in estudiantes
    ]


@router.get("/estudiantes/listado")
def listar_estudiantes(
    q: str = "",
    carrera: str = "",
    pagina: int = 1,
    por_pagina: int = 20,
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_effective_permission(Permission.USUARIOS_VER)
    ),
):
    """
    Listado completo de estudiantes con paginaci?n.

    Para alcances administrativos limitados, tanto la visibilidad
    del estudiante como sus m?tricas se restringen a citas con
    profesionales incluidos en el alcance efectivo.
    """
    alcance = obtener_alcance_administrativo_efectivo(
        db,
        current_user,
    )

    if alcance is None:
        raise HTTPException(
            status_code=403,
            detail="No tienes acceso al alcance solicitado.",
        )

    ids_profesionales = None

    if not alcance.institucional:
        ids_profesionales = _ids_profesionales_en_alcance(
            db,
            alcance,
        )

        if not ids_profesionales:
            return {
                "total": 0,
                "pagina": pagina,
                "por_pagina": por_pagina,
                "estudiantes": [],
            }

    query = db.query(Usuario).filter(
        Usuario.rol == "estudiante"
    )

    if ids_profesionales is not None:
        query = (
            query
            .join(
                Cita,
                Cita.estudiante_id == Usuario.id,
            )
            .filter(
                Cita.profesional_id.in_(
                    ids_profesionales
                )
            )
            .distinct()
        )

    if q:
        query = query.filter(
            (
                Usuario.nombre.ilike(f"%{q}%")
                | Usuario.rut.ilike(f"%{q}%")
            )
        )

    if carrera:
        query = query.filter(
            Usuario.carrera.ilike(
                f"%{carrera}%"
            )
        )

    total = query.count()

    estudiantes = (
        query
        .order_by(Usuario.nombre)
        .offset(
            (pagina - 1) * por_pagina
        )
        .limit(por_pagina)
        .all()
    )

    ids = [e.id for e in estudiantes]

    conteos = {}

    if ids:
        conteos_query = (
            db.query(
                Cita.estudiante_id,
                func.count(Cita.id),
            )
            .filter(
                Cita.estudiante_id.in_(ids)
            )
        )

        if ids_profesionales is not None:
            conteos_query = conteos_query.filter(
                Cita.profesional_id.in_(
                    ids_profesionales
                )
            )

        conteos = dict(
            conteos_query
            .group_by(Cita.estudiante_id)
            .all()
        )

    atendidas = {}

    if ids:
        atendidas_query = (
            db.query(
                Cita.estudiante_id,
                func.count(Cita.id),
            )
            .filter(
                Cita.estudiante_id.in_(ids),
                Cita.estado == "completada",
            )
        )

        if ids_profesionales is not None:
            atendidas_query = atendidas_query.filter(
                Cita.profesional_id.in_(
                    ids_profesionales
                )
            )

        atendidas = dict(
            atendidas_query
            .group_by(Cita.estudiante_id)
            .all()
        )

    return {
        "total": total,
        "pagina": pagina,
        "por_pagina": por_pagina,
        "estudiantes": [
            {
                "id": e.id,
                "nombre": e.nombre or "?",
                "rut": e.rut or "?",
                "carrera": e.carrera or "?",
                "correo": e.correo,
                "citas_totales": conteos.get(e.id, 0),
                "citas_atendidas": atendidas.get(e.id, 0),
            }
            for e in estudiantes
        ],
    }


@router.get("/estudiantes/{estudiante_id}/perfil")
def perfil_estudiante_admin(
    estudiante_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_effective_permission(Permission.USUARIOS_VER)
    ),
):
    """
    Ficha administrativa de un estudiante.

    En alcance limitado, la visibilidad del estudiante y todas
    las m?tricas derivadas de citas se restringen a profesionales
    incluidos en el alcance efectivo.
    """
    alcance = obtener_alcance_administrativo_efectivo(
        db,
        current_user,
    )

    if alcance is None:
        raise HTTPException(
            status_code=403,
            detail="No tienes acceso al alcance solicitado.",
        )

    ids_profesionales = None

    if not alcance.institucional:
        ids_profesionales = _ids_profesionales_en_alcance(
            db,
            alcance,
        )

        if not ids_profesionales:
            raise HTTPException(
                status_code=404,
                detail="Estudiante no encontrado",
            )

    est = (
        db.query(Usuario)
        .filter(
            Usuario.id == estudiante_id,
            Usuario.rol == "estudiante",
        )
        .first()
    )

    if not est:
        raise HTTPException(
            status_code=404,
            detail="Estudiante no encontrado",
        )

    total_query = db.query(
        func.count(Cita.id)
    ).filter(
        Cita.estudiante_id == estudiante_id
    )

    if ids_profesionales is not None:
        total_query = total_query.filter(
            Cita.profesional_id.in_(
                ids_profesionales
            )
        )

    total = total_query.scalar() or 0

    # Para alcance limitado, una ficha administrativa solo es
    # visible si existe al menos una cita dentro del scope.
    if (
        ids_profesionales is not None
        and total == 0
    ):
        raise HTTPException(
            status_code=404,
            detail="Estudiante no encontrado",
        )

    citas_query = (
        db.query(Cita)
        .filter(
            Cita.estudiante_id == estudiante_id
        )
    )

    if ids_profesionales is not None:
        citas_query = citas_query.filter(
            Cita.profesional_id.in_(
                ids_profesionales
            )
        )

    citas = (
        citas_query
        .order_by(Cita.fecha.desc())
        .limit(10)
        .all()
    )

    ultimas = []

    for c in citas:
        prof = (
            db.query(Profesional)
            .filter(
                Profesional.id == c.profesional_id
            )
            .first()
        )

        ultimas.append({
            "id": c.id,
            "fecha": c.fecha,
            "hora": c.hora,
            "estado": c.estado,
            "especialidad": (
                prof.especialidad
                if prof
                else "?"
            ),
            "profesional": (
                prof.nombre
                if prof
                else "?"
            ),
        })

    atendidas_query = db.query(
        func.count(Cita.id)
    ).filter(
        Cita.estudiante_id == estudiante_id,
        Cita.estado == "completada",
    )

    if ids_profesionales is not None:
        atendidas_query = atendidas_query.filter(
            Cita.profesional_id.in_(
                ids_profesionales
            )
        )

    atendidas = atendidas_query.scalar() or 0

    return {
        "id": est.id,
        "nombre": est.nombre or "?",
        "rut": est.rut or "?",
        "carrera": est.carrera or "?",
        "correo": est.correo,
        "citas_totales": total,
        "citas_atendidas": atendidas,
        "ultimas_atenciones": ultimas,
    }


# ══════════════════════════════════════
# GRÁFICOS
# ══════════════════════════════════════

@router.get("/graficos/especialidad")
def get_grafico_especialidad(
    mes: int = None, anio: int = None,
    profesional_id: int = None, especialidad: str = None, carrera: str = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_effective_permission(Permission.REPORTES_VER))
):
    alcance = obtener_alcance_administrativo_efectivo(
        db,
        current_user,
    )

    if alcance is None:
        raise HTTPException(
            status_code=403,
            detail="No tienes acceso al alcance solicitado.",
        )

    ids_profesionales = None

    if not alcance.institucional:
        ids_profesionales = _ids_profesionales_en_alcance(
            db,
            alcance,
        )

        if not ids_profesionales:
            return []

    query = db.query(
        Profesional.especialidad,
        func.count(Cita.id),
    )\
        .join(Cita, Cita.profesional_id == Profesional.id)\
        .join(Usuario, Cita.estudiante_id == Usuario.id)\
        .filter(Cita.estado.in_(["pendiente", "completada"]))

    if ids_profesionales is not None:
        query = query.filter(
            Cita.profesional_id.in_(ids_profesionales)
        )
    if mes and anio:
        query = query.filter(Cita.fecha.like(f"{anio}-{str(mes).zfill(2)}%"))
    elif anio:
        query = query.filter(Cita.fecha.like(f"{anio}%"))
    if profesional_id: query = query.filter(Cita.profesional_id == profesional_id)
    if especialidad:   query = query.filter(Profesional.especialidad == especialidad)
    if carrera:        query = query.filter(Usuario.carrera.ilike(f"%{carrera}%"))
    resultados = query.group_by(Profesional.especialidad).all()
    total = sum(r[1] for r in resultados) or 1
    return [{"especialidad": r[0], "cantidad": r[1], "porcentaje": round((r[1]/total)*100)} for r in resultados]


@router.get("/graficos/semana")
def get_grafico_semana(
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_effective_permission(Permission.REPORTES_VER)
    ),
):
    hoy = date.today()
    lunes = hoy - timedelta(days=hoy.weekday())
    dias = ["Lun", "Mar", "Mi?", "Jue", "Vie", "S?b", "Dom"]

    alcance = obtener_alcance_administrativo_efectivo(
        db,
        current_user,
    )

    if alcance is None:
        raise HTTPException(
            status_code=403,
            detail="No tienes acceso al alcance solicitado.",
        )

    ids_profesionales = None

    if not alcance.institucional:
        ids_profesionales = _ids_profesionales_en_alcance(
            db,
            alcance,
        )

        if not ids_profesionales:
            return [
                {
                    "dia": dias[i],
                    "fecha": (
                        lunes + timedelta(days=i)
                    ).isoformat(),
                    "cantidad": 0,
                }
                for i in range(7)
            ]

    result = []

    for i in range(7):
        dia = lunes + timedelta(days=i)

        query = db.query(Cita).filter(
            Cita.fecha == dia.isoformat(),
            Cita.estado.in_(
                ["pendiente", "completada"]
            ),
        )

        if ids_profesionales is not None:
            query = query.filter(
                Cita.profesional_id.in_(
                    ids_profesionales
                )
            )

        cantidad = query.count()

        result.append({
            "dia": dias[i],
            "fecha": dia.isoformat(),
            "cantidad": cantidad,
        })

    return result


# ══════════════════════════════════════
# GESTIÓN DE PROFESIONALES
# ══════════════════════════════════════

@router.get("/profesionales")
def get_profesionales_admin(db: Session = Depends(get_db), current_user: dict = Depends(require_effective_permission(Permission.PROFESIONALES_VER))):
    alcance = obtener_alcance_administrativo_efectivo(
        db,
        current_user,
    )

    if alcance is None:
        raise HTTPException(
            status_code=403,
            detail="No tienes acceso al alcance solicitado.",
        )

    profesionales = [
        p
        for p in db.query(Profesional).all()
        if especialidad_permitida_por_alcance(
            alcance,
            p.especialidad,
        )
    ]

    return [
        {
            "id": p.id, "nombre": p.nombre, "tratamiento": p.tratamiento,
            "especialidad": p.especialidad,
            "iniciales": p.iniciales, "descripcion": p.descripcion,
            "duracion_min": p.duracion_min, "estado": p.estado or "activo",
            "correo": p.correo, "rut": p.rut, "usuario_id": p.usuario_id,
            "foto_url": p.foto_url, "color_identificador": p.color_identificador
        }
        for p in profesionales
    ]


@router.post("/profesionales")
def crear_profesional(
    datos: ProfesionalCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_effective_permission(
            Permission.PROFESIONALES_GESTIONAR
        )
    ),
):
    alcance = obtener_alcance_administrativo_efectivo(
        db,
        current_user,
    )

    if alcance is None:
        raise HTTPException(
            status_code=403,
            detail="No tienes acceso al alcance solicitado.",
        )

    if not especialidad_permitida_por_alcance(
        alcance,
        datos.especialidad,
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "No tienes permiso para gestionar "
                "profesionales de esta especialidad."
            ),
        )

    if datos.rut and not validar_rut(datos.rut):
        raise HTTPException(
            status_code=400,
            detail="El RUT ingresado no es v?lido",
        )

    if not color_identificador_es_valido(
        datos.color_identificador
    ):
        raise HTTPException(
            status_code=400,
            detail="Color identificador no permitido",
        )

    iniciales = datos.iniciales

    if not iniciales and datos.nombre:
        partes = datos.nombre.split()
        iniciales = (
            (partes[0][0] + partes[1][0]).upper()
            if len(partes) >= 2
            else datos.nombre[:2].upper()
        )

    nuevo_usuario = Usuario(
        correo=datos.correo,
        password=hash_password(
            datos.password or "prof123"
        ),
        rol="profesional",
        nombre=datos.nombre,
        activo=True,
    )

    db.add(nuevo_usuario)

    # Obtener nuevo_usuario.id SIN commit, para poder crear
    # Profesional con el FK correcto.
    db.flush()

    nuevo_prof = Profesional(
        nombre=datos.nombre,
        tratamiento=datos.tratamiento,
        especialidad=datos.especialidad,
        iniciales=iniciales,
        descripcion=datos.descripcion or "",
        duracion_min=datos.duracion_min or 45,
        correo=datos.correo,
        rut=datos.rut,
        estado="activo",
        usuario_id=nuevo_usuario.id,
        color_identificador=datos.color_identificador,
    )

    db.add(nuevo_prof)

    # Obtener nuevo_prof.id SIN commit para la auditor?a.
    db.flush()

    registrar_evento_auditoria(
        db,
        current_user,
        "Agreg? profesional",
        entidad="profesional",
        entidad_id=nuevo_prof.id,
        detalle=(
            f"{datos.nombre} ? "
            f"{datos.especialidad}"
        ),
    )

    # ?NICO commit: Usuario + Profesional + auditor?a,
    # misma transacci?n.
    db.commit()

    db.refresh(nuevo_prof)

    return nuevo_prof


@router.patch("/profesionales/{prof_id}")
def actualizar_profesional(
    prof_id: int,
    datos: ProfesionalUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_effective_permission(
            Permission.PROFESIONALES_GESTIONAR
        )
    ),
):
    alcance = obtener_alcance_administrativo_efectivo(
        db,
        current_user,
    )

    if alcance is None:
        raise HTTPException(
            status_code=403,
            detail="No tienes acceso al alcance solicitado.",
        )

    prof = (
        db.query(Profesional)
        .filter(
            Profesional.id == prof_id
        )
        .first()
    )

    if (
        not prof
        or not especialidad_permitida_por_alcance(
            alcance,
            prof.especialidad,
        )
    ):
        # No revelar profesionales pertenecientes
        # a otra especialidad administrativa.
        raise HTTPException(
            status_code=404,
            detail="Profesional no encontrado",
        )

    if (
        datos.especialidad is not None
        and not especialidad_permitida_por_alcance(
            alcance,
            datos.especialidad,
        )
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "No tienes permiso para mover al profesional "
                "a esta especialidad."
            ),
        )

    if datos.rut and not validar_rut(datos.rut):
        raise HTTPException(
            status_code=400,
            detail="El RUT ingresado no es v?lido",
        )

    if (
        "color_identificador" in datos.model_fields_set
        and not color_identificador_es_valido(
            datos.color_identificador
        )
    ):
        raise HTTPException(
            status_code=400,
            detail="Color identificador no permitido",
        )

    if datos.nombre is not None:
        prof.nombre = datos.nombre

    # tratamiento y color_identificador distinguen entre
    # "no enviado" y "enviado expl?citamente como null".
    if "tratamiento" in datos.model_fields_set:
        prof.tratamiento = datos.tratamiento

    if "color_identificador" in datos.model_fields_set:
        prof.color_identificador = datos.color_identificador

    if datos.especialidad is not None:
        prof.especialidad = datos.especialidad

    if datos.iniciales is not None:
        prof.iniciales = datos.iniciales

    if datos.descripcion is not None:
        prof.descripcion = datos.descripcion

    if datos.duracion_min is not None:
        prof.duracion_min = datos.duracion_min

    if datos.correo is not None:
        prof.correo = datos.correo

    if datos.rut is not None:
        prof.rut = datos.rut

    if datos.estado is not None:
        prof.estado = datos.estado

    if datos.nombre and prof.usuario_id:
        usuario = (
            db.query(Usuario)
            .filter(
                Usuario.id == prof.usuario_id
            )
            .first()
        )

        if usuario:
            usuario.nombre = datos.nombre

    registrar_evento_auditoria(
        db,
        current_user,
        "Edit? profesional",
        entidad="profesional",
        entidad_id=prof_id,
        detalle=prof.nombre,
    )

    db.commit()
    db.refresh(prof)

    return prof


@router.delete("/profesionales/{prof_id}")
def eliminar_profesional(
    prof_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_effective_permission(
            Permission.PROFESIONALES_GESTIONAR
        )
    ),
):
    alcance = obtener_alcance_administrativo_efectivo(
        db,
        current_user,
    )

    if alcance is None:
        raise HTTPException(
            status_code=403,
            detail="No tienes acceso al alcance solicitado.",
        )

    prof = (
        db.query(Profesional)
        .filter(
            Profesional.id == prof_id
        )
        .first()
    )

    if (
        not prof
        or not especialidad_permitida_por_alcance(
            alcance,
            prof.especialidad,
        )
    ):
        # No revelar profesionales pertenecientes
        # a otra especialidad administrativa.
        raise HTTPException(
            status_code=404,
            detail="Profesional no encontrado",
        )

    citas = (
        db.query(Cita)
        .filter(
            Cita.profesional_id == prof_id,
            Cita.estado == "pendiente",
        )
        .all()
    )

    for cita in citas:
        est = (
            db.query(Usuario)
            .filter(
                Usuario.id == cita.estudiante_id
            )
            .first()
        )

        cita.estado = "cancelada"
        cita.cancelada_por_admin = True
        cita.motivo_cancelacion = (
            "Profesional eliminado del sistema"
        )

        db.add(
            Notificacion(
                usuario_id=cita.estudiante_id,
                mensaje=(
                    f"Tu cita del {cita.fecha} a las "
                    f"{cita.hora} fue cancelada porque el "
                    "profesional ya no est? disponible en SESAES."
                ),
                tipo="cancelacion",
            )
        )

        simular_envio_correo(
            db,
            destinatario=(
                est.correo
                if est
                else "?"
            ),
            asunto="SESAES ? Cancelaci?n de cita",
            cuerpo=(
                f"Tu cita del {cita.fecha} a las "
                f"{cita.hora} fue cancelada."
            ),
            tipo="cancelacion",
            referencia_id=cita.id,
        )

    if prof.usuario_id:
        usuario = (
            db.query(Usuario)
            .filter(
                Usuario.id == prof.usuario_id
            )
            .first()
        )

        if usuario:
            usuario.activo = False

    nombre_prof = prof.nombre

    db.delete(prof)

    registrar_evento_auditoria(
        db,
        current_user,
        "Elimin? profesional",
        entidad="profesional",
        entidad_id=prof_id,
        detalle=(
            f"{nombre_prof} ? "
            f"{len(citas)} citas canceladas"
        ),
    )

    db.commit()

    return {
        "message": "Profesional eliminado correctamente"
    }


@router.patch("/profesionales/{prof_id}/estado")
def cambiar_estado_profesional(
    prof_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_effective_permission(
            Permission.PROFESIONALES_GESTIONAR
        )
    ),
):
    alcance = obtener_alcance_administrativo_efectivo(
        db,
        current_user,
    )

    if alcance is None:
        raise HTTPException(
            status_code=403,
            detail="No tienes acceso al alcance solicitado.",
        )

    prof = (
        db.query(Profesional)
        .filter(
            Profesional.id == prof_id
        )
        .first()
    )

    if (
        not prof
        or not especialidad_permitida_por_alcance(
            alcance,
            prof.especialidad,
        )
    ):
        # No revelar profesionales fuera del alcance
        # administrativo efectivo.
        raise HTTPException(
            status_code=404,
            detail="Profesional no encontrado",
        )

    estado_anterior = prof.estado or "activo"
    estado_nuevo = body.get(
        "estado",
        "activo",
    )
    cancelar_citas = body.get(
        "cancelar_citas",
        False,
    )
    fecha_afectada = body.get(
        "fecha",
        date.today().isoformat(),
    )
    motivo = body.get("motivo")

    prof.estado = estado_nuevo

    db.add(
        HistorialEstadoProfesional(
            profesional_id=prof_id,
            estado_anterior=estado_anterior,
            estado_nuevo=estado_nuevo,
            motivo=motivo,
            registrado_por=None,
        )
    )

    registrar_evento_auditoria(
        db,
        current_user,
        "Cambi? estado de profesional",
        entidad="profesional",
        entidad_id=prof_id,
        detalle=(
            f"{prof.nombre}: "
            f"{estado_anterior} ? {estado_nuevo}"
        ),
    )

    # Se conserva el commit hist?rico previo a la eventual
    # cancelaci?n masiva de citas.
    db.commit()

    if cancelar_citas:
        citas = (
            db.query(Cita)
            .filter(
                Cita.profesional_id == prof_id,
                Cita.fecha == fecha_afectada,
                Cita.estado == "pendiente",
            )
            .all()
        )

        for cita in citas:
            est = (
                db.query(Usuario)
                .filter(
                    Usuario.id == cita.estudiante_id
                )
                .first()
            )

            cita.estado = "cancelada"
            cita.cancelada_por_admin = True
            cita.motivo_cancelacion = (
                f"Profesional: {estado_nuevo}"
            )

            db.add(
                Notificacion(
                    usuario_id=cita.estudiante_id,
                    mensaje=(
                        "Tu cita fue cancelada por fuerza mayor. "
                        "Puedes reagendar tu hora cuando lo desees "
                        "desde tu dashboard."
                    ),
                    tipo="cancelacion",
                )
            )

            simular_envio_correo(
                db,
                destinatario=(
                    est.correo
                    if est
                    else "?"
                ),
                asunto="SESAES ? Tu cita fue cancelada",
                cuerpo=(
                    f"Tu cita del {cita.fecha} a las "
                    f"{cita.hora} fue cancelada por fuerza mayor."
                ),
                tipo="cancelacion",
                referencia_id=cita.id,
            )

        registrar_evento_auditoria(
            db,
            current_user,
            "Cancel? citas masivas",
            entidad="profesional",
            entidad_id=prof_id,
            detalle=(
                f"{prof.nombre} ? "
                f"{len(citas)} citas el {fecha_afectada}"
            ),
        )

        db.commit()

        return {
            "message": (
                f"Estado '{estado_nuevo}'. "
                f"{len(citas)} citas canceladas."
            ),
            "citas_canceladas": len(citas),
        }

    return {
        "message": (
            f"Estado actualizado a '{estado_nuevo}'"
        )
    }


@router.get("/profesionales/{prof_id}/historial-estados")
def get_historial_estados(
    prof_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_effective_permission(Permission.PROFESIONALES_VER)
    ),
):
    """
    Devuelve el historial administrativo de estados de un
    profesional visible dentro del alcance efectivo.
    """
    alcance = obtener_alcance_administrativo_efectivo(
        db,
        current_user,
    )

    if alcance is None:
        raise HTTPException(
            status_code=403,
            detail="No tienes acceso al alcance solicitado.",
        )

    prof = (
        db.query(Profesional)
        .filter(
            Profesional.id == prof_id
        )
        .first()
    )

    if (
        not prof
        or not especialidad_permitida_por_alcance(
            alcance,
            prof.especialidad,
        )
    ):
        # No revelar si existe un profesional perteneciente
        # a otra especialidad administrativa.
        raise HTTPException(
            status_code=404,
            detail="Profesional no encontrado",
        )

    registros = (
        db.query(HistorialEstadoProfesional)
        .filter(
            HistorialEstadoProfesional.profesional_id == prof_id
        )
        .order_by(
            HistorialEstadoProfesional.fecha.desc()
        )
        .all()
    )

    return [
        {
            "id": r.id,
            "estado_anterior": r.estado_anterior,
            "estado_nuevo": r.estado_nuevo,
            "motivo": r.motivo,
            "fecha": (
                r.fecha.isoformat()
                if r.fecha
                else None
            ),
        }
        for r in registros
    ]


# ══════════════════════════════════════
# CITAS URGENTES
# ══════════════════════════════════════
#
# A.2 — decisión explícita: este endpoint NO pasa por
# evaluar_disponibilidad_slot() (app.services.agenda_disponibilidad_service).
# A.2 conserva la semántica actual de urgencias y no la redefine; su
# relación definitiva con disponibilidad se decidirá en la fase
# específica de urgencias/emergencias.

@router.post("/citas/urgente")
def crear_cita_urgente(cita: CitaCreate, db: Session = Depends(get_db), current_user: dict = Depends(require_effective_permission(Permission.AGENDA_GESTIONAR))):
    alcance = obtener_alcance_administrativo_efectivo(
        db,
        current_user,
    )

    if alcance is None:
        raise HTTPException(
            status_code=403,
            detail="No tienes acceso al alcance solicitado.",
        )

    # A.3 (v3) — canonicalizar la fecha UNA sola vez, antes de CUALQUIER
    # query que dependa de ella, y reutilizar exactamente ese mismo
    # valor en todo el resto del flujo (DiaCerrado, lock,
    # hay_solapamiento_con_cita_activa, INSERT de Cita.fecha) — ver
    # canonicalizar_fecha_valida() en agenda_disponibilidad_service.py.
    # Una fecha no interpretable nunca se mutasilenciosamente ni se deja
    # pasar: se rechaza acá mismo con 400, antes de la consulta de
    # DiaCerrado que existía antes de esta corrección y que comparaba
    # contra el string crudo.
    try:
        fecha_canon = canonicalizar_fecha_valida(cita.fecha)
    except SlotInvalidoError as exc:
        raise HTTPException(status_code=400, detail=exc.mensaje)

    if db.query(DiaCerrado).filter(DiaCerrado.fecha == fecha_canon).first():
        raise HTTPException(status_code=400, detail="El centro permanece cerrado ese día. Elige otra fecha.")

    prof = db.query(Profesional).filter(Profesional.id == cita.profesional_id).first()
    if not prof:
        raise HTTPException(
            status_code=404,
            detail="Profesional no encontrado",
        )

    if not especialidad_permitida_por_alcance(
        alcance,
        prof.especialidad,
    ):
        # No revelar que existe un profesional fuera del
        # alcance administrativo del usuario.
        raise HTTPException(
            status_code=404,
            detail="Profesional no encontrado",
        )
    est = db.query(Usuario).filter(Usuario.id == cita.estudiante_id).first()
    if not est or est.rol != "estudiante":
        raise HTTPException(
            status_code=400,
            detail="El paciente seleccionado no corresponde a una cuenta de estudiante."
        )
    # A.3 (v3) — se guarda fecha_canon, NUNCA cita.fecha crudo: es la
    # misma forma canónica ya usada arriba para DiaCerrado (ver
    # canonicalizar_fecha_valida()); se reutiliza también abajo para el
    # lock y hay_solapamiento_con_cita_activa. Guardar el string crudo
    # aquí era precisamente el hueco que A.3 (v2) dejaba abierto.
    # A.4.1 — mismo criterio de origen que POST /citas (ver
    # app.services.cita_origen_service): current_user ya viene resuelto
    # por get_current_user() contra el estado ACTUAL de Usuario en BD.
    # A.4.1 NO cambia nada más de este endpoint: sigue sin pasar por
    # evaluar_disponibilidad_slot(), sigue sin crear CitaSobrecupo, y
    # conserva exactamente su auditoría actual ("Creó cita urgente").
    origen = resolver_origen_cita(db, current_user)

    nueva = Cita(estudiante_id=cita.estudiante_id, profesional_id=cita.profesional_id,
                 fecha=fecha_canon, hora=cita.hora, observaciones=cita.observaciones,
                 estado="pendiente", urgente=True,
                 creado_por_usuario_id=origen.creado_por_usuario_id,
                 creado_por_rol=origen.creado_por_rol,
                 creado_por_perfil=origen.creado_por_perfil)

    # A.3 — concurrencia/doble-reserva, y cierre del hueco de ocupación
    # que este endpoint tenía desde A.2 (ver diagnóstico de A.3):
    # crear_cita_urgente() deliberadamente NO llama a
    # evaluar_disponibilidad_slot() — no evalúa grilla, jornada,
    # colación ni cierre de centro, decisión histórica de A.2 que A.3
    # NO amplía (su relación definitiva con disponibilidad estructural
    # sigue pendiente de la fase específica de urgencias/emergencias).
    # Pero SÍ debe protegerse, como cualquier otro canal, de dejar dos
    # citas activas solapadas para el mismo profesional — ese es el
    # objetivo mínimo obligatorio de A.3, y hoy este endpoint podía
    # solapar una cita activa incluso SIN ninguna carrera de por medio,
    # simplemente porque nunca comprobaba ocupación.
    #
    # El lock debe adquirirse ANTES de comprobar solapamiento, y la
    # comprobación debe repetirse DENTRO de él — evaluar antes del lock
    # y confiar en que el resultado siga vigente al insertar es
    # exactamente la carrera que esto existe para cerrar (ver
    # adquirir_lock_agenda_profesional_fecha()).
    #
    # urgente=True NO es un permiso para crear una segunda cita encima
    # de otra activa: si hay solapamiento, 409 — nunca se convierte
    # automáticamente en sobrecupo (que además este endpoint ni
    # siquiera expone hoy; ver CitaCreate.sobrecupo, ignorado acá).
    adquirir_lock_agenda_profesional_fecha(
        db,
        profesional_id=cita.profesional_id,
        fecha=fecha_canon,
    )
    # A.3 (v2) — fecha/hora no interpretables deben rechazarse con 400
    # ANTES de decidir si hay solapamiento: nunca "no hay solapamiento"
    # por defecto (fail-open), porque este endpoint no pasa por
    # evaluar_disponibilidad_slot() y no tiene otra red de validación
    # de formato. Ver SlotInvalidoError en agenda_disponibilidad_service.py.
    # (v3) — `fecha` ya se canonicalizó y validó arriba con
    # canonicalizar_fecha_valida(); este try/except queda por si
    # `hora` es la que resulta inválida (fecha ya no puede fallar acá).
    try:
        hay_conflicto = hay_solapamiento_con_cita_activa(
            db,
            profesional=prof,
            fecha=fecha_canon,
            hora=cita.hora,
        )
    except SlotInvalidoError as exc:
        raise HTTPException(status_code=400, detail=exc.mensaje)

    if hay_conflicto:
        raise HTTPException(
            status_code=409,
            detail="Esa hora ya está reservada por otra cita activa. Elige otra hora.",
        )

    db.add(nueva)
    try:
        # SA-2 — transaccionalidad: flush() envía el INSERT de `nueva` a la
        # base de datos SIN hacer commit todavía. Esto basta para obtener
        # nueva.id (necesario para el correo y la auditoría) y para que
        # cualquier IntegrityError real de este INSERT (p. ej. violación de
        # FK) se detecte antes de continuar, sin partir la transacción en
        # dos commits separados.
        #
        # A.3 — este except sigue sin ser (nunca lo fue) la protección
        # contra doble reserva: Cita no tiene, ni tuvo nunca, ningún
        # UniqueConstraint/Index único sobre profesional_id + fecha +
        # hora (confirmado en el diagnóstico de A.3; el aviso explícito
        # que existía antes en este mismo bloque ya lo señalaba
        # correctamente). La protección real contra doble
        # reserva/concurrencia es, desde A.3,
        # adquirir_lock_agenda_profesional_fecha() +
        # hay_solapamiento_con_cita_activa() evaluados DENTRO de ese
        # lock, arriba. Este bloque se conserva solo como red de
        # seguridad genérica ante cualquier otra violación de
        # integridad real (p. ej. FK), no como mecanismo de
        # concurrencia.
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Esa hora acaba de ser reservada por otra persona. Por favor elige otra."
        )

    simular_envio_correo(db, destinatario=est.correo if est else "—",
        asunto="SESAES — Cita urgente agendada",
        cuerpo=f"Se agendó una cita URGENTE para el {cita.fecha} a las {cita.hora} con {prof.nombre}.",
        tipo="urgente", referencia_id=nueva.id)

    registrar_evento_auditoria(
        db, current_user, "Creó cita urgente",
        entidad="cita", entidad_id=nueva.id,
        detalle=f"Estudiante: {est.nombre if est else cita.estudiante_id} — {prof.nombre} ({cita.fecha} {cita.hora})",
    )

    db.commit()  # ÚNICO commit: Cita + log de correo (correo_log) + auditoría, misma transacción
    db.refresh(nueva)
    return {"id": nueva.id, "profesional": prof.nombre, "especialidad": prof.especialidad,
            "fecha": nueva.fecha, "hora": nueva.hora, "urgente": True, "estado": nueva.estado}


@router.patch("/citas/{cita_id}/cancelar")
def cancelar_cita_admin(
    cita_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_effective_permission(Permission.AGENDA_GESTIONAR)
    ),
):
    alcance = obtener_alcance_administrativo_efectivo(
        db,
        current_user,
    )

    if alcance is None:
        raise HTTPException(
            status_code=403,
            detail="No tienes acceso al alcance solicitado.",
        )

    cita = (
        db.query(Cita)
        .filter(
            Cita.id == cita_id
        )
        .first()
    )

    if not cita:
        raise HTTPException(
            status_code=404,
            detail="Cita no encontrada",
        )

    prof = (
        db.query(Profesional)
        .filter(
            Profesional.id == cita.profesional_id
        )
        .first()
    )

    if (
        not prof
        or not especialidad_permitida_por_alcance(
            alcance,
            prof.especialidad,
        )
    ):
        # No revelar la existencia de citas pertenecientes
        # a profesionales fuera del alcance administrativo.
        raise HTTPException(
            status_code=404,
            detail="Cita no encontrada",
        )

    est = (
        db.query(Usuario)
        .filter(
            Usuario.id == cita.estudiante_id
        )
        .first()
    )

    motivo = body.get(
        "motivo",
        "Cancelada por administrador",
    )

    cita.estado = "cancelada"
    cita.cancelada_por_admin = True
    cita.motivo_cancelacion = motivo

    db.add(
        Notificacion(
            usuario_id=cita.estudiante_id,
            mensaje=(
                "Tu cita fue cancelada por fuerza mayor. "
                "Puedes reagendar tu hora cuando lo desees "
                "desde tu dashboard."
            ),
            tipo="cancelacion",
        )
    )

    simular_envio_correo(
        db,
        destinatario=(
            est.correo
            if est
            else "?"
        ),
        asunto="SESAES ? Tu cita fue cancelada",
        cuerpo=(
            f"Tu cita del {cita.fecha} a las {cita.hora} "
            f"con {prof.nombre} fue cancelada."
        ),
        tipo="cancelacion",
        referencia_id=cita_id,
    )

    registrar_evento_auditoria(
        db,
        current_user,
        "Cancel? cita",
        entidad="cita",
        entidad_id=cita_id,
        detalle=(
            f"Estudiante: "
            f"{est.nombre if est else '?'} ? "
            f"{prof.nombre} ? "
            f"{cita.fecha} {cita.hora}"
        ),
    )

    db.commit()

    return {
        "message": "Cita cancelada y estudiante notificado"
    }


@router.patch("/citas/{cita_id}/prioridad")
def cambiar_prioridad_cita(
    cita_id: int,
    body: dict,
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_effective_permission(Permission.AGENDA_GESTIONAR)
    ),
):
    """
    Cambia la prioridad administrativa de una cita existente.

    La acci?n solo puede aplicarse a citas cuyo profesional se
    encuentre dentro del alcance administrativo efectivo.
    """
    alcance = obtener_alcance_administrativo_efectivo(
        db,
        current_user,
    )

    if alcance is None:
        raise HTTPException(
            status_code=403,
            detail="No tienes acceso al alcance solicitado.",
        )

    cita = (
        db.query(Cita)
        .filter(
            Cita.id == cita_id
        )
        .first()
    )

    if not cita:
        raise HTTPException(
            status_code=404,
            detail="Cita no encontrada",
        )

    prof = (
        db.query(Profesional)
        .filter(
            Profesional.id == cita.profesional_id
        )
        .first()
    )

    if (
        not prof
        or not especialidad_permitida_por_alcance(
            alcance,
            prof.especialidad,
        )
    ):
        # No revelar la existencia ni el estado de una cita
        # perteneciente a otra especialidad.
        raise HTTPException(
            status_code=404,
            detail="Cita no encontrada",
        )

    if cita.estado in (
        "cancelada",
        "completada",
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "No se puede cambiar la prioridad de una cita "
                "cancelada o ya completada."
            ),
        )

    nuevo_valor = bool(
        body.get(
            "urgente",
            False,
        )
    )

    cita.urgente = nuevo_valor

    est = (
        db.query(Usuario)
        .filter(
            Usuario.id == cita.estudiante_id
        )
        .first()
    )

    if nuevo_valor:
        db.add(
            Notificacion(
                usuario_id=cita.estudiante_id,
                mensaje=(
                    f"Tu cita del {cita.fecha} a las "
                    f"{cita.hora} fue marcada como urgente "
                    "por administraci?n."
                ),
                tipo="urgente",
            )
        )

    registrar_evento_auditoria(
        db,
        current_user,
        (
            "Marc? cita como urgente"
            if nuevo_valor
            else "Quit? prioridad urgente a cita"
        ),
        entidad="cita",
        entidad_id=cita_id,
        detalle=(
            f"Estudiante: "
            f"{est.nombre if est else '?'} ? "
            f"{prof.nombre} ? "
            f"{cita.fecha} {cita.hora}"
        ),
    )

    db.commit()

    return {
        "id": cita.id,
        "urgente": cita.urgente,
        "estado": cita.estado,
    }


# ══════════════════════════════════════
# DÍAS CERRADOS — el centro completo no atiende ese día
# ══════════════════════════════════════
# A diferencia de un feriado "normal" (que solo se marca visualmente, sin
# bloquear el agendamiento), un día cerrado SÍ bloquea por completo el
# agendamiento para cualquier profesional, y cancela + notifica
# automáticamente cualquier cita que ya existiera para esa fecha.

@router.get("/dias-cerrados")
def listar_dias_cerrados(
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_effective_permission(Permission.AGENDA_VER)
    ),
):
    """
    Devuelve los d?as en que el centro completo est? cerrado.

    Esta informaci?n es institucional y no se filtra por
    especialidad: cualquier administrador con permiso de lectura
    de agenda necesita conocer los cierres que afectan al centro.
    """
    dias = (
        db.query(DiaCerrado)
        .order_by(DiaCerrado.fecha)
        .all()
    )

    return [
        {
            "id": d.id,
            "fecha": d.fecha,
            "motivo": d.motivo,
            "fecha_creacion": d.fecha_creacion,
        }
        for d in dias
    ]


@router.post("/dias-cerrados")
def crear_dia_cerrado(body: dict, db: Session = Depends(get_db), current_user: dict = Depends(require_effective_permission(Permission.AGENDA_GESTIONAR))):
    alcance = obtener_alcance_administrativo_efectivo(
        db,
        current_user,
    )

    if (
        alcance is None
        or not alcance.institucional
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "Esta operaci?n requiere alcance "
                "administrativo institucional."
            ),
        )

    fecha  = body.get("fecha")
    motivo = body.get("motivo") or "El centro permanecerá cerrado este día."
    if not fecha:
        raise HTTPException(status_code=400, detail="Debes indicar la fecha a cerrar.")

    ya_existe = db.query(DiaCerrado).filter(DiaCerrado.fecha == fecha).first()
    if ya_existe:
        raise HTTPException(status_code=400, detail="Ese día ya está marcado como cerrado.")

    dia = DiaCerrado(fecha=fecha, motivo=motivo, creado_por=current_user["id"])
    db.add(dia)

    # Cancelar y notificar TODAS las citas pendientes de ese día,
    # sin importar el profesional.
    citas_afectadas = db.query(Cita).filter(Cita.fecha == fecha, Cita.estado == "pendiente").all()
    detalle_citas_canceladas = []
    for cita in citas_afectadas:
        cita.estado = "cancelada"
        cita.cancelada_por_admin = True
        cita.motivo_cancelacion = f"El centro permanecerá cerrado el {fecha}. {motivo}"

        est = db.query(Usuario).filter(Usuario.id == cita.estudiante_id).first()
        prof = db.query(Profesional).filter(Profesional.id == cita.profesional_id).first()

        detalle_citas_canceladas.append({
            "cita_id": cita.id,
            "estudiante": est.nombre if est else "—",
            "rut": est.rut if est else "—",
            "correo": est.correo if est else "—",
            "profesional": prof.nombre if prof else "—",
            "especialidad": prof.especialidad if prof else "—",
            "hora": cita.hora,
        })

        db.add(Notificacion(
            usuario_id=cita.estudiante_id,
            mensaje=f"Tu cita del {fecha} fue cancelada: el centro permanecerá cerrado ese día. "
                    f"Puedes reagendar cuando quieras desde tu dashboard.",
            tipo="cancelacion"
        ))
        simular_envio_correo(db,
            destinatario=est.correo if est else "—",
            asunto="SESAES — Tu cita fue cancelada (centro cerrado)",
            cuerpo=f"Tu cita del {fecha} a las {cita.hora} con {prof.nombre if prof else ''} fue cancelada "
                   f"porque el centro permanecerá cerrado ese día. Motivo: {motivo}. "
                   f"Puedes reagendar cuando quieras desde tu dashboard.",
            tipo="cancelacion", referencia_id=cita.id
        )

    registrar_evento_auditoria(db, current_user, "Cerró el centro un día completo",
                                entidad="dia_cerrado", entidad_id=None,
                                detalle=f"{fecha} — {motivo} ({len(citas_afectadas)} citas canceladas y notificadas)")
    db.commit()
    db.refresh(dia)
    return {
        "message": f"Día {fecha} cerrado correctamente.",
        "id": dia.id,
        "citas_canceladas": len(citas_afectadas),
        "detalle_citas_canceladas": detalle_citas_canceladas
    }


@router.get("/dias-cerrados/{dia_id}/citas")
def citas_canceladas_por_dia_cerrado(
    dia_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_effective_permission(Permission.AGENDA_VER)
    ),
):
    """
    Detalle administrativo de las citas canceladas en un d?a
    de cierre institucional.

    El cierre es informaci?n global, pero las personas y citas
    mostradas respetan el alcance administrativo efectivo.
    """
    alcance = obtener_alcance_administrativo_efectivo(
        db,
        current_user,
    )

    if alcance is None:
        raise HTTPException(
            status_code=403,
            detail="No tienes acceso al alcance solicitado.",
        )

    dia = (
        db.query(DiaCerrado)
        .filter(
            DiaCerrado.id == dia_id
        )
        .first()
    )

    if not dia:
        raise HTTPException(
            status_code=404,
            detail="D?a cerrado no encontrado",
        )

    ids_profesionales = None

    if not alcance.institucional:
        ids_profesionales = _ids_profesionales_en_alcance(
            db,
            alcance,
        )

        if not ids_profesionales:
            return []

    query = db.query(Cita).filter(
        Cita.fecha == dia.fecha,
        Cita.cancelada_por_admin == True,  # noqa: E712
    )

    if ids_profesionales is not None:
        query = query.filter(
            Cita.profesional_id.in_(
                ids_profesionales
            )
        )

    citas = query.all()

    resultado = []

    for cita in citas:
        est = (
            db.query(Usuario)
            .filter(
                Usuario.id == cita.estudiante_id
            )
            .first()
        )

        prof = (
            db.query(Profesional)
            .filter(
                Profesional.id == cita.profesional_id
            )
            .first()
        )

        resultado.append({
            "cita_id": cita.id,
            "estudiante": (
                est.nombre
                if est
                else "?"
            ),
            "rut": (
                est.rut
                if est
                else "?"
            ),
            "correo": (
                est.correo
                if est
                else "?"
            ),
            "profesional": (
                prof.nombre
                if prof
                else "?"
            ),
            "especialidad": (
                prof.especialidad
                if prof
                else "?"
            ),
            "hora": cita.hora,
        })

    return resultado


@router.delete("/dias-cerrados/{dia_id}")
def eliminar_dia_cerrado(dia_id: int, db: Session = Depends(get_db), current_user: dict = Depends(require_effective_permission(Permission.AGENDA_GESTIONAR))):
    alcance = obtener_alcance_administrativo_efectivo(
        db,
        current_user,
    )

    if (
        alcance is None
        or not alcance.institucional
    ):
        raise HTTPException(
            status_code=403,
            detail=(
                "Esta operaci?n requiere alcance "
                "administrativo institucional."
            ),
        )

    dia = db.query(DiaCerrado).filter(DiaCerrado.id == dia_id).first()
    if not dia:
        raise HTTPException(status_code=404, detail="Día cerrado no encontrado")
    registrar_evento_auditoria(db, current_user, "Reabrió un día previamente cerrado",
                                entidad="dia_cerrado", entidad_id=dia_id, detalle=dia.fecha)
    db.delete(dia)
    db.commit()
    return {"message": "Día reabierto correctamente. Las citas ya canceladas no se restauran automáticamente."}


# ══════════════════════════════════════
# HISTORIAL CON FILTROS — fix búsqueda
# ══════════════════════════════════════

@router.get("/historial")
def get_historial_admin(
    estudiante: str = None,
    fecha_inicio: str = None,
    fecha_fin: str = None,
    especialidad: str = None,
    estado: str = None,
    profesional_id: int = None,
    carrera: str = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(
        require_effective_permission(Permission.REPORTES_VER)
    ),
):
    alcance = obtener_alcance_administrativo_efectivo(
        db,
        current_user,
    )

    if alcance is None:
        raise HTTPException(
            status_code=403,
            detail="No tienes acceso al alcance solicitado.",
        )

    ids_profesionales = None

    if not alcance.institucional:
        ids_profesionales = _ids_profesionales_en_alcance(
            db,
            alcance,
        )

        if not ids_profesionales:
            return []

    query = (
        db.query(Cita)
        .join(
            Profesional,
            Cita.profesional_id == Profesional.id,
        )
        .join(
            Usuario,
            Cita.estudiante_id == Usuario.id,
        )
    )

    # El alcance se aplica antes de cualquier filtro solicitado.
    # Los filtros del request solo pueden reducir este conjunto.
    if ids_profesionales is not None:
        query = query.filter(
            Cita.profesional_id.in_(
                ids_profesionales
            )
        )

    if estudiante:
        q = f"%{estudiante}%"
        query = query.filter(
            (Usuario.nombre.ilike(q))
            | (Usuario.rut.ilike(q))
        )

    if fecha_inicio:
        query = query.filter(
            Cita.fecha >= fecha_inicio
        )

    if fecha_fin:
        query = query.filter(
            Cita.fecha <= fecha_fin
        )

    if especialidad:
        query = query.filter(
            Profesional.especialidad == especialidad
        )

    if estado:
        query = query.filter(
            Cita.estado == estado
        )

    if profesional_id:
        query = query.filter(
            Cita.profesional_id == profesional_id
        )

    if carrera:
        query = query.filter(
            Usuario.carrera.ilike(
                f"%{carrera}%"
            )
        )

    citas = (
        query
        .order_by(Cita.fecha.desc())
        .all()
    )

    result = []

    for c in citas:
        prof = (
            db.query(Profesional)
            .filter(
                Profesional.id == c.profesional_id
            )
            .first()
        )

        est = (
            db.query(Usuario)
            .filter(
                Usuario.id == c.estudiante_id
            )
            .first()
        )

        result.append({
            "id": c.id,
            "estudiante": (
                est.nombre
                if est
                else "?"
            ),
            "rut": (
                est.rut
                if est
                else "?"
            ),
            "carrera": (
                est.carrera
                if est
                else "?"
            ),
            "especialidad": (
                prof.especialidad
                if prof
                else "?"
            ),
            "profesional": (
                prof.nombre
                if prof
                else "?"
            ),
            "iniciales": (
                prof.iniciales
                if prof
                else "??"
            ),
            "fecha": c.fecha,
            "hora": c.hora,
            "estado": c.estado,
            "urgente": c.urgente or False,
            "tiene_pdf": c.estado == "completada",
        })

    return result


# ══════════════════════════════════════
# NOTIFICACIONES DEL ADMIN
# ══════════════════════════════════════

@router.get("/notificaciones")
def get_notificaciones_admin(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Devuelve ?nicamente las notificaciones de la cuenta
    autenticada que est? usando el dashboard administrativo.

    Es una operaci?n self-service: no requiere permisos para
    consultar o gestionar cuentas de terceros y no depende del
    alcance administrativo por especialidad.
    """
    usuario_id = current_user["id"]

    notifs = (
        db.query(Notificacion)
        .filter(
            Notificacion.usuario_id == usuario_id
        )
        .order_by(
            Notificacion.fecha_creacion.desc()
        )
        .limit(100)
        .all()
    )

    return [
        {
            "id": n.id,
            "mensaje": n.mensaje,
            "tipo": n.tipo,
            "leida": n.leida,
            "fecha_creacion": (
                n.fecha_creacion.isoformat()
                if n.fecha_creacion
                else None
            ),
        }
        for n in notifs
    ]


# ══════════════════════════════════════
# EXPORTACIÓN CGR
# ══════════════════════════════════════
# Solo se incluyen atenciones "completada" — un reporte de este tipo
# debe reflejar atenciones que realmente ocurrieron, no citas agendadas
# (pendiente) que todavía no se realizan.

@router.get("/exportar/cgr")
def exportar_cgr(anio: int, fecha_fin: str = None, db: Session = Depends(get_db), current_user: dict = Depends(require_permission(Permission.REPORTES_CGR_EXPORTAR))):
    query = db.query(Cita)\
        .join(Profesional, Cita.profesional_id == Profesional.id)\
        .join(Usuario, Cita.estudiante_id == Usuario.id)\
        .filter(Cita.fecha.like(f"{anio}%"), Cita.estado == "completada")
    if fecha_fin: query = query.filter(Cita.fecha <= fecha_fin)
    citas = query.order_by(Cita.fecha).all()
    headers = ["Nombre Completo","RUT","Tipo de Atención","Fecha","Hora","Medicamento Suministrado","Profesional que Atendió"]
    filas = ["\t".join(headers)]
    for c in citas:
        est  = db.query(Usuario).filter(Usuario.id == c.estudiante_id).first()
        prof = db.query(Profesional).filter(Profesional.id == c.profesional_id).first()
        rut_est = est.rut if est else ""
        if rut_est in RUTS_EXCLUIDOS_CGR: continue
        filas.append("\t".join([
            est.nombre if est else "—", rut_est or "—",
            prof.especialidad if prof else "—", c.fecha, c.hora,
            c.medicamento or "No aplica", prof.nombre if prof else "—"
        ]))
    contenido = "\n".join(filas)
    nombre_archivo = f"cgr_atenciones_{anio}" + (f"_hasta_{fecha_fin}" if fecha_fin else "") + ".xls"
    return StreamingResponse(io.BytesIO(contenido.encode("utf-8-sig")),
        media_type="text/tab-separated-values",
        headers={"Content-Disposition": f"attachment; filename={nombre_archivo}"})


@router.get("/exportar/alumnos")
def exportar_listado_alumnos(db: Session = Depends(get_db), current_user: dict = Depends(require_permission(Permission.REPORTES_CGR_EXPORTAR))):
    estudiantes = db.query(Usuario).filter(Usuario.rol == "estudiante").order_by(Usuario.nombre).all()
    filas = ["Nombre Completo\tRUT\tCarrera\tCorreo"]
    for e in estudiantes:
        if e.rut in RUTS_EXCLUIDOS_CGR: continue
        filas.append(f"{e.nombre or '—'}\t{e.rut or '—'}\t{e.carrera or '—'}\t{e.correo or '—'}")
    contenido = "\n".join(filas)
    return StreamingResponse(io.BytesIO(contenido.encode("utf-8-sig")),
        media_type="text/tab-separated-values",
        headers={"Content-Disposition": "attachment; filename=listado_alumnos.xls"})


# ══════════════════════════════════════
# AUDITORÍA — con fix de filtro por fecha
# ══════════════════════════════════════

@router.get("/auditoria")
def get_auditoria(fecha_inicio: str = None, fecha_fin: str = None, db: Session = Depends(get_db), current_user: dict = Depends(require_permission(Permission.AUDITORIA_VER))):
    query = db.query(Auditoria).order_by(Auditoria.fecha.desc())
    if fecha_inicio:
        query = query.filter(Auditoria.fecha >= datetime.strptime(fecha_inicio, "%Y-%m-%d"))
    if fecha_fin:
        query = query.filter(Auditoria.fecha <= datetime.strptime(fecha_fin, "%Y-%m-%d").replace(hour=23, minute=59, second=59))
    registros = query.limit(200).all()
    return [
        {"id": r.id, "usuario_id": r.usuario_id, "actor_rol": r.actor_rol,
         "accion": r.accion, "resultado": r.resultado, "detalle": r.detalle,
         "entidad": r.entidad, "entidad_id": r.entidad_id,
         "fecha": r.fecha.isoformat() if r.fecha else None}
        for r in registros
    ]


# SA-5: la auditoría es append-only a nivel de aplicación. No existen
# endpoints DELETE/PATCH/PUT para /admin/auditoria. Una eventual retención
# institucional futura deberá resolverse como proceso administrativo
# separado, fuera de esta API.


# ══════════════════════════════════════
# CONFIGURACIÓN
# ══════════════════════════════════════

@router.get("/configuracion", response_model=ConfiguracionOut)
def get_configuracion(db: Session = Depends(get_db), current_user: dict = Depends(require_permission(Permission.CONFIGURACION_GESTIONAR))):
    config = db.query(ConfiguracionSistema).first()
    if not config:
        # La tabla nunca se siembra en init_db.py — se crea con los
        # valores por defecto del modelo la primera vez que se consulta,
        # en vez de forzar al admin a chocar con un 404 en su primera visita.
        config = ConfiguracionSistema()
        db.add(config); db.commit(); db.refresh(config)
    return config


@router.patch("/configuracion", response_model=ConfiguracionOut)
def actualizar_configuracion(datos: ConfiguracionUpdate, db: Session = Depends(get_db), current_user: dict = Depends(require_permission(Permission.CONFIGURACION_GESTIONAR))):
    config = db.query(ConfiguracionSistema).first()
    if not config:
        config = ConfiguracionSistema()
        db.add(config); db.commit(); db.refresh(config)
    if datos.duracion_turno_min         is not None: config.duracion_turno_min         = datos.duracion_turno_min
    if datos.agendamiento_por_pacientes is not None: config.agendamiento_por_pacientes = datos.agendamiento_por_pacientes
    if datos.cancelacion_instantanea    is not None: config.cancelacion_instantanea    = datos.cancelacion_instantanea
    if datos.sobreturnos_habilitados    is not None: config.sobreturnos_habilitados    = datos.sobreturnos_habilitados
    if datos.cupos_por_turno            is not None: config.cupos_por_turno            = datos.cupos_por_turno
    db.commit(); db.refresh(config)
    return config