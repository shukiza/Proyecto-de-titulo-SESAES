from app.models.usuario import Usuario
from app.models.profesional import Profesional
from app.models.horario import HorarioDisponible
from app.models.cita import Cita
from app.models.cita_sobrecupo import CitaSobrecupo, CitaSobrecupoConflicto  # noqa: F401 — A.4.1
from app.models.notificacion import Notificacion
from app.models.configuracion import ConfiguracionSistema
from app.models.auditoria import Auditoria
from app.models.configuracion_centro import ConfiguracionCentro
from app.models.historial_estado_profesional import HistorialEstadoProfesional
from app.models.correo_log import CorreoLog
from app.models.dia_cerrado import DiaCerrado
from app.models.especialidad_capability import EspecialidadCapability  # noqa: F401 — SA-11.2