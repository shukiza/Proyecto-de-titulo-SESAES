"""
SESAES — RBAC (Fase 3.1): tests con unittest (stdlib).

pytest NO está declarado en requirements.txt (confirmado en la
auditoría de esta fase), así que estos tests usan exclusivamente
unittest de la biblioteca estándar.

Ejecutar desde backend/:
    python -m unittest discover -s tests -v
"""

import unittest

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.auth_dependencies import get_current_user
from app.database import Base
from app.models.usuario import Usuario
from app.rbac.dependencies import require_permission
from app.rbac.permissions import ROLE_DEFAULT_PERMISSIONS, Permission, has_permission
from app.rbac.roles import Role, normalizar_rol
from app.security import create_access_token, hash_password


class NormalizarRolTests(unittest.TestCase):
    def test_reconoce_roles_validos(self):
        self.assertEqual(normalizar_rol("estudiante"), Role.ESTUDIANTE)
        self.assertEqual(normalizar_rol("profesional"), Role.PROFESIONAL)
        self.assertEqual(normalizar_rol("admin"), Role.ADMIN)
        self.assertEqual(normalizar_rol("superadmin"), Role.SUPERADMIN)

    def test_acepta_role_ya_normalizado(self):
        self.assertEqual(normalizar_rol(Role.ADMIN), Role.ADMIN)

    def test_es_insensible_a_mayusculas_y_espacios(self):
        self.assertEqual(normalizar_rol("  Admin  "), Role.ADMIN)
        self.assertEqual(normalizar_rol("ESTUDIANTE"), Role.ESTUDIANTE)

    def test_null_devuelve_none(self):
        self.assertIsNone(normalizar_rol(None))

    def test_vacio_devuelve_none(self):
        self.assertIsNone(normalizar_rol(""))
        self.assertIsNone(normalizar_rol("   "))

    def test_desconocido_devuelve_none(self):
        self.assertIsNone(normalizar_rol("superusuario"))
        self.assertIsNone(normalizar_rol("root"))

    def test_tipo_invalido_devuelve_none(self):
        self.assertIsNone(normalizar_rol(123))
        self.assertIsNone(normalizar_rol(["admin"]))

    def test_nunca_hace_fallback_a_admin_o_superadmin(self):
        for valor_invalido in (None, "", "   ", "desconocido", 123):
            rol = normalizar_rol(valor_invalido)
            self.assertNotIn(rol, (Role.ADMIN, Role.SUPERADMIN))


class RoleDefaultPermissionsTests(unittest.TestCase):
    def test_los_cuatro_roles_estan_definidos(self):
        self.assertEqual(set(ROLE_DEFAULT_PERMISSIONS.keys()), set(Role))

    def test_catalogo_tiene_21_permisos(self):
        # A.4.3 — 20 (Fase 3.1) + Permission.AGENDA_SOBRECUPO, capacidad
        # distinta de AGENDA_GESTIONAR (ver sobrecupo_policy_service).
        self.assertEqual(len(list(Permission)), 21)

    def test_ningun_rol_tiene_permisos_duplicados_ni_desconocidos(self):
        for permisos in ROLE_DEFAULT_PERMISSIONS.values():
            for permiso in permisos:
                self.assertIsInstance(permiso, Permission)


class SuperadminSinWildcardTests(unittest.TestCase):
    """Casos obligatorios del checklist de Fase 3.1 para SUPERADMIN."""

    def test_superadmin_tiene_permisos_administrativos_explicitos(self):
        self.assertTrue(has_permission(Role.SUPERADMIN, Permission.USUARIOS_GESTIONAR))
        self.assertTrue(has_permission(Role.SUPERADMIN, Permission.ROLES_GESTIONAR))
        self.assertTrue(has_permission(Role.SUPERADMIN, Permission.AUDITORIA_VER))

    def test_superadmin_no_tiene_acceso_clinico_por_defecto(self):
        self.assertFalse(has_permission(Role.SUPERADMIN, Permission.FICHA_VER_ASIGNADA))
        self.assertFalse(has_permission(Role.SUPERADMIN, Permission.FICHA_EDITAR_ASIGNADA))
        self.assertFalse(has_permission(Role.SUPERADMIN, Permission.ATENCIONES_REGISTRAR))
        self.assertFalse(has_permission(Role.SUPERADMIN, Permission.ATENCIONES_VER_ASIGNADAS))

    def test_superadmin_no_tiene_wildcard_no_concede_permiso_inventado(self):
        # No existe ningún Permission "*"/"ALL"/"FULL_ACCESS" en el enum;
        # esta prueba confirma que ningún permiso REAL fuera de la lista
        # explícita queda concedido por accidente.
        permisos_superadmin = ROLE_DEFAULT_PERMISSIONS[Role.SUPERADMIN]
        for permiso in Permission:
            esperado = permiso in permisos_superadmin
            self.assertEqual(has_permission(Role.SUPERADMIN, permiso), esperado)


class RoleDefaultPermissionCasosObligatoriosTests(unittest.TestCase):
    """Casos obligatorios de la sección 8 del checklist de Fase 3.1."""

    def test_admin_no_tiene_roles_gestionar(self):
        self.assertFalse(has_permission(Role.ADMIN, Permission.ROLES_GESTIONAR))

    def test_admin_no_recibe_permisos_operativos_por_rol(self):
        self.assertEqual(
            ROLE_DEFAULT_PERMISSIONS[Role.ADMIN],
            frozenset(),
        )

        for permiso in Permission:
            self.assertFalse(
                has_permission(
                    Role.ADMIN,
                    permiso,
                )
            )

    def test_admin_no_tiene_configuracion_gestionar(self):
        self.assertFalse(has_permission(Role.ADMIN, Permission.CONFIGURACION_GESTIONAR))

    def test_admin_no_tiene_reportes_cgr_exportar(self):
        self.assertFalse(has_permission(Role.ADMIN, Permission.REPORTES_CGR_EXPORTAR))

    def test_admin_no_tiene_auditoria_ver(self):
        self.assertFalse(has_permission(Role.ADMIN, Permission.AUDITORIA_VER))

    def test_profesional_no_tiene_permisos_de_administracion(self):
        self.assertFalse(has_permission(Role.PROFESIONAL, Permission.USUARIOS_GESTIONAR))
        self.assertFalse(has_permission(Role.PROFESIONAL, Permission.ROLES_GESTIONAR))
        self.assertFalse(has_permission(Role.PROFESIONAL, Permission.CONFIGURACION_GESTIONAR))

    def test_profesional_tiene_solo_permisos_clinicos_necesarios(self):
        self.assertTrue(has_permission(Role.PROFESIONAL, Permission.FICHA_VER_ASIGNADA))
        self.assertTrue(has_permission(Role.PROFESIONAL, Permission.ATENCIONES_REGISTRAR))

    def test_estudiante_no_tiene_permisos_profesionales(self):
        self.assertFalse(has_permission(Role.ESTUDIANTE, Permission.ATENCIONES_REGISTRAR))
        self.assertFalse(has_permission(Role.ESTUDIANTE, Permission.FICHA_VER_ASIGNADA))
        self.assertFalse(has_permission(Role.ESTUDIANTE, Permission.AGENDA_GESTIONAR_PROPIA))

    def test_estudiante_tiene_solo_permisos_propios(self):
        self.assertTrue(has_permission(Role.ESTUDIANTE, Permission.CITAS_GESTIONAR_PROPIAS))
        self.assertTrue(has_permission(Role.ESTUDIANTE, Permission.PERFIL_VER_PROPIO))

    def test_rol_desconocido_devuelve_false(self):
        self.assertFalse(has_permission("superusuario", Permission.PERFIL_VER_PROPIO))

    def test_rol_null_devuelve_false(self):
        self.assertFalse(has_permission(None, Permission.PERFIL_VER_PROPIO))

    def test_rol_vacio_devuelve_false(self):
        self.assertFalse(has_permission("", Permission.PERFIL_VER_PROPIO))


class HasPermissionApiTests(unittest.TestCase):
    def test_acepta_dict_tipo_current_user(self):
        current_user = {"id": 1, "rol": "estudiante", "correo": "a@a.cl"}
        self.assertTrue(has_permission(current_user, Permission.CITAS_GESTIONAR_PROPIAS))
        self.assertFalse(has_permission(current_user, Permission.USUARIOS_GESTIONAR))

    def test_acepta_string_de_rol_crudo(self):
        # La API sigue aceptando el rol como string.
        self.assertTrue(
            has_permission(
                "superadmin",
                Permission.USUARIOS_GESTIONAR,
            )
        )

        # SA-9: que "admin" sea un rol valido ya no implica
        # permisos administrativos estaticos.
        self.assertFalse(
            has_permission(
                "admin",
                Permission.USUARIOS_GESTIONAR,
            )
        )

    def test_dict_sin_rol_devuelve_false(self):
        self.assertFalse(has_permission({"id": 1}, Permission.PERFIL_VER_PROPIO))


class RequirePermissionDependencyTests(unittest.TestCase):
    """
    Testea require_permission de forma aislada, sin levantar la app
    FastAPI completa (httpx/TestClient no están en requirements.txt).

    - Autorizado: llama la dependencia directamente con un current_user
      válido y confirma que lo retorna sin excepción.
    - 403: confirma que un current_user autenticado pero sin el permiso
      lanza HTTPException(403).
    - 401: confirma, por separado, que get_current_user (la dependencia
      de autenticación que require_permission reutiliza sin modificar)
      sigue lanzando 401 ante token ausente/inválido — esto es lo que
      preserva conceptualmente require_permission al construirse sobre
      Depends(get_current_user) en vez de reimplementar autenticación.
    """

    def test_autorizado_devuelve_current_user(self):
        dependencia = require_permission(
            Permission.USUARIOS_GESTIONAR
        )

        current_user = {
            "id": 1,
            "rol": "superadmin",
            "correo": "superadmin@sesaes.cl",
        }

        resultado = dependencia(
            current_user=current_user
        )

        self.assertEqual(
            resultado,
            current_user,
        )

    def test_sin_permiso_lanza_403(self):
        dependencia = require_permission(Permission.ROLES_GESTIONAR)
        current_user = {"id": 1, "rol": "admin", "correo": "admin@sesaes.cl"}

        with self.assertRaises(HTTPException) as ctx:
            dependencia(current_user=current_user)

        self.assertEqual(ctx.exception.status_code, 403)

    def test_rol_desconocido_en_current_user_lanza_403_no_500(self):
        dependencia = require_permission(Permission.PERFIL_VER_PROPIO)
        current_user = {"id": 1, "rol": "rol-inventado", "correo": "x@x.cl"}

        with self.assertRaises(HTTPException) as ctx:
            dependencia(current_user=current_user)

        self.assertEqual(ctx.exception.status_code, 403)

    def setUp(self):
        # SA-3: get_current_user ahora consulta Usuario en BD, así que
        # necesita una sesión real (aquí, SQLite en memoria, mismo patrón
        # que test_auditoria.py / test_usuarios_me.py) — nunca una
        # SessionLocal real de producción en un test unitario.
        self._engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=self._engine, tables=[Usuario.__table__])
        TestingSessionLocal = sessionmaker(bind=self._engine)
        self.db = TestingSessionLocal()

    def tearDown(self):
        self.db.close()
        self._engine.dispose()

    def test_get_current_user_sigue_lanzando_401_sin_token(self):
        # No se reemplaza ni se atrapa este 401: require_permission se
        # construye sobre Depends(get_current_user) sin modificarlo.
        # Se pasa `db` igualmente por completitud de la firma, pero
        # nunca se usa porque la función falla antes de consultarla.
        with self.assertRaises(HTTPException) as ctx:
            get_current_user(credentials=None, db=self.db)

        self.assertEqual(ctx.exception.status_code, 401)

    def test_get_current_user_lanza_401_con_token_invalido(self):
        credenciales_invalidas = HTTPAuthorizationCredentials(
            scheme="Bearer", credentials="token-basura-no-es-un-jwt"
        )

        with self.assertRaises(HTTPException) as ctx:
            get_current_user(credentials=credenciales_invalidas, db=self.db)

        self.assertEqual(ctx.exception.status_code, 401)

    def test_get_current_user_acepta_token_valido(self):
        usuario = Usuario(
            correo="p@sesaes.cl",
            password=hash_password("Password123!"),
            rol="profesional",
            nombre="Profesional de Prueba",
            activo=True,
            debe_cambiar_password=False,
        )
        self.db.add(usuario)
        self.db.commit()
        self.db.refresh(usuario)

        token = create_access_token({"id": usuario.id, "rol": "profesional", "correo": "p@sesaes.cl"})
        credenciales = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

        current_user = get_current_user(credentials=credenciales, db=self.db)

        self.assertEqual(current_user["id"], usuario.id)
        self.assertEqual(current_user["rol"], "profesional")

    def test_get_current_user_usa_rol_actual_de_bd_no_el_del_jwt(self):
        # SA-3: caso central del bug corregido. El JWT fue emitido con
        # rol=superadmin, pero la BD ya tiene a ese usuario como admin;
        # current_user["rol"] debe reflejar el rol ACTUAL de BD.
        usuario = Usuario(
            correo="cambio-rol@sesaes.cl",
            password=hash_password("Password123!"),
            rol="admin",
            activo=True,
            debe_cambiar_password=False,
        )
        self.db.add(usuario)
        self.db.commit()
        self.db.refresh(usuario)

        token_viejo = create_access_token({"id": usuario.id, "rol": "superadmin", "correo": usuario.correo})
        credenciales = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token_viejo)

        current_user = get_current_user(credentials=credenciales, db=self.db)

        self.assertEqual(current_user["rol"], "admin")

    def test_get_current_user_rechaza_cuenta_desactivada(self):
        usuario = Usuario(
            correo="desactivado@sesaes.cl",
            password=hash_password("Password123!"),
            rol="admin",
            activo=False,
            debe_cambiar_password=False,
        )
        self.db.add(usuario)
        self.db.commit()
        self.db.refresh(usuario)

        token = create_access_token({"id": usuario.id, "rol": "admin", "correo": usuario.correo})
        credenciales = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

        with self.assertRaises(HTTPException) as ctx:
            get_current_user(credentials=credenciales, db=self.db)

        self.assertEqual(ctx.exception.status_code, 401)

    def test_get_current_user_rechaza_usuario_inexistente(self):
        token = create_access_token({"id": 999999, "rol": "admin", "correo": "fantasma@sesaes.cl"})
        credenciales = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

        with self.assertRaises(HTTPException) as ctx:
            get_current_user(credentials=credenciales, db=self.db)

        self.assertEqual(ctx.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main()
