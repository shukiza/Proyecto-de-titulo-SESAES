"""Bootstrap comun de modelos SQLAlchemy para la suite de tests.

Los tests deben poder ejecutarse de forma aislada y en cualquier orden.
Importar explicitamente los modulos registra todas las clases relacionadas
en el registry de SQLAlchemy antes de que configure_mappers() sea necesario.
"""

import app.models.acceso_administrativo
import app.models.auditoria
import app.models.cita
import app.models.cita_sobrecupo
import app.models.configuracion
import app.models.correo_log
import app.models.dia_cerrado
import app.models.historial_estado_profesional
import app.models.historial_paciente
import app.models.historial_plantilla_pregunta
import app.models.horario
import app.models.notificacion
import app.models.profesional
import app.models.solicitud_horario
import app.models.usuario
