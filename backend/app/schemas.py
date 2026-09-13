from pydantic import BaseModel, Field, field_validator
from typing import Literal, Optional
from datetime import datetime
import re


# ══════════════════════════════════════
# CITAS
# ══════════════════════════════════════

class CitaCreate(BaseModel):
    estudiante_id:  int
    profesional_id: int
    fecha:          str            # YYYY-MM-DD
    hora:           str            # HH:MM 24h (lo que entrega /disponibilidad). También se acepta "HH:MM AM/PM" por compatibilidad — ver citas.py:_cita_a_datetime
    observaciones:  Optional[str] = None
    urgente:        Optional[bool] = False
    sobrecupo:      Optional[bool] = False   # solo admin puede marcarla; se ignora si la manda cualquier otro rol
    # A.4.1 — motivo HUMANO del sobrecupo (distinto de `observaciones`,
    # que es el motivo de consulta clínica). Opcional TEMPORALMENTE: el
    # frontend actual (dashboard-admin.ts) todavía no lo envía en el
    # body de POST /citas. Ver deuda documentada en
    # app.models.cita_sobrecupo.CitaSobrecupo.motivo — A.4.7 debe
    # actualizar el modal de Angular antes de evaluar si corresponde
    # exigirlo para sobrecupos nuevos.
    sobrecupo_motivo: Optional[str] = None


class CompletarCitaBody(BaseModel):
    """
    SESAES — SA-11.3B: body explícito de
    PATCH /profesional/{prof_id}/citas/{cita_id}/completar.

    Reemplaza el `body: dict` sin tipar que tenía el endpoint. Ambos
    campos son opcionales y aceptan `None` o campo omitido (mismo
    comportamiento que el `dict.get(...)` anterior); lo que cambia es
    que Pydantic ya no acepta objetos/listas/números como valor —
    solo `str` o `null`. No se definen longitudes máximas de negocio
    en este checkpoint (fuera de alcance de SA-11.3B).

    `medicamento` es el medicamento SUMINISTRADO durante la atención
    (no una receta/prescripción/indicación previa — ver
    app/rbac/clinical_capabilities.py). Si viene con contenido no
    vacío, completar_cita exige además la capability clínica
    ClinicalCapability.REGISTRAR_MEDICAMENTO_SUMINISTRADO (ver
    app/routers/profesionales.py).
    """
    medicamento:            Optional[str] = None
    observaciones_atencion: Optional[str] = None


# ══════════════════════════════════════
# ESTUDIANTE
# ══════════════════════════════════════

class EstudianteOut(BaseModel):
    id:          int
    correo:      str
    correo_secundario: Optional[str] = None
    rol:         str
    nombre:      Optional[str]  = None
    telefono:    Optional[str]  = None
    foto_url:    Optional[str]  = None
    tema_oscuro: Optional[bool] = False
    carrera:     Optional[str]  = None
    rut:         Optional[str]  = None

    class Config:
        from_attributes = True


class EstudianteUpdate(BaseModel):
    nombre:      Optional[str]  = None
    telefono:    Optional[str]  = None
    correo_secundario: Optional[str] = None
    foto_url:    Optional[str]  = None
    tema_oscuro: Optional[bool] = None
    carrera:     Optional[str]  = None
    rut:         Optional[str]  = None

    @field_validator("correo_secundario")
    @classmethod
    def validar_correo_secundario(cls, valor):
        if valor is None or valor == "":
            return valor
        patron = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
        if not re.match(patron, valor):
            raise ValueError("El correo secundario no tiene un formato válido")
        return valor


# ══════════════════════════════════════
# PROFESIONAL
# ══════════════════════════════════════

# Paleta cerrada de color_identificador. Debe coincidir exactamente con
# `coloresDisponibles` en admin-profesionales.ts (frontend). Único lugar de
# verdad: la lista y la función de chequeo viven aquí; el router
# (admin.py) es quien decide qué HTTPException lanzar si no pasa, para
# poder responder 400 (decisión de producto) en vez del 422 automático
# que generaría un field_validator de Pydantic.
COLORES_PERMITIDOS = frozenset({
    "#5E857D", "#7FB8A6", "#8B5CF6", "#A78BFA", "#4F8EF7",
    "#60A5FA", "#D9A441", "#E07A5F", "#D96C8A", "#C75B5B",
})


def color_identificador_es_valido(v: Optional[str]) -> bool:
    """None/ausente = sin color (válido). Cualquier otro valor debe estar
    exactamente en la paleta cerrada — rechaza nombres CSS ('red'),
    funciones ('url(...)', 'var(...)') o hex fuera de lista."""
    return v is None or v in COLORES_PERMITIDOS


class ProfesionalCreate(BaseModel):
    nombre:       str
    tratamiento:  Optional[str] = None  # "Dr.", "Dra." o None ("Sin prefijo") — únicas opciones que ofrece la interfaz actual
    especialidad: str
    iniciales:    Optional[str] = None
    descripcion:  Optional[str] = None
    duracion_min: Optional[int] = 45
    correo:       Optional[str] = None
    rut:          Optional[str] = None
    password:     Optional[str] = None  # None = el backend genera una contraseña temporal aleatoria
    color_identificador: Optional[str] = None  # hex de COLORES_PERMITIDOS, o null = sin color propio (usa fallback visual). Validado en el router (ver admin.py) para responder 400, no aquí.


class ProfesionalUpdate(BaseModel):
    nombre:       Optional[str] = None
    tratamiento:  Optional[str] = None
    especialidad: Optional[str] = None
    iniciales:    Optional[str] = None
    descripcion:  Optional[str] = None
    duracion_min: Optional[int] = None
    correo:       Optional[str] = None
    rut:          Optional[str] = None
    estado:       Optional[str] = None
    hora_almuerzo_inicio: Optional[str] = None 
    color_identificador: Optional[str] = None  # ídem: validado en el router, no acá.

class ProfesionalOut(BaseModel):
    id:           int
    nombre:       str
    tratamiento:  Optional[str] = None
    especialidad: str
    iniciales:    Optional[str] = None
    descripcion:  Optional[str] = None
    duracion_min: int
    estado:       Optional[str] = "activo"
    correo:       Optional[str] = None
    rut:          Optional[str] = None
    usuario_id:   Optional[int] = None
    hora_almuerzo_inicio: Optional[str] = None  
    hora_almuerzo_fin:    Optional[str] = None
    color_identificador: Optional[str] = None
    class Config:
        from_attributes = True


# ══════════════════════════════════════
# NOTIFICACIONES
# ══════════════════════════════════════

class NotificacionOut(BaseModel):
    id:             int
    usuario_id:     int
    mensaje:        str
    tipo:           str
    leida:          bool
    fecha_creacion: Optional[datetime] = None

    class Config:
        from_attributes = True


# ══════════════════════════════════════
# CONFIGURACIÓN DEL SISTEMA
# ══════════════════════════════════════

class ConfiguracionOut(BaseModel):
    id:                         int
    duracion_turno_min:         int
    agendamiento_por_pacientes: bool
    cancelacion_instantanea:    bool
    sobreturnos_habilitados:    bool
    cupos_por_turno:            int

    class Config:
        from_attributes = True


class ConfiguracionUpdate(BaseModel):
    duracion_turno_min:         Optional[int]  = None
    agendamiento_por_pacientes: Optional[bool] = None
    cancelacion_instantanea:    Optional[bool] = None
    sobreturnos_habilitados:    Optional[bool] = None
    cupos_por_turno:            Optional[int]  = None


# ══════════════════════════════════════
# AUDITORÍA
# ══════════════════════════════════════

class AuditoriaOut(BaseModel):
    id:         int
    usuario_id: Optional[int] = None
    actor_rol:  Optional[str] = None
    accion:     str
    resultado:  str = "exito"
    detalle:    Optional[str] = None
    entidad:    Optional[str] = None
    entidad_id: Optional[int] = None
    fecha:      Optional[datetime] = None

    class Config:
        from_attributes = True


# ══════════════════════════════════════
# CONFIGURACIÓN DEL CENTRO SESAES
# ══════════════════════════════════════

class ConfiguracionCentroOut(BaseModel):
    id:               int
    nombre_centro:    str
    direccion:        str
    telefono:         Optional[str] = None
    correo_contacto:  Optional[str] = None
    horario_atencion: str
    foto_admin_url:   Optional[str] = None
    nombre_admin:     str

    class Config:
        from_attributes = True


class ConfiguracionCentroUpdate(BaseModel):
    nombre_centro:    Optional[str] = None
    direccion:        Optional[str] = None
    telefono:         Optional[str] = None
    correo_contacto:  Optional[str] = None
    horario_atencion: Optional[str] = None
    foto_admin_url:   Optional[str] = None
    nombre_admin:     Optional[str] = None


# ══════════════════════════════════════
# USUARIO — MI PERFIL (SA-1.2)
# ══════════════════════════════════════
# Usuario es la única fuente de verdad de identidad personal para
# ADMIN/SUPERADMIN. Estos schemas respaldan GET/PATCH /usuarios/me,
# que operan exclusivamente sobre current_user (nunca sobre un
# usuario_id recibido del cliente).

class UsuarioMeOut(BaseModel):
    id:       int
    nombre:   Optional[str] = None
    correo:   str
    telefono: Optional[str] = None
    foto_url: Optional[str] = None
    rol:      str
    activo:   bool

    class Config:
        from_attributes = True


class AlcanceAdministrativoEfectivoOut(BaseModel):
    tipo: Literal["institucional", "especialidades"]
    especialidades: list[str]


class AccesoAdministrativoEfectivoOut(BaseModel):
    """
    Contexto administrativo efectivo de la sesi?n actual.

    Es informaci?n para UX; el backend sigue validando cada endpoint
    independientemente.
    """

    rol: Literal["admin", "superadmin"]
    perfil: Optional[
        Literal[
            "administrador_general",
            "administrador_especialidad",
            "secretaria_general",
            "secretaria_especialidad",
        ]
    ] = None
    permisos: list[str]
    alcance: AlcanceAdministrativoEfectivoOut


PerfilAdministrativoLiteral = Literal[
    "administrador_general",
    "administrador_especialidad",
    "secretaria_general",
    "secretaria_especialidad",
]

TipoAlcanceAdministrativoLiteral = Literal[
    "institucional",
    "especialidades",
]


class AlcanceAdministrativoGestionIn(BaseModel):
    """Alcance completo que SUPERADMIN asigna a una cuenta ADMIN."""

    model_config = {"extra": "forbid"}

    tipo: TipoAlcanceAdministrativoLiteral
    especialidades: list[str] = Field(default_factory=list)


class AccesoAdministrativoGestionUpdate(BaseModel):
    """
    Reemplazo completo de la configuración administrativa de una cuenta
    ADMIN.

    No acepta permisos reservados ni campos parciales. La capa de
    dominio valida perfil + alcance + permisos como una sola unidad.
    """

    model_config = {"extra": "forbid"}

    perfil: PerfilAdministrativoLiteral
    permisos: list[str] = Field(default_factory=list)
    alcance: AlcanceAdministrativoGestionIn


class AlcanceAdministrativoGestionOut(BaseModel):
    tipo: Optional[TipoAlcanceAdministrativoLiteral] = None
    especialidades: list[str] = Field(default_factory=list)


class AccesoAdministrativoGestionOut(BaseModel):
    usuario_id: int
    configurado: bool
    perfil: Optional[PerfilAdministrativoLiteral] = None
    permisos: list[str] = Field(default_factory=list)
    alcance: AlcanceAdministrativoGestionOut


class PerfilAccesoAdministrativoCatalogoOut(BaseModel):
    perfil: PerfilAdministrativoLiteral
    tipo_alcance: TipoAlcanceAdministrativoLiteral
    permisos_permitidos: list[str]


class CatalogoAccesoAdministrativoOut(BaseModel):
    perfiles: list[PerfilAccesoAdministrativoCatalogoOut]


class UsuarioMeUpdate(BaseModel):
    """
    Actualización de Mi Perfil (SA-1.2). Únicos campos editables por el
    propio usuario: nombre, foto_url, telefono.

    extra="forbid": cualquier campo no declarado aquí (rol, activo,
    permisos, password, debe_cambiar_password, usuario_id, correo) hace
    que Pydantic rechace la petición COMPLETA con 422, en vez de
    aceptarla e ignorar esos campos en silencio.
    """
    model_config = {"extra": "forbid"}

    nombre:   Optional[str] = None
    foto_url: Optional[str] = None
    telefono: Optional[str] = None


# ══════════════════════════════════════
# GOBERNANZA ADMIN/SUPERADMIN (SA-3)
# ══════════════════════════════════════
# Schemas de /usuarios/administradores. Deliberadamente separados de
# UsuarioMeOut/UsuarioMeUpdate (Mi Perfil): estos operan sobre un
# usuario_id de la URL/body, no sobre current_user, así que exigen sus
# propios contratos estrictos, distintos y no reutilizables entre sí.

class UsuarioAdministrativoOut(BaseModel):
    id:       int
    nombre:   Optional[str] = None
    correo:   str
    telefono: Optional[str] = None
    foto_url: Optional[str] = None
    rol:      str
    activo:   bool

    class Config:
        from_attributes = True


class UsuarioAdministrativoCreate(BaseModel):
    """
    Alta de una cuenta ADMIN o SUPERADMIN vía gobernanza (SA-3).

    extra="forbid": no se aceptan campos fuera de los declarados acá
    (en particular, no se acepta 'activo' ni 'debe_cambiar_password' —
    las cuentas nuevas siempre nacen activo=True,
    debe_cambiar_password=False, decisión fija del backend, no del
    caller).
    """
    model_config = {"extra": "forbid"}

    correo:   str
    password: str
    nombre:   Optional[str] = None
    telefono: Optional[str] = None
    rol:      Literal["admin", "superadmin"]


class UsuarioAdministrativoEstadoUpdate(BaseModel):
    """PATCH .../estado — activar/desactivar una cuenta admin/superadmin."""
    model_config = {"extra": "forbid"}

    activo: bool


class UsuarioAdministrativoRolUpdate(BaseModel):
    """PATCH .../rol — promover/degradar entre admin y superadmin."""
    model_config = {"extra": "forbid"}

    rol: Literal["admin", "superadmin"]


# ══════════════════════════════════════
# HISTORIAL DE ESTADOS DEL PROFESIONAL
# ══════════════════════════════════════

class HistorialEstadoOut(BaseModel):
    id:              int
    profesional_id:  int
    estado_anterior: Optional[str] = None
    estado_nuevo:    str
    motivo:          Optional[str] = None
    fecha:           Optional[datetime] = None
    registrado_por:  Optional[int] = None

    class Config:
        from_attributes = True