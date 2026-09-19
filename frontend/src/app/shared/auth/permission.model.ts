/**
 * SESAES — RBAC frontend: catálogo de permisos (Fase 3.1)
 *
 * IMPORTANTE — este mapa frontend NO es una barrera de seguridad.
 * El backend (backend/app/rbac/permissions.py) es la única autoridad
 * real: cada endpoint protegido valida el permiso server-side vía
 * require_permission. Este archivo solo existe para resolver UX en el
 * cliente (ej. ocultar un botón que igualmente sería rechazado con 403
 * si se llamara al backend), usando los DEFAULT permissions por rol
 * porque el JWT actual solo contiene 'rol', no la lista de permisos.
 *
 * Los 20 permission strings deben coincidir con backend/app/rbac/permissions.py.
 * ADMIN y SUPERADMIN no derivan sus permisos efectivos desde este mapa;
 * su contexto administrativo se carga desde el backend.
 *
 * Nota — autorización de recurso (no implementada todavía): los
 * permisos con sufijo '_asignada'/'_asignadas' son solo una capacidad
 * RBAC, no una comprobación de que el recurso concreto pertenezca al
 * profesional. Ver la misma nota en backend/app/rbac/permissions.py.
 */

import type { Role } from './role.model';

/**
 * Única fuente de verdad de los 20 permission strings (Fase 3.3).
 * `Permission` se deriva de este array para no mantener dos listas
 * manuales separadas, y `isPermission` permite validar en runtime un
 * valor arbitrario (ej. route.data.permission) contra este catálogo.
 */
export const PERMISSION_VALUES = [
  // Administración
  'usuarios.ver',
  'usuarios.gestionar',
  'profesionales.ver',
  'profesionales.gestionar',
  'agenda.ver',
  'agenda.gestionar',
  // A.4.3 (backend) / A.4.7A (frontend) — capacidad DISTINTA de
  // agenda.gestionar: poder gestionar una agenda (crear/mover/cancelar
  // citas normales) no implica poder forzar un sobrecupo sobre un
  // conflicto real (slot ya ocupado, colación, fuera de jornada). El
  // backend (backend/app/rbac/permissions.py) ya la incluye en el
  // catálogo y en ROLE_DEFAULT_PERMISSIONS[SUPERADMIN] desde A.4.3;
  // esta entrada estaba ausente acá, lo que hacía que
  // normalizarContextoAccesoAdministrativo() (auth.service.ts)
  // rechazara TODO el contexto administrativo de cualquier cuenta cuyo
  // `permisos` incluyera este string (ver hallazgo FE-A1).
  'agenda.sobrecupo',
  'configuracion.gestionar',
  'reportes.ver',
  'reportes.cgr.exportar',
  'auditoria.ver',
  'roles.gestionar',
  // Clínico / Profesional
  'atenciones.ver_asignadas',
  'atenciones.registrar',
  'ficha.ver_asignada',
  'ficha.editar_asignada',
  'agenda.ver_profesional',
  'agenda.gestionar_propia',
  // Estudiante / Autoservicio
  'citas.gestionar_propias',
  'perfil.ver_propio',
  'documentos.ver_propios',
] as const;

export type Permission = (typeof PERMISSION_VALUES)[number];

/**
 * Type guard en runtime para validar que un valor arbitrario (ej.
 * route.data['permission'], potencialmente manipulado) es realmente uno
 * de los 20 Permission conocidos. Fail-closed: cualquier valor fuera del
 * catálogo (incluyendo undefined, '', u otro tipo) → false.
 */
export function isPermission(value: unknown): value is Permission {
  return (
    typeof value === 'string' &&
    (PERMISSION_VALUES as readonly string[]).includes(value)
  );
}

/**
 * Espejo exacto de ROLE_DEFAULT_PERMISSIONS (backend/app/rbac/permissions.py).
 * Mínimo privilegio; sin wildcard. SUPERADMIN no recibe permisos
 * clínicos automáticos, igual que en el backend.
 */
export const ROLE_DEFAULT_PERMISSIONS: Readonly<Record<Role, readonly Permission[]>> = {
  // SA-10: ADMIN y SUPERADMIN fallan cerrado hasta recibir
  // su contexto administrativo efectivo desde el backend.
  superadmin: [],
  admin: [],
  profesional: [
    'atenciones.ver_asignadas',
    'atenciones.registrar',
    'ficha.ver_asignada',
    'ficha.editar_asignada',
    'agenda.ver_profesional',
    'agenda.gestionar_propia',
  ],
  estudiante: ['citas.gestionar_propias', 'perfil.ver_propio', 'documentos.ver_propios'],
};
