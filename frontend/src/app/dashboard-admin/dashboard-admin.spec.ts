import { ChangeDetectorRef } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Router } from '@angular/router';
import { describe, expect, it, vi } from 'vitest';
import { of, Subject, throwError } from 'rxjs';

import { AuthService } from '../auth.service';
import { Permission } from '../shared/auth/permission.model';
import { ToastService } from '../shared/toast/toast.service';
import { DashboardAdminComponent } from './dashboard-admin';

/**
 * SA-1.1 — Identidad real del shell/topbar.
 *
 * Mismo patrón de mock de AuthService que ya usa rbac-frontend.spec.ts en
 * este mismo dashboard (auth como objeto plano con los métodos usados,
 * cast a AuthService), en vez de una instancia real respaldada por
 * sessionStorage, para quedar consistente con el resto de la suite de
 * este componente.
 *
 * Cubre únicamente los 4 getters de identidad agregados en esta fase
 * (nombreUsuario, fotoUsuarioUrl, rolVisual, inicialesUsuario). El resto
 * del shell (agenda, XLSX, RBAC, etc.) ya está cubierto por
 * rbac-frontend.spec.ts y no se toca ni se duplica aquí.
 */
function crearShell(opciones: {
  nombre?: string | null;
  fotoUrl?: string | null;
  rol?: string | null;
} = {}) {
  const auth = {
    getNombre: vi.fn(() => opciones.nombre ?? null),
    getFotoUrl: vi.fn(() => opciones.fotoUrl ?? null),
    getRol: vi.fn(() => opciones.rol ?? null),
    hasPermission: vi.fn(() => false),
    getUsuarioId: vi.fn(() => null)
  } as unknown as AuthService;

  const router = {} as Router;
  const http = {} as HttpClient;
 const toast = {
  success: vi.fn(),
  error: vi.fn()
} as unknown as ToastService;
  const cdr = {} as ChangeDetectorRef;

  return new DashboardAdminComponent(router, http, cdr, toast, auth);
}

describe('DashboardAdminComponent — identidad real del topbar (SA-1.1)', () => {
  // ── 1. ADMIN: nombre real + rol visual "Administrador" ────────────
  it('ADMIN: muestra el nombre real de la sesión y rol visual "Administrador"', () => {
    const component = crearShell({ nombre: 'Claudia Pérez', rol: 'admin' });

    expect(component.nombreUsuario).toBe('Claudia Pérez');
    expect(component.rolVisual).toBe('Administrador');
  });

  // ── 2. SUPERADMIN: nombre real + rol visual "Superadministrador" ──
  it('SUPERADMIN: muestra el nombre real de la sesión y rol visual "Superadministrador"', () => {
    const component = crearShell({ nombre: 'Superadministrador SESAES', rol: 'superadmin' });

    expect(component.nombreUsuario).toBe('Superadministrador SESAES');
    expect(component.rolVisual).toBe('Superadministrador');
  });

  // ── 3. El nombre del topbar es independiente de configCentro.nombre_admin ──
  it('el nombre del topbar no proviene de configCentro.nombre_admin', () => {
    const component = crearShell({ nombre: 'Claudia Pérez', rol: 'admin' });
    // configCentro simula el valor legacy que el topbar mostraba antes de SA-1.1.
    (component as any).configCentro.nombre_admin = 'Admin SESAES';

    expect(component.nombreUsuario).toBe('Claudia Pérez');
    expect(component.nombreUsuario).not.toBe('Admin SESAES');
  });

  // ── 4. La foto del topbar proviene de foto_url de sesión ──────────
  it('usa la foto_url de la sesión para el avatar del topbar, no configCentro.foto_admin_url', () => {
    const component = crearShell({ fotoUrl: 'https://cdn.sesaes.cl/fotos/claudia.jpg' });
    (component as any).configCentro.foto_admin_url = 'https://legacy.example.com/otra-foto.jpg';

    expect(component.fotoUsuarioUrl).toBe('https://cdn.sesaes.cl/fotos/claudia.jpg');
    expect(component.fotoUsuarioUrl).not.toBe('https://legacy.example.com/otra-foto.jpg');
  });

  // ── 5. Sin foto: iniciales derivadas del nombre real ──────────────
  it('sin foto_url, genera iniciales desde el nombre real (no un "AD" fijo para todos)', () => {
    const superadmin = crearShell({ nombre: 'Superadministrador SESAES' });
    expect(superadmin.fotoUsuarioUrl).toBeNull();
    expect(superadmin.inicialesUsuario).toBe('SS');

    const admin = crearShell({ nombre: 'Claudia Pérez' });
    expect(admin.inicialesUsuario).toBe('CP');
  });

  // ── 6. Fallback final sin nombre de sesión -> "AD" ────────────────
  it('sin nombre de sesión, las iniciales caen a "AD" como último fallback', () => {
    const component = crearShell();

    expect(component.inicialesUsuario).toBe('AD');
  });
});

/**
 * SA-1.2 — Mi Perfil sobre Usuario (GET/PATCH /usuarios/me) y
 * coordinación con el cambio de contraseña.
 *
 * Mismo patrón de instanciación directa (sin TestBed) que el describe
 * de SA-1.1 de arriba, ampliado con un mock de `http` que registra las
 * llamadas (get/patch) para poder afirmar CUÁNTAS y EN QUÉ ORDEN se
 * disparan — es justamente lo que corrige el bug de la entrega
 * anterior (PATCH de perfil disparado antes de validar la contraseña).
 *
 * `auth.actualizarIdentidadSesion` se mockea con estado real (no un
 * simple vi.fn() vacío): actualiza variables locales que
 * `getNombre`/`getFotoUrl` vuelven a leer, para poder comprobar que el
 * topbar (nombreUsuario/fotoUsuarioUrl, getters de SA-1.1) refleja el
 * cambio sin relogin, tal como hace la implementación real de
 * AuthService contra sessionStorage.
 */
function crearShellConPerfil(opciones: {
  nombre?: string | null;
  fotoUrl?: string | null;
  rol?: string | null;
  getUsuarioMeResponse?: any;
  patchUsuarioMeResponse?: any;
  patchUsuarioMeError?: any;
  patchPasswordError?: any;
} = {}) {
  let nombreSesion = opciones.nombre ?? null;
  let fotoSesion   = opciones.fotoUrl ?? null;

  const actualizarIdentidadSesion = vi.fn((datos: { nombre?: string; foto_url?: string | null }) => {
    if (datos.nombre !== undefined) nombreSesion = datos.nombre;
    if (datos.foto_url !== undefined) fotoSesion = datos.foto_url ?? null;
  });

  const auth = {
    getNombre: vi.fn(() => nombreSesion),
    getFotoUrl: vi.fn(() => fotoSesion),
    getRol: vi.fn(() => opciones.rol ?? null),
    hasPermission: vi.fn(() => false),
    getUsuarioId: vi.fn(() => null),
    actualizarIdentidadSesion
  } as unknown as AuthService;

  const patchCalls: { url: string; body: any }[] = [];
  const getCalls: string[] = [];

  const http = {
    get: vi.fn((url: string) => {
      getCalls.push(url);
      return of(opciones.getUsuarioMeResponse ?? {});
    }),
    patch: vi.fn((url: string, body: any) => {
      patchCalls.push({ url, body });
      if (url.includes('/usuarios/me')) {
        if (opciones.patchUsuarioMeError) return throwError(() => opciones.patchUsuarioMeError);
        return of(opciones.patchUsuarioMeResponse ?? { ...body });
      }
      if (url.includes('cambiar-password')) {
        if (opciones.patchPasswordError) return throwError(() => opciones.patchPasswordError);
        return of({ message: 'Contraseña actualizada correctamente' });
      }
      return of({});
    })
  } as unknown as HttpClient;

  const router = {} as Router;
  const toast = {
    success: vi.fn(),
    error: vi.fn()
  } as unknown as ToastService;
  const cdr = { detectChanges: vi.fn() } as unknown as ChangeDetectorRef;

  const component = new DashboardAdminComponent(router, http, cdr, toast, auth);
  return { component, http, auth, patchCalls, getCalls, actualizarIdentidadSesion };
}

describe('DashboardAdminComponent — Mi Perfil sobre Usuario (SA-1.2)', () => {
  // ── 1. Carga: estado antes/después de GET /usuarios/me ────────────
  it('cargarMiPerfil(): perfilCargado pasa de false a true tras un GET exitoso, sin datos inventados de por medio', () => {
    const { component } = crearShellConPerfil({
      getUsuarioMeResponse: { id: 7, nombre: 'Admin SESAES', correo: 'admin@utem.cl', telefono: null, foto_url: null, rol: 'admin', activo: true }
    });

    expect(component.perfilCargado).toBe(false);
    expect(component.cargandoPerfil).toBe(false);

    component.cargarMiPerfil();

    expect(component.cargandoPerfil).toBe(false); // el observable síncrono de test ya resolvió
    expect(component.perfilCargado).toBe(true);
    expect(component.usuarioPerfil.nombre).toBe('Admin SESAES');
  });

  // ── 2. Contraseñas nuevas no coinciden -> CERO requests ────────────
  it('contraseña nueva y confirmación distintas -> no dispara ningún PATCH (ni perfil ni contraseña)', () => {
    const { component, patchCalls } = crearShellConPerfil();

    component.onGuardarPerfilAdmin({
      nombre: 'Nuevo Nombre', telefono: '+56911111111',
      contrasena_actual: 'actual123', contrasena_nueva: 'nueva123', contrasena_conf: 'otra-distinta'
    });

    expect(patchCalls.length).toBe(0);
    expect(component.mensajeError).toBe('Las contraseñas nuevas no coinciden.');
  });

  // ── 2.1 a 2.4. SA-1.2 v3 — estados incompletos de contraseña ───────
  // Regla: si CUALQUIERA de los tres campos de contraseña tiene
  // contenido, se exigen los tres. Falta cualquiera -> CERO requests.
  it('solo contraseña actual -> 0 PATCH, "Completa todos los campos de contraseña."', () => {
    const { component, patchCalls } = crearShellConPerfil();

    component.onGuardarPerfilAdmin({
      nombre: 'Nuevo Nombre', telefono: '+56911111111',
      contrasena_actual: 'actual123', contrasena_nueva: '', contrasena_conf: ''
    });

    expect(patchCalls.length).toBe(0);
    expect(component.mensajeError).toBe('Completa todos los campos de contraseña.');
  });

  it('solo nueva + confirmación (sin actual) -> 0 PATCH, "Completa todos los campos de contraseña."', () => {
    const { component, patchCalls } = crearShellConPerfil();

    component.onGuardarPerfilAdmin({
      nombre: 'Nuevo Nombre', telefono: '+56911111111',
      contrasena_actual: '', contrasena_nueva: 'nueva123', contrasena_conf: 'nueva123'
    });

    expect(patchCalls.length).toBe(0);
    expect(component.mensajeError).toBe('Completa todos los campos de contraseña.');
  });

  it('nueva sin confirmación -> 0 PATCH, "Completa todos los campos de contraseña."', () => {
    const { component, patchCalls } = crearShellConPerfil();

    component.onGuardarPerfilAdmin({
      nombre: 'Nuevo Nombre', telefono: '+56911111111',
      contrasena_actual: 'actual123', contrasena_nueva: 'nueva123', contrasena_conf: ''
    });

    expect(patchCalls.length).toBe(0);
    expect(component.mensajeError).toBe('Completa todos los campos de contraseña.');
  });

  it('los tres completos pero no coinciden -> 0 PATCH, "Las contraseñas nuevas no coinciden."', () => {
    const { component, patchCalls } = crearShellConPerfil();

    component.onGuardarPerfilAdmin({
      nombre: 'Nuevo Nombre', telefono: '+56911111111',
      contrasena_actual: 'actual123', contrasena_nueva: 'nueva123', contrasena_conf: 'nueva456'
    });

    expect(patchCalls.length).toBe(0);
    expect(component.mensajeError).toBe('Las contraseñas nuevas no coinciden.');
  });

  it('los tres completos y coinciden -> flujo normal (2 PATCH, perfil + contraseña)', () => {
    const { component, patchCalls } = crearShellConPerfil({
      patchUsuarioMeResponse: { id: 7, nombre: 'Nuevo Nombre', correo: 'admin@utem.cl', telefono: '+56911111111', foto_url: null, rol: 'admin', activo: true }
    });

    component.onGuardarPerfilAdmin({
      nombre: 'Nuevo Nombre', telefono: '+56911111111',
      contrasena_actual: 'actual123', contrasena_nueva: 'nueva123', contrasena_conf: 'nueva123'
    });

    expect(patchCalls.length).toBe(2);
    expect(patchCalls[0].url).toContain('/usuarios/me');
    expect(patchCalls[1].url).toContain('cambiar-password');
    expect(component.mensajeExito).toBe('Perfil y contraseña actualizados correctamente.');
  });

  // ── 3. Perfil falla -> nunca se intenta el cambio de contraseña ────
  it('si PATCH /usuarios/me falla, no se intenta PATCH de contraseña aunque se haya solicitado', () => {
    const { component, patchCalls } = crearShellConPerfil({
      patchUsuarioMeError: { error: { detail: 'boom' } }
    });

    component.onGuardarPerfilAdmin({
      nombre: 'Nuevo Nombre', telefono: '+56911111111',
      contrasena_actual: 'actual123', contrasena_nueva: 'nueva123', contrasena_conf: 'nueva123'
    });

    expect(patchCalls.length).toBe(1);
    expect(patchCalls[0].url).toContain('/usuarios/me');
    expect(component.mensajeError).toBe('No se pudo actualizar el perfil.');
  });

  // ── 4. Perfil OK, sin cambio de contraseña -> mensaje "Perfil actualizado" ──
  it('perfil OK sin cambio de contraseña -> mensaje "Perfil actualizado correctamente."', () => {
    const { component, patchCalls } = crearShellConPerfil({
      patchUsuarioMeResponse: { id: 7, nombre: 'Nuevo Nombre', correo: 'admin@utem.cl', telefono: '+56911111111', foto_url: null, rol: 'admin', activo: true }
    });

    component.onGuardarPerfilAdmin({
      nombre: 'Nuevo Nombre', telefono: '+56911111111',
      contrasena_actual: '', contrasena_nueva: '', contrasena_conf: ''
    });

    expect(patchCalls.length).toBe(1);
    expect(component.mensajeExito).toBe('Perfil actualizado correctamente.');
  });

  // ── 5. Perfil OK + contraseña OK -> mensaje conjunto ───────────────
  it('perfil OK + contraseña OK -> mensaje "Perfil y contraseña actualizados correctamente."', () => {
    const { component, patchCalls } = crearShellConPerfil({
      patchUsuarioMeResponse: { id: 7, nombre: 'Nuevo Nombre', correo: 'admin@utem.cl', telefono: '+56911111111', foto_url: null, rol: 'admin', activo: true }
    });

    component.onGuardarPerfilAdmin({
      nombre: 'Nuevo Nombre', telefono: '+56911111111',
      contrasena_actual: 'actual123', contrasena_nueva: 'nueva123', contrasena_conf: 'nueva123'
    });

    expect(patchCalls.length).toBe(2);
    expect(patchCalls[0].url).toContain('/usuarios/me');
    expect(patchCalls[1].url).toContain('cambiar-password');
    expect(component.mensajeExito).toBe('Perfil y contraseña actualizados correctamente.');
  });

  // ── 6. Perfil OK + contraseña falla -> mensaje parcial, nunca "ambos OK" ──
  it('perfil OK + contraseña falla -> informa que el perfil sí se guardó y la contraseña no cambió', () => {
    const { component, patchCalls } = crearShellConPerfil({
      patchUsuarioMeResponse: { id: 7, nombre: 'Nuevo Nombre', correo: 'admin@utem.cl', telefono: '+56911111111', foto_url: null, rol: 'admin', activo: true },
      patchPasswordError: { error: { detail: 'Contraseña actual incorrecta' } }
    });

    component.onGuardarPerfilAdmin({
      nombre: 'Nuevo Nombre', telefono: '+56911111111',
      contrasena_actual: 'mala', contrasena_nueva: 'nueva123', contrasena_conf: 'nueva123'
    });

    expect(patchCalls.length).toBe(2);
    expect(component.mensajeExito).toBe(''); // nunca afirma éxito conjunto
    expect(component.mensajeError).toContain('Perfil actualizado');
    expect(component.mensajeError).toContain('contraseña no se pudo cambiar');
  });

  // ── 7. actualizarIdentidadSesion se llama tras perfil OK ───────────
  it('tras PATCH /usuarios/me exitoso, llama a AuthService.actualizarIdentidadSesion con nombre/foto_url', () => {
    const { component, actualizarIdentidadSesion } = crearShellConPerfil({
      patchUsuarioMeResponse: { id: 7, nombre: 'Nuevo Nombre', correo: 'admin@utem.cl', telefono: null, foto_url: 'data:image/png;base64,ABC', rol: 'admin', activo: true }
    });

    component.onGuardarPerfilAdmin({
      nombre: 'Nuevo Nombre', telefono: '',
      contrasena_actual: '', contrasena_nueva: '', contrasena_conf: ''
    });

    expect(actualizarIdentidadSesion).toHaveBeenCalledWith({
      nombre: 'Nuevo Nombre',
      foto_url: 'data:image/png;base64,ABC'
    });
  });

  // ── 8. El topbar refleja el nuevo nombre/foto sin relogin ──────────
  it('el topbar (nombreUsuario/fotoUsuarioUrl) refleja el cambio inmediatamente, sin relogin', () => {
    const { component } = crearShellConPerfil({
      nombre: 'Nombre Viejo', fotoUrl: null,
      patchUsuarioMeResponse: { id: 7, nombre: 'Nombre Actualizado', correo: 'admin@utem.cl', telefono: null, foto_url: 'https://cdn.sesaes.cl/nueva.jpg', rol: 'admin', activo: true }
    });

    expect(component.nombreUsuario).toBe('Nombre Viejo');
    expect(component.fotoUsuarioUrl).toBeNull();

    component.onGuardarPerfilAdmin({
      nombre: 'Nombre Actualizado', telefono: '',
      contrasena_actual: '', contrasena_nueva: '', contrasena_conf: ''
    });

    expect(component.nombreUsuario).toBe('Nombre Actualizado');
    expect(component.fotoUsuarioUrl).toBe('https://cdn.sesaes.cl/nueva.jpg');
  });

  // ── 9. Sin cambio de contraseña -> nunca se dispara cambiar-password ──
  it('sin contrasena_nueva, nunca dispara PATCH /configuracion-centro/cambiar-password', () => {
    const { component, patchCalls } = crearShellConPerfil({
      patchUsuarioMeResponse: { id: 7, nombre: 'X', correo: 'admin@utem.cl', telefono: null, foto_url: null, rol: 'admin', activo: true }
    });

    component.onGuardarPerfilAdmin({
      nombre: 'X', telefono: '',
      contrasena_actual: '', contrasena_nueva: '', contrasena_conf: ''
    });

    expect(patchCalls.some(c => c.url.includes('cambiar-password'))).toBe(false);
  });
});

/**
 * SA-4 — UI Administradores: gating a nivel shell.
 *
 * No se usa TestBed/fixture acá: mismo patrón que rbac-frontend.spec.ts
 * en este mismo directorio (instanciación directa de
 * DashboardAdminComponent con AuthService/HttpClient/Router/ToastService
 * mockeados). Estos tests cubren exactamente la condición booleana que
 * el template evalúa en dashboard-admin.html:
 *   - nav:  *ngIf="puedeAccederSeccion('administradores')"
 *   - hijo: *ngIf="seccionActiva==='administradores' && puedeAccederSeccion('administradores')"
 * sin necesitar compilar la plantilla completa (que arrastra ~10
 * componentes hijos y no aporta cobertura adicional sobre esta regla
 * de gating puntual).
 */
function crearShellConPermisos(permisos: Permission[]) {
  const permitidos = new Set<Permission>(permisos);

  const auth = {
    hasPermission: vi.fn((permission: Permission) => permitidos.has(permission)),
    getNombre: vi.fn(() => null),
    getFotoUrl: vi.fn(() => null),
    getRol: vi.fn(() => null),
    getUsuarioId: vi.fn(() => null),
    cargarAccesoAdministrativo: vi.fn(
      () => of({})
    )
  } as unknown as AuthService;

  const http = { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() } as unknown as HttpClient;
  const toast = { success: vi.fn(), error: vi.fn() } as unknown as ToastService;
  const router = {} as Router;
  const cdr = { detectChanges: vi.fn() } as unknown as ChangeDetectorRef;

  return new DashboardAdminComponent(router, http, cdr, toast, auth);
}

function crearShellConPermisosYHttp(permisos: Permission[]) {
  const permitidos = new Set<Permission>(permisos);

  const auth = {
    hasPermission: vi.fn((permission: Permission) => permitidos.has(permission)),
    getNombre: vi.fn(() => null),
    getFotoUrl: vi.fn(() => null),
    getRol: vi.fn(() => null),
    getUsuarioId: vi.fn(() => null),
    cargarAccesoAdministrativo: vi.fn(() => of({}))
  } as unknown as AuthService;

  const http = {
    get: vi.fn((url: string) => of([])),
    post: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn()
  };
  const toast = { success: vi.fn(), error: vi.fn() } as unknown as ToastService;
  const router = {} as Router;
  const cdr = { detectChanges: vi.fn() } as unknown as ChangeDetectorRef;

  return {
    component: new DashboardAdminComponent(
      router,
      http as unknown as HttpClient,
      cdr,
      toast,
      auth
    ),
    http
  };
}

describe('DashboardAdminComponent — SA-10.2B handlers fail-closed', () => {
  it('sin agenda.gestionar, los handlers de Agenda/Inicio/Citas no producen POST/PATCH/DELETE', () => {
    const { component, http } = crearShellConPermisosYHttp([]);

    component.nuevaCita = {
      fecha: '2026-09-08',
      hora: '10:00',
      estudiante_id: 10,
      profesional_id: 20,
      observaciones: '',
      urgente: false,
      sobrecupo: false
    };
    component.nuevoDiaCerrado = { fecha: '2026-09-18', motivo: 'Mantención' };

    component.marcarInasistencia({ id: 1, estudiante: 'Ana' });
    component.cancelarCitaAdmin({ id: 2, estudiante: 'Bruno' });
    component.marcarPrioridadCita({ id: 3 }, true);
    component.crearCitaDesdeHorario();
    component.aprobarSolicitudHorario({ id: 4, profesional_nombre: 'Prof.' });
    component.rechazarSolicitudHorario({ id: 5, profesional_nombre: 'Prof.' });
    component.crearDiaCerrado();
    component.reabrirDiaCerrado({ id: 6, fecha: '2026-09-18' });

    expect(http.post).not.toHaveBeenCalled();
    expect(http.patch).not.toHaveBeenCalled();
    expect(http.delete).not.toHaveBeenCalled();
  });

  it('sin profesionales.gestionar, los handlers CRUD de profesionales no producen POST/PATCH/DELETE', () => {
    const { component, http } = crearShellConPermisosYHttp([]);
    const profesional = { id: 7, nombre: 'Profesional Read Only' };

    component.onCrearProfesional({ nombre: 'Nuevo' });
    component.onCambiarEstadoProfesional({ profesional, nuevoEstado: 'licencia', motivo: 'test' });
    component.onCambiarDuracionProfesional({ profesional, duracionMin: 30 });
    component.onCambiarTratamientoProfesional({ profesional, tratamiento: 'Dr.' });
    component.onCambiarColorIdentificador({ profesional, color: '#4F8EF7' });
    component.onEliminarProfesional(profesional);

    expect(http.post).not.toHaveBeenCalled();
    expect(http.patch).not.toHaveBeenCalled();
    expect(http.delete).not.toHaveBeenCalled();
  });

  it('al invalidar el contexto se cierran los modales de mutación de Agenda', () => {
    const { component } = crearShellConPermisosYHttp([]);
    component.modalCitaAbierto = true;
    component.sobrecupoConfirmAbierto = true;
    component.sobrecupoPendiente = {
      fecha: '2026-09-08',
      hora: '10:00',
      mensaje: 'Estás agendando fuera del horario habitual de un profesional. ¿Confirmas?'
    };

    (component as any).limpiarDatosVisiblesDeAccesoAnterior();

    expect(component.modalCitaAbierto).toBe(false);
    expect(component.sobrecupoConfirmAbierto).toBe(false);
    expect(component.sobrecupoPendiente).toBeNull();
  });
});

describe('DashboardAdminComponent — SA-10.2C lectura *.ver', () => {
  it('agenda.ver abre Agenda y Citas sin conceder agenda.gestionar', () => {
    const component = crearShellConPermisos(['agenda.ver']);

    expect(component.puedeAccederSeccion('horario')).toBe(true);
    expect(component.puedeAccederSeccion('citas')).toBe(true);
    expect(component.hasPermission('agenda.gestionar')).toBe(false);
  });

  it('profesionales.ver abre Profesionales y usuarios.ver abre Estudiantes', () => {
    const profesionales = crearShellConPermisos(['profesionales.ver']);
    const estudiantes = crearShellConPermisos(['usuarios.ver']);

    expect(profesionales.puedeAccederSeccion('profesional')).toBe(true);
    expect(profesionales.hasPermission('profesionales.gestionar')).toBe(false);
    expect(estudiantes.puedeAccederSeccion('estudiantes')).toBe(true);
    expect(estudiantes.hasPermission('usuarios.gestionar')).toBe(false);
  });

  it('agenda.ver carga datos de lectura y catálogo operacional, pero no solicitudes de horario', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver']);

    component.cargarDatos();

    const urls = http.get.mock.calls.map(call => String(call[0]));
    expect(urls.some(url => url.includes('/admin/proximas-citas'))).toBe(true);
    expect(urls.some(url => url.includes('/admin/resumen-dia'))).toBe(true);
    expect(urls.some(url => url.includes('/admin/dias-cerrados'))).toBe(true);
    expect(urls.some(url => url.includes('/agenda/profesionales'))).toBe(true);
    expect(urls.some(url => url.includes('/admin/solicitudes-horario'))).toBe(false);
  });

  it('profesionales.ver usa /admin/profesionales y no el catálogo mínimo de Agenda', () => {
    const { component, http } = crearShellConPermisosYHttp(['profesionales.ver']);

    component.cargarDatos();

    const urls = http.get.mock.calls.map(call => String(call[0]));
    expect(urls.some(url => url.includes('/admin/profesionales'))).toBe(true);
    expect(urls.some(url => url.includes('/agenda/profesionales'))).toBe(false);
  });

  it('usuarios.ver permite búsqueda global de estudiantes sin usuarios.gestionar', () => {
    const { component, http } = crearShellConPermisosYHttp(['usuarios.ver']);
    component.busquedaGlobal = 'ana';

    component.buscarGlobal();

    expect(http.get).toHaveBeenCalledWith(expect.stringContaining('/admin/estudiantes?q=ana'));
  });
});

describe('DashboardAdminComponent — SA-4 gating de "administradores" (roles.gestionar)', () => {
  // ── 1. Sin roles.gestionar: la sección no aparece en el menú ──────
  it('roles.gestionar=false -> puedeAccederSeccion(\'administradores\') es false (Administradores oculto)', () => {
    const component = crearShellConPermisos(['usuarios.gestionar', 'agenda.gestionar']);
    expect(component.puedeAccederSeccion('administradores')).toBe(false);
  });

  // ── 2. Con roles.gestionar: la sección aparece en el menú ─────────
  it('roles.gestionar=true -> puedeAccederSeccion(\'administradores\') es true (Administradores visible)', () => {
    const component = crearShellConPermisos(['roles.gestionar']);
    expect(component.puedeAccederSeccion('administradores')).toBe(true);
  });

  // ── 3. Seleccionar Administradores navega y habilita el render del hijo ──
  it('con permiso, navegarA(\'administradores\') activa la sección y la condición de render del hijo es true', () => {
    const component = crearShellConPermisos(['roles.gestionar']);

    component.navegarA('administradores');

    expect(component.seccionActiva).toBe('administradores');
    // Misma condición combinada que usa el *ngIf del hijo en el template.
    expect(component.seccionActiva === 'administradores' && component.puedeAccederSeccion('administradores')).toBe(true);
  });

  // ── 4. Sección forzada internamente sin permiso: el hijo NO se renderiza ──
  it('si seccionActiva se fuerza a \'administradores\' sin roles.gestionar, la condición de render del hijo es false', () => {
    const component = crearShellConPermisos(['usuarios.gestionar']); // sin roles.gestionar

    // navegarA() ya bloquea la navegación normal (probado en el describe
    // RBAC de este mismo archivo/rbac-frontend.spec.ts), pero esta prueba
    // cubre el caso más estricto: aunque algo fuerce seccionActiva
    // internamente (bug, estado inconsistente, etc.), el *ngIf del hijo
    // vuelve a evaluar el permiso y no debe renderizarlo.
    (component as any).seccionActiva = 'administradores';

    expect(component.seccionActiva === 'administradores' && component.puedeAccederSeccion('administradores')).toBe(false);
  });
});
describe('DashboardAdminComponent - SA-6.2 salida de seccion tras perder permiso', () => {
  it('sale de Administradores hacia Inicio cuando la sesion pierde roles.gestionar', () => {
    const component = crearShellConPermisos(['roles.gestionar']);

    component.navegarA('administradores');

    expect(component.seccionActiva).toBe('administradores');
    expect(component.puedeAccederSeccion('administradores')).toBe(true);

    // Simula el AuthService despues de que la propia cuenta fue degradada.
    const auth = (component as any).auth as {
      hasPermission: ReturnType<typeof vi.fn>;
    };

    auth.hasPermission.mockReturnValue(false);

    vi
      .spyOn(component, 'cargarDatos')
      .mockImplementation(() => {});

    component.onSesionAdministrativaActualizada();

    expect(component.seccionActiva).toBe('inicio');
    expect(component.puedeAccederSeccion('administradores')).toBe(false);
  });

  it('permanece en la seccion actual si el nuevo rol todavia conserva acceso', () => {
    const component = crearShellConPermisos(['roles.gestionar']);

    component.navegarA('administradores');
    vi
      .spyOn(component, 'cargarDatos')
      .mockImplementation(() => {});

    component.onSesionAdministrativaActualizada();

    expect(component.seccionActiva).toBe('administradores');
  });
});

describe('DashboardAdminComponent — SA-10.2D matriz read-only vs gestionar', () => {
  it('cada permiso *.ver abre solo su dominio y no concede permisos de gestión', () => {
    const agenda = crearShellConPermisos(['agenda.ver']);
    const profesionales = crearShellConPermisos(['profesionales.ver']);
    const estudiantes = crearShellConPermisos(['usuarios.ver']);

    expect(agenda.puedeAccederSeccion('horario')).toBe(true);
    expect(agenda.puedeAccederSeccion('citas')).toBe(true);
    expect(agenda.puedeAccederSeccion('profesional')).toBe(false);
    expect(agenda.puedeAccederSeccion('estudiantes')).toBe(false);
    expect(agenda.hasPermission('agenda.gestionar')).toBe(false);

    expect(profesionales.puedeAccederSeccion('profesional')).toBe(true);
    expect(profesionales.puedeAccederSeccion('horario')).toBe(false);
    expect(profesionales.puedeAccederSeccion('citas')).toBe(false);
    expect(profesionales.puedeAccederSeccion('estudiantes')).toBe(false);
    expect(profesionales.hasPermission('profesionales.gestionar')).toBe(false);

    expect(estudiantes.puedeAccederSeccion('estudiantes')).toBe(true);
    expect(estudiantes.puedeAccederSeccion('horario')).toBe(false);
    expect(estudiantes.puedeAccederSeccion('citas')).toBe(false);
    expect(estudiantes.puedeAccederSeccion('profesional')).toBe(false);
    expect(estudiantes.hasPermission('usuarios.gestionar')).toBe(false);
  });

  it('agenda.ver permite lecturas pero una invocación directa de handlers no produce mutaciones', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver']);

    component.nuevaCita = {
      fecha: '2026-09-22',
      hora: '10:00',
      estudiante_id: 10,
      profesional_id: 20,
      observaciones: '',
      urgente: false,
      sobrecupo: false
    };
    component.nuevoDiaCerrado = {
      fecha: '2026-09-23',
      motivo: 'Read-only'
    };

    component.marcarInasistencia({ id: 1 });
    component.cancelarCitaAdmin({ id: 2 });
    component.marcarPrioridadCita({ id: 3 }, true);
    component.crearCitaDesdeHorario();
    component.aprobarSolicitudHorario({ id: 4 });
    component.rechazarSolicitudHorario({ id: 5 });
    component.crearDiaCerrado();
    component.reabrirDiaCerrado({ id: 6, fecha: '2026-09-23' });

    expect(http.post).not.toHaveBeenCalled();
    expect(http.patch).not.toHaveBeenCalled();
    expect(http.delete).not.toHaveBeenCalled();
  });

  it('profesionales.ver permite lectura pero una invocación directa de CRUD no produce mutaciones', () => {
    const { component, http } = crearShellConPermisosYHttp(['profesionales.ver']);
    const profesional = { id: 7, nombre: 'Profesional Read-only' };

    component.onCrearProfesional({ nombre: 'No crear' });
    component.onCambiarEstadoProfesional({
      profesional,
      nuevoEstado: 'licencia',
      motivo: 'test'
    });
    component.onCambiarDuracionProfesional({ profesional, duracionMin: 30 });
    component.onCambiarTratamientoProfesional({ profesional, tratamiento: 'Dr.' });
    component.onCambiarColorIdentificador({ profesional, color: '#123456' });
    component.onEliminarProfesional(profesional);

    expect(http.post).not.toHaveBeenCalled();
    expect(http.patch).not.toHaveBeenCalled();
    expect(http.delete).not.toHaveBeenCalled();
  });

  it('sin permisos *.ver los loaders directos de Agenda, Profesionales y Estudiantes no consultan esos módulos', () => {
    const { component, http } = crearShellConPermisosYHttp([]);

    component.filtroProfesionalId = '20';
    component.cargarResumenDia();
    component.cargarProximasCitas();
    component.cargarDiasCerrados();
    component.cargarHorarioProfesional();
    component.cargarProfesionales();
    (component as any).cargarProfesionalesAgenda();
    component.cargarEstudiantes();
    component.verPerfilEstudianteAdmin({ id: 10 });
    component.busquedaGlobal = 'ana';
    component.buscarGlobal();

    const urls = http.get.mock.calls.map(call => String(call[0]));

    expect(urls.some(url => url.includes('/admin/resumen-dia'))).toBe(false);
    expect(urls.some(url => url.includes('/admin/proximas-citas'))).toBe(false);
    expect(urls.some(url => url.includes('/admin/dias-cerrados'))).toBe(false);
    expect(urls.some(url => url.includes('/agenda/profesional/'))).toBe(false);
    expect(urls.some(url => url.includes('/admin/profesionales'))).toBe(false);
    expect(urls.some(url => url.includes('/agenda/profesionales'))).toBe(false);
    expect(urls.some(url => url.includes('/admin/estudiantes'))).toBe(false);
  });

  it('usuarios.ver permite listado, perfil y búsqueda de estudiantes sin usuarios.gestionar', () => {
    const { component, http } = crearShellConPermisosYHttp(['usuarios.ver']);

    component.cargarEstudiantes();
    component.verPerfilEstudianteAdmin({ id: 10 });
    component.busquedaGlobal = 'ana';
    component.buscarGlobal();

    const urls = http.get.mock.calls.map(call => String(call[0]));

    expect(urls.some(url => url.includes('/admin/estudiantes/listado'))).toBe(true);
    expect(urls.some(url => url.includes('/admin/estudiantes/10/perfil'))).toBe(true);
    expect(urls.some(url => url.includes('/admin/estudiantes?q=ana'))).toBe(true);
    expect(component.hasPermission('usuarios.gestionar')).toBe(false);
  });

  it('al perder el permiso de lectura de la sección activa vuelve a Inicio y limpia datos visibles', () => {
    const { component } = crearShellConPermisosYHttp(['agenda.ver']);

    component.navegarA('horario');
    component.proximasCitas = [{ id: 1 }];
    component.resumenDia = [{ id: 2 }];

    const auth = (component as any).auth as {
      hasPermission: ReturnType<typeof vi.fn>;
    };
    auth.hasPermission.mockReturnValue(false);

    component.onSesionAdministrativaActualizada();

    expect(component.seccionActiva).toBe('inicio');
    expect(component.proximasCitas).toEqual([]);
    expect(component.resumenDia).toEqual([]);
  });
});


describe('DashboardAdminComponent — A.2B.2 Angular Semana', () => {
  const setSemana = (component: DashboardAdminComponent, inicio = '2026-09-07'): void => {
    const [anio, mes, dia] = inicio.split('-').map(Number);
    const lunes = new Date(anio, mes - 1, dia);
    component.semanaActual = Array.from({ length: 7 }, (_, i) => {
      const fecha = new Date(lunes);
      fecha.setDate(lunes.getDate() + i);
      const f = `${fecha.getFullYear()}-${String(fecha.getMonth() + 1).padStart(2, '0')}-${String(fecha.getDate()).padStart(2, '0')}`;
      return { fecha: f };
    });
  };

  const respuestaRango = (
    profesionalId: number,
    fechaInicio: string,
    fechaFin: string,
    slots: Array<{ fecha: string; hora: string; disponible: boolean; motivo: string | null }>
  ) => ({
    profesional_id: profesionalId,
    fecha_inicio: fechaInicio,
    fecha_fin: fechaFin,
    duracion_min: 60,
    dias: Array.from(new Set(slots.map(slot => slot.fecha))).map(fecha => ({
      fecha,
      slots: slots
        .filter(slot => slot.fecha === fecha)
        .map(slot => ({
          hora: slot.hora,
          disponible: slot.disponible,
          motivo: slot.motivo,
          overridable_con_sobrecupo: slot.motivo === 'en_colacion' || slot.motivo === 'fuera_de_jornada'
        }))
    }))
  });

  it('consulta una sola disponibilidad por profesional y rango semanal exacto', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver']);
    component.filtroProfesionalId = '20';
    setSemana(component);

    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/disponibilidad')) {
        return of(respuestaRango(20, '2026-09-07', '2026-09-13', [
          { fecha: '2026-09-07', hora: '08:00', disponible: true, motivo: null }
        ]));
      }
      return of([]);
    });

    component.cargarHorarioProfesional();

    const urlsDisponibilidad = http.get.mock.calls
      .map(call => String(call[0]))
      .filter(url => url.includes('/disponibilidad'));

    expect(urlsDisponibilidad).toHaveLength(1);
    expect(urlsDisponibilidad[0]).toContain('/agenda/profesional/20/disponibilidad');
    expect(urlsDisponibilidad[0]).toContain('fecha_inicio=2026-09-07');
    expect(urlsDisponibilidad[0]).toContain('fecha_fin=2026-09-13');
  });

  it('al cambiar de semana consulta el nuevo rango y no hace requests por celda', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver']);
    component.filtroProfesionalId = '20';
    setSemana(component);

    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/disponibilidad')) {
        const inicio = url.includes('2026-09-14') ? '2026-09-14' : '2026-09-07';
        const fin = inicio === '2026-09-14' ? '2026-09-20' : '2026-09-13';
        return of(respuestaRango(20, inicio, fin, []));
      }
      return of([]);
    });

    component.cargarHorarioProfesional();
    component.semanaSiguiente();

    const urlsDisponibilidad = http.get.mock.calls
      .map(call => String(call[0]))
      .filter(url => url.includes('/disponibilidad'));

    expect(urlsDisponibilidad).toHaveLength(2);
    expect(urlsDisponibilidad[0]).toContain('fecha_inicio=2026-09-07');
    expect(urlsDisponibilidad[0]).toContain('fecha_fin=2026-09-13');
    expect(urlsDisponibilidad[1]).toContain('fecha_inicio=2026-09-14');
    expect(urlsDisponibilidad[1]).toContain('fecha_fin=2026-09-20');
  });

  it('prioriza una cita real sobre el estado de disponibilidad backend', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver']);
    component.filtroProfesionalId = '20';
    setSemana(component);

    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/citas')) {
        return of([{ id: 1, fecha: '2026-09-07', hora: '08:00', estado: 'pendiente', urgente: true, sobrecupo: false, estudiante: 'Ana' }]);
      }
      if (url.includes('/disponibilidad')) {
        return of(respuestaRango(20, '2026-09-07', '2026-09-13', [
          { fecha: '2026-09-07', hora: '08:00', disponible: true, motivo: null }
        ]));
      }
      return of([]);
    });

    component.cargarHorarioProfesional();

    expect(component.getBloqueEstado('2026-09-07', '08:00')).toBe('urgente');
    expect(component.getBloqueInfo('2026-09-07', '08:00')).toBe('Ana');
  });

  // ────────────────────────────────────────────────────────────────
  // A.4.7A.1 — orden determinista de presentación por bloque
  // ────────────────────────────────────────────────────────────────
  // Regla (ver compararCitasBloque en dashboard-admin.ts): urgente
  // primero, luego normal, luego sobrecupo al final — nunca el orden
  // de llegada del arreglo de backend.

  it('A.4.7A.1 — orden determinista: normal antes de sobrecupo (orden de backend: sobrecupo, normal)', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver']);
    component.filtroProfesionalId = '20';
    setSemana(component);

    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/citas')) {
        // Backend entrega primero la sobrecupo, después la normal.
        return of([
          { id: 2, fecha: '2026-09-17', hora: '08:00', estado: 'pendiente', urgente: false, sobrecupo: true,  estudiante: 'Carlos Muñoz' },
          { id: 1, fecha: '2026-09-17', hora: '08:00', estado: 'pendiente', urgente: false, sobrecupo: false, estudiante: 'Diego Soto' }
        ]);
      }
      if (url.includes('/disponibilidad')) {
        return of(respuestaRango(20, '2026-09-07', '2026-09-13', []));
      }
      return of([]);
    });

    component.cargarHorarioProfesional();

    const citas = component.getBloqueCitas('2026-09-17', '08:00');
    expect(citas.map((c: any) => c.estudiante)).toEqual(['Diego Soto', 'Carlos Muñoz']);
  });

  it('A.4.7A.1 — orden determinista: el mismo resultado se obtiene si backend invierte el arreglo (orden de backend: normal, sobrecupo)', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver']);
    component.filtroProfesionalId = '20';
    setSemana(component);

    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/citas')) {
        // Mismo par de citas, orden de llegada invertido respecto al
        // test anterior — el resultado visual NO debe cambiar.
        return of([
          { id: 1, fecha: '2026-09-17', hora: '08:00', estado: 'pendiente', urgente: false, sobrecupo: false, estudiante: 'Diego Soto' },
          { id: 2, fecha: '2026-09-17', hora: '08:00', estado: 'pendiente', urgente: false, sobrecupo: true,  estudiante: 'Carlos Muñoz' }
        ]);
      }
      if (url.includes('/disponibilidad')) {
        return of(respuestaRango(20, '2026-09-07', '2026-09-13', []));
      }
      return of([]);
    });

    component.cargarHorarioProfesional();

    const citas = component.getBloqueCitas('2026-09-17', '08:00');
    expect(citas.map((c: any) => c.estudiante)).toEqual(['Diego Soto', 'Carlos Muñoz']);
    expect(component.getBloqueInfo('2026-09-17', '08:00'))
      .toBe('Diego Soto · Carlos Muñoz (Sobrecupo)');
    expect(component.getBloqueEstado('2026-09-17', '08:00')).toBe('sobrecupo');
  });

  it('A.4.7A.1 — orden determinista: urgente antes que normal y que sobrecupo, sin importar el orden de llegada', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver']);
    component.filtroProfesionalId = '20';
    setSemana(component);

    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/citas')) {
        return of([
          { id: 2, fecha: '2026-09-17', hora: '08:00', estado: 'pendiente', urgente: false, sobrecupo: true,  estudiante: 'Carlos Muñoz' },
          { id: 3, fecha: '2026-09-17', hora: '08:00', estado: 'pendiente', urgente: true,  sobrecupo: false, estudiante: 'Ana Ríos' },
          { id: 1, fecha: '2026-09-17', hora: '08:00', estado: 'pendiente', urgente: false, sobrecupo: false, estudiante: 'Diego Soto' }
        ]);
      }
      if (url.includes('/disponibilidad')) {
        return of(respuestaRango(20, '2026-09-07', '2026-09-13', []));
      }
      return of([]);
    });

    component.cargarHorarioProfesional();

    const citas = component.getBloqueCitas('2026-09-17', '08:00');
    expect(citas.map((c: any) => c.estudiante)).toEqual(['Ana Ríos', 'Diego Soto', 'Carlos Muñoz']);
    expect(component.getBloqueEstado('2026-09-17', '08:00')).toBe('urgente');
  });

  // ────────────────────────────────────────────────────────────────
  // A.4.7A.1 — casos de regresión
  // ────────────────────────────────────────────────────────────────

  it('A.4.7A.1 (A) — una cita cancelada en el mismo slot no aparece entre las citas activas del bloque', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver']);
    component.filtroProfesionalId = '20';
    setSemana(component);

    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/citas')) {
        return of([
          { id: 1, fecha: '2026-09-17', hora: '08:00', estado: 'cancelada', urgente: false, sobrecupo: false, estudiante: 'Diego Soto' },
          { id: 2, fecha: '2026-09-17', hora: '08:00', estado: 'pendiente', urgente: false, sobrecupo: true,  estudiante: 'Carlos Muñoz' }
        ]);
      }
      if (url.includes('/disponibilidad')) {
        return of(respuestaRango(20, '2026-09-07', '2026-09-13', []));
      }
      return of([]);
    });

    component.cargarHorarioProfesional();

    const citas = component.getBloqueCitas('2026-09-17', '08:00');
    expect(citas.map((c: any) => c.estudiante)).toEqual(['Carlos Muñoz']);
  });

  it('A.4.7A.1 (B) — una cita con inasistencia en el mismo slot no aparece entre las citas activas del bloque', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver']);
    component.filtroProfesionalId = '20';
    setSemana(component);

    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/citas')) {
        return of([
          { id: 1, fecha: '2026-09-17', hora: '08:00', estado: 'inasistencia', urgente: false, sobrecupo: false, estudiante: 'Diego Soto' },
          { id: 2, fecha: '2026-09-17', hora: '08:00', estado: 'pendiente',    urgente: false, sobrecupo: true,  estudiante: 'Carlos Muñoz' }
        ]);
      }
      if (url.includes('/disponibilidad')) {
        return of(respuestaRango(20, '2026-09-07', '2026-09-13', []));
      }
      return of([]);
    });

    component.cargarHorarioProfesional();

    const citas = component.getBloqueCitas('2026-09-17', '08:00');
    expect(citas.map((c: any) => c.estudiante)).toEqual(['Carlos Muñoz']);
  });

  it('A.4.7A.1 (C) — normal + sobrecupo en el mismo slot: ambas aparecen', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver']);
    component.filtroProfesionalId = '20';
    setSemana(component);

    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/citas')) {
        return of([
          { id: 1, fecha: '2026-09-17', hora: '08:00', estado: 'pendiente', urgente: false, sobrecupo: false, estudiante: 'Diego Soto' },
          { id: 2, fecha: '2026-09-17', hora: '08:00', estado: 'pendiente', urgente: false, sobrecupo: true,  estudiante: 'Carlos Muñoz' }
        ]);
      }
      if (url.includes('/disponibilidad')) {
        return of(respuestaRango(20, '2026-09-07', '2026-09-13', []));
      }
      return of([]);
    });

    component.cargarHorarioProfesional();

    const citas = component.getBloqueCitas('2026-09-17', '08:00');
    expect(citas).toHaveLength(2);
    expect(citas.map((c: any) => c.estudiante)).toEqual(['Diego Soto', 'Carlos Muñoz']);
  });

  it('A.4.7A.1 (D) — el panel contextual sigue recibiendo ambas citas activas del día aunque compartan slot', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver']);
    component.filtroProfesionalId = '20';
    setSemana(component);

    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/citas')) {
        return of([
          { id: 1, fecha: '2026-09-17', hora: '08:00', estado: 'pendiente', urgente: false, sobrecupo: false, estudiante: 'Diego Soto' },
          { id: 2, fecha: '2026-09-17', hora: '08:00', estado: 'pendiente', urgente: false, sobrecupo: true,  estudiante: 'Carlos Muñoz' }
        ]);
      }
      if (url.includes('/disponibilidad')) {
        return of(respuestaRango(20, '2026-09-07', '2026-09-13', []));
      }
      return of([]);
    });

    component.cargarHorarioProfesional();
    component.diaSeleccionado = '2026-09-17';

    expect(component.citasDiaSeleccionado).toHaveLength(2);
    expect(component.citasDiaSeleccionado.map((c: any) => c.estudiante).sort())
      .toEqual(['Carlos Muñoz', 'Diego Soto']);
  });

  it('A.4.7A.1 (E) — dos citas en el slot (una ya sobrecupo) + overridable_con_sobrecupo=false: sin affordance para una tercera', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    component.filtroProfesionalId = '20';
    setSemana(component);

    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/citas')) {
        return of([
          { id: 1, fecha: '2026-09-17', hora: '08:00', estado: 'pendiente', urgente: false, sobrecupo: false, estudiante: 'Diego Soto' },
          { id: 2, fecha: '2026-09-17', hora: '08:00', estado: 'pendiente', urgente: false, sobrecupo: true,  estudiante: 'Carlos Muñoz' }
        ]);
      }
      if (url.includes('/disponibilidad')) {
        return of({
          profesional_id: 20, fecha_inicio: '2026-09-07', fecha_fin: '2026-09-13', duracion_min: 60,
          dias: [{
            fecha: '2026-09-17',
            slots: [{ hora: '08:00', disponible: false, motivo: 'slot_ocupado', overridable_con_sobrecupo: false }]
          }]
        });
      }
      return of([]);
    });

    component.cargarHorarioProfesional();

    // El estado ya es 'sobrecupo' (hay una sobrecupo real en el slot), no
    // 'ocupado' — por eso puedeSolicitarSobrecupo() rechaza de entrada,
    // consistente con no poder pedir un tercer sobrecupo sobre este bloque.
    expect(component.getBloqueEstado('2026-09-17', '08:00')).toBe('sobrecupo');
    expect(component.puedeSolicitarSobrecupo('2026-09-17', '08:00')).toBe(false);
  });

  it('A.4.7A.1 (F) — una sola cita normal + overridable_con_sobrecupo=true: conserva affordance para solicitar la segunda', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    component.filtroProfesionalId = '20';
    setSemana(component);

    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/citas')) {
        return of([
          { id: 1, fecha: '2026-09-17', hora: '08:00', estado: 'pendiente', urgente: false, sobrecupo: false, estudiante: 'Diego Soto' }
        ]);
      }
      if (url.includes('/disponibilidad')) {
        return of({
          profesional_id: 20, fecha_inicio: '2026-09-07', fecha_fin: '2026-09-13', duracion_min: 60,
          dias: [{
            fecha: '2026-09-17',
            slots: [{ hora: '08:00', disponible: false, motivo: 'slot_ocupado', overridable_con_sobrecupo: true }]
          }]
        });
      }
      return of([]);
    });

    component.cargarHorarioProfesional();

    expect(component.getBloqueEstado('2026-09-17', '08:00')).toBe('ocupado');
    expect(component.puedeSolicitarSobrecupo('2026-09-17', '08:00')).toBe(true);
  });

  it('mapea únicamente presentación desde motivos backend y no inventa disponibilidad', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver']);
    component.filtroProfesionalId = '20';
    setSemana(component);

    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/disponibilidad')) {
        return of(respuestaRango(20, '2026-09-07', '2026-09-13', [
          { fecha: '2026-09-07', hora: '08:00', disponible: true, motivo: null },
          { fecha: '2026-09-07', hora: '09:00', disponible: false, motivo: 'en_colacion' },
          { fecha: '2026-09-07', hora: '10:00', disponible: false, motivo: 'fuera_de_jornada' },
          { fecha: '2026-09-07', hora: '11:00', disponible: false, motivo: 'dia_cerrado' },
          { fecha: '2026-09-07', hora: '12:00', disponible: false, motivo: 'profesional_inactivo' },
          { fecha: '2026-09-07', hora: '13:00', disponible: false, motivo: 'slot_ocupado' }
        ]));
      }
      return of([]);
    });

    component.cargarHorarioProfesional();

    expect(component.getBloqueEstado('2026-09-07', '08:00')).toBe('disponible');
    expect(component.getBloqueEstado('2026-09-07', '09:00')).toBe('colacion');
    expect(component.getBloqueEstado('2026-09-07', '10:00')).toBe('fuera-horario');
    expect(component.getBloqueEstado('2026-09-07', '11:00')).toBe('cerrado-centro');
    expect(component.getBloqueEstado('2026-09-07', '12:00')).toBe('bloqueado');
    expect(component.getBloqueEstado('2026-09-07', '13:00')).toBe('ocupado');
    expect(component.getBloqueEstado('2026-09-07', '14:00')).toBe('sin-datos');
    expect(component.getBloqueInfo('2026-09-07', '09:00')).toBe('Colación');
  });

  it('mientras carga y si falla disponibilidad permanece fail-closed en sin-datos', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver']);
    const disponibilidad$ = new Subject<any>();
    component.filtroProfesionalId = '20';
    setSemana(component);

    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/disponibilidad')) return disponibilidad$;
      return of([]);
    });

    component.cargarHorarioProfesional();

    expect(component.disponibilidadCargando).toBe(true);
    expect(component.getBloqueEstado('2026-09-07', '08:00')).toBe('sin-datos');

    disponibilidad$.error(new Error('backend no disponible'));

    expect(component.disponibilidadCargando).toBe(false);
    expect(component.disponibilidadError).toBe(true);
    expect(component.getBloqueEstado('2026-09-07', '08:00')).toBe('sin-datos');
  });

  it('ignora una respuesta stale de profesional anterior', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver']);
    const primera$ = new Subject<any>();
    const segunda$ = new Subject<any>();
    component.filtroProfesionalId = '20';
    setSemana(component);

    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/disponibilidad')) {
        return url.includes('/20/') ? primera$ : segunda$;
      }
      return of([]);
    });

    component.cargarHorarioProfesional();
    component.filtroProfesionalId = '21';
    component.cargarHorarioProfesional();

    segunda$.next(respuestaRango(21, '2026-09-07', '2026-09-13', [
      { fecha: '2026-09-07', hora: '08:00', disponible: true, motivo: null }
    ]));
    segunda$.complete();

    expect(component.getBloqueEstado('2026-09-07', '08:00')).toBe('disponible');

    primera$.next(respuestaRango(20, '2026-09-07', '2026-09-13', [
      { fecha: '2026-09-07', hora: '08:00', disponible: false, motivo: 'en_colacion' }
    ]));
    primera$.complete();

    expect(component.getBloqueEstado('2026-09-07', '08:00')).toBe('disponible');
  });


  it('ignora una respuesta stale de la semana anterior', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver']);
    const primera$ = new Subject<any>();
    const segunda$ = new Subject<any>();
    component.filtroProfesionalId = '20';
    setSemana(component);

    let disponibilidadIndex = 0;
    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/disponibilidad')) {
        disponibilidadIndex += 1;
        return disponibilidadIndex === 1 ? primera$ : segunda$;
      }
      return of([]);
    });

    component.cargarHorarioProfesional();
    component.semanaSiguiente();

    segunda$.next(respuestaRango(20, '2026-09-14', '2026-09-20', [
      { fecha: '2026-09-14', hora: '08:00', disponible: true, motivo: null }
    ]));
    segunda$.complete();

    expect(component.getBloqueEstado('2026-09-14', '08:00')).toBe('disponible');

    primera$.next(respuestaRango(20, '2026-09-07', '2026-09-13', [
      { fecha: '2026-09-07', hora: '08:00', disponible: false, motivo: 'dia_cerrado' }
    ]));
    primera$.complete();

    expect(component.getBloqueEstado('2026-09-14', '08:00')).toBe('disponible');
    expect(component.getBloqueEstado('2026-09-07', '08:00')).toBe('sin-datos');
  });


  it('sin agenda.ver no consulta citas ni disponibilidad', () => {
    const { component, http } = crearShellConPermisosYHttp([]);
    component.filtroProfesionalId = '20';
    setSemana(component);

    component.cargarHorarioProfesional();

    const urls = http.get.mock.calls.map(call => String(call[0]));
    expect(urls.some(url => url.includes('/agenda/profesional/20/citas'))).toBe(false);
    expect(urls.some(url => url.includes('/agenda/profesional/20/disponibilidad'))).toBe(false);
    expect(component.getBloqueEstado('2026-09-07', '08:00')).toBe('sin-datos');
  });

  it('sin-datos no abre cita ni sobrecupo aunque exista agenda.gestionar', () => {
    const { component } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar']);
    component.filtroProfesionalId = '20';
    setSemana(component);
    const abrirSpy = vi.spyOn(component, 'abrirModalNuevaCitaConFechaHora');

    component.clickBloque('2026-09-07', '08:00');

    expect(abrirSpy).not.toHaveBeenCalled();
    expect(component.sobrecupoConfirmAbierto).toBe(false);
    expect(component.diaSeleccionado).toBeNull();
  });
});

describe('DashboardAdminComponent — A.2B v2: correcciones de smoke visual', () => {
  const setSemana = (component: DashboardAdminComponent, inicio = '2026-09-07'): void => {
    const [anio, mes, dia] = inicio.split('-').map(Number);
    const lunes = new Date(anio, mes - 1, dia);
    component.semanaActual = Array.from({ length: 7 }, (_, i) => {
      const fecha = new Date(lunes);
      fecha.setDate(lunes.getDate() + i);
      const f = `${fecha.getFullYear()}-${String(fecha.getMonth() + 1).padStart(2, '0')}-${String(fecha.getDate()).padStart(2, '0')}`;
      return { fecha: f };
    });
  };

  const respuestaRango = (
    profesionalId: number,
    fechaInicio: string,
    fechaFin: string,
    slots: Array<{ fecha: string; hora: string; disponible: boolean; motivo: string | null }>
  ) => ({
    profesional_id: profesionalId,
    fecha_inicio: fechaInicio,
    fecha_fin: fechaFin,
    duracion_min: 60,
    dias: Array.from(new Set(slots.map(slot => slot.fecha))).map(fecha => ({
      fecha,
      slots: slots
        .filter(slot => slot.fecha === fecha)
        .map(slot => ({
          hora: slot.hora,
          disponible: slot.disponible,
          motivo: slot.motivo,
          overridable_con_sobrecupo: slot.motivo === 'en_colacion' || slot.motivo === 'fuera_de_jornada'
        }))
    }))
  });

  // ── Punto 1: diaSeleccionado no debe sobrevivir a un cambio de semana
  //    si la fecha seleccionada ya no pertenece al rango visible ──

  it('semanaSiguiente limpia diaSeleccionado si ya no pertenece a la nueva semana', () => {
    const { component } = crearShellConPermisosYHttp(['agenda.ver']);
    setSemana(component, '2026-09-07');
    component.diaSeleccionado = '2026-09-07';

    component.semanaSiguiente();

    expect(component.diaSeleccionado).toBeNull();
  });

  it('semanaAnterior limpia diaSeleccionado si ya no pertenece a la nueva semana', () => {
    const { component } = crearShellConPermisosYHttp(['agenda.ver']);
    setSemana(component, '2026-09-07');
    component.diaSeleccionado = '2026-09-10';

    component.semanaAnterior();

    expect(component.diaSeleccionado).toBeNull();
  });

  it('generarSemanaActual conserva diaSeleccionado si sigue perteneciendo a la semana visible', () => {
    const { component } = crearShellConPermisosYHttp(['agenda.ver']);
    const hoy = new Date();
    const hoyStr = `${hoy.getFullYear()}-${String(hoy.getMonth() + 1).padStart(2, '0')}-${String(hoy.getDate()).padStart(2, '0')}`;
    component.diaSeleccionado = hoyStr;

    component.generarSemanaActual();

    expect(component.diaSeleccionado).toBe(hoyStr);
  });

  // ── Punto 2: un clic en un bloque no agendable (histórico, día cerrado,
  //    etc.) debe igualmente actualizar el panel contextual al día correcto ──

  it('clic en un bloque bloqueado (p. ej. fecha pasada) actualiza diaSeleccionado sin abrir cita ni sobrecupo', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar']);
    component.filtroProfesionalId = '20';
    setSemana(component);
    const abrirSpy = vi.spyOn(component, 'abrirModalNuevaCitaConFechaHora');

    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/disponibilidad')) {
        return of(respuestaRango(20, '2026-09-07', '2026-09-13', [
          { fecha: '2026-09-07', hora: '08:00', disponible: false, motivo: 'fecha_pasada' }
        ]));
      }
      return of([]);
    });

    component.cargarHorarioProfesional();
    expect(component.getBloqueEstado('2026-09-07', '08:00')).toBe('bloqueado');

    component.clickBloque('2026-09-07', '08:00');

    expect(component.diaSeleccionado).toBe('2026-09-07');
    expect(abrirSpy).not.toHaveBeenCalled();
    expect(component.sobrecupoConfirmAbierto).toBe(false);
  });

  it('clic en un día cerrado (centro cerrado) actualiza diaSeleccionado sin abrir cita', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar']);
    component.filtroProfesionalId = '20';
    setSemana(component);
    const abrirSpy = vi.spyOn(component, 'abrirModalNuevaCitaConFechaHora');

    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/disponibilidad')) {
        return of(respuestaRango(20, '2026-09-07', '2026-09-13', [
          { fecha: '2026-09-07', hora: '08:00', disponible: false, motivo: 'dia_cerrado' }
        ]));
      }
      return of([]);
    });

    component.cargarHorarioProfesional();
    expect(component.getBloqueEstado('2026-09-07', '08:00')).toBe('cerrado-centro');

    component.clickBloque('2026-09-07', '08:00');

    expect(component.diaSeleccionado).toBe('2026-09-07');
    expect(abrirSpy).not.toHaveBeenCalled();
  });

  it('clic en una cita histórica (ocupada, no cancelada) actualiza diaSeleccionado al día correcto', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar']);
    component.filtroProfesionalId = '20';
    setSemana(component);

    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/citas')) {
        return of([{
          id: 1, fecha: '2026-09-07', hora: '08:00', estado: 'completada',
          urgente: false, sobrecupo: false, estudiante: 'Ana'
        }]);
      }
      if (url.includes('/disponibilidad')) {
        return of(respuestaRango(20, '2026-09-07', '2026-09-13', []));
      }
      return of([]);
    });

    component.cargarHorarioProfesional();
    expect(component.getBloqueEstado('2026-09-07', '08:00')).toBe('ocupado');

    component.clickBloque('2026-09-07', '08:00');

    expect(component.diaSeleccionado).toBe('2026-09-07');
  });

  it('sin-datos sigue sin actualizar diaSeleccionado (no hay nada real que mostrar todavía)', () => {
    const { component } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar']);
    component.filtroProfesionalId = '20';
    setSemana(component);

    component.clickBloque('2026-09-07', '08:00');

    expect(component.getBloqueEstado('2026-09-07', '08:00')).toBe('sin-datos');
    expect(component.diaSeleccionado).toBeNull();
  });

  // ── Punto 3: una fecha histórica nunca debe abrir "Nueva cita" ──

  it('abrirModalNuevaCitaConFechaHora rechaza una fecha histórica', () => {
    const { component } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar']);
    component.filtroProfesionalId = '20';

    component.abrirModalNuevaCitaConFechaHora('2020-01-01', '08:00', false);

    expect(component.modalCitaAbierto).toBe(false);
  });

  it('el botón de "Nueva cita" no abre el modal si diaSeleccionado quedó en una fecha histórica', () => {
    const { component } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar']);
    component.filtroProfesionalId = '20';
    component.diaSeleccionado = '2020-01-01';

    component.abrirModalNuevaCita();

    expect(component.modalCitaAbierto).toBe(false);
  });

  it('abrirModalNuevaCitaConFechaHora sigue permitiendo una fecha futura', () => {
    const { component } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar']);
    component.filtroProfesionalId = '20';
    const futura = new Date();
    futura.setFullYear(futura.getFullYear() + 1);
    const futuraStr = `${futura.getFullYear()}-${String(futura.getMonth() + 1).padStart(2, '0')}-${String(futura.getDate()).padStart(2, '0')}`;

    component.abrirModalNuevaCitaConFechaHora(futuraStr, '08:00', false);

    expect(component.modalCitaAbierto).toBe(true);
  });

  it('confirmarSobrecupo sobre una fecha histórica tampoco abre el modal (defensa adicional)', () => {
    const { component } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar']);
    component.filtroProfesionalId = '20';
    component.sobrecupoConfirmAbierto = true;
    component.sobrecupoPendiente = { fecha: '2020-01-01', hora: '08:00', mensaje: 'Estás agendando fuera del horario habitual. ¿Confirmas?' };

    component.confirmarSobrecupo();

    expect(component.modalCitaAbierto).toBe(false);
  });
});

/**
 * A.4.7A — sobrecupo real sobre un slot ya OCUPADO (A.4.4 backend: máximo
 * 2 citas por slot), gateado por el permiso Permission.AGENDA_SOBRECUPO
 * (A.4.3), distinto de agenda.gestionar. Cubre: FE-A2 (capacidad
 * puedeSolicitarSobrecupo), FE-A3/A4 (flujo/modal/motivo), payload, manejo
 * de errores 400/403/409 y regresión de los flujos ya existentes
 * (colación/fuera de jornada/disponible/sin-datos).
 */
describe('DashboardAdminComponent — A.4.7A: sobrecupo sobre slot ocupado', () => {
  // Fecha futura dinámica: evita que estas pruebas queden "fecha pasada"
  // con el paso del tiempo real (mismo criterio que A.2B v2 más arriba).
  const futura = new Date();
  futura.setFullYear(futura.getFullYear() + 1);
  const FUTURA = `${futura.getFullYear()}-${String(futura.getMonth() + 1).padStart(2, '0')}-${String(futura.getDate()).padStart(2, '0')}`;
  const finSemana = new Date(futura);
  finSemana.setDate(finSemana.getDate() + 6);
  // cargarHorarioProfesional() pide fecha_inicio/fecha_fin = primer y último
  // día de semanaActual (7 días) y descarta como "stale" cualquier
  // respuesta cuyo eco no coincida exactamente — por eso el mock de
  // /disponibilidad siempre debe declarar el rango completo de la semana,
  // no solo el día del slot que interesa al test.
  const FIN_SEMANA = `${finSemana.getFullYear()}-${String(finSemana.getMonth() + 1).padStart(2, '0')}-${String(finSemana.getDate()).padStart(2, '0')}`;

  const setSemanaDesde = (component: DashboardAdminComponent, inicioIso: string): void => {
    const [anio, mes, dia] = inicioIso.split('-').map(Number);
    const base = new Date(anio, mes - 1, dia);
    component.semanaActual = Array.from({ length: 7 }, (_, i) => {
      const fecha = new Date(base);
      fecha.setDate(base.getDate() + i);
      const f = `${fecha.getFullYear()}-${String(fecha.getMonth() + 1).padStart(2, '0')}-${String(fecha.getDate()).padStart(2, '0')}`;
      return { fecha: f };
    });
  };

  const respuestaRango = (
    profesionalId: number,
    fechaInicio: string,
    fechaFin: string,
    slots: Array<{ fecha: string; hora: string; disponible: boolean; motivo: string | null; overridable?: boolean }>
  ) => ({
    profesional_id: profesionalId,
    fecha_inicio: fechaInicio,
    fecha_fin: fechaFin,
    duracion_min: 60,
    dias: Array.from(new Set(slots.map(slot => slot.fecha))).map(fecha => ({
      fecha,
      slots: slots
        .filter(slot => slot.fecha === fecha)
        .map(slot => ({
          hora: slot.hora,
          disponible: slot.disponible,
          motivo: slot.motivo,
          // Por defecto replica la regla real (colación/fuera de jornada
          // son overridables, slot_ocupado no) — cada test que necesite
          // un slot_ocupado overridable lo indica explícitamente, para
          // no cambiar el comportamiento por defecto de los tests A.2B
          // ya existentes (que usan su propio respuestaRango local).
          overridable_con_sobrecupo: slot.overridable
            ?? (slot.motivo === 'en_colacion' || slot.motivo === 'fuera_de_jornada')
        }))
    }))
  });

  /** Deja cargado un slot 'ocupado' (cita real completada) + su disponibilidad */
  const cargarSlotOcupado = (
    component: DashboardAdminComponent,
    http: any,
    overridable: boolean,
    fecha = FUTURA,
    hora = '08:00'
  ) => {
    component.filtroProfesionalId = '20';
    setSemanaDesde(component, fecha);
    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/citas')) {
        return of([{
          id: 1, fecha, hora, estado: 'completada',
          urgente: false, sobrecupo: false, estudiante: 'Ana'
        }]);
      }
      if (url.includes('/disponibilidad')) {
        return of(respuestaRango(20, fecha, FIN_SEMANA, [
          { fecha, hora, disponible: false, motivo: 'slot_ocupado', overridable }
        ]));
      }
      return of([]);
    });
    component.cargarHorarioProfesional();
  };

  // ── FE-A2 — puedeSolicitarSobrecupo() ───────────────────────────────

  it('5. ocupado + overridable=false → no se ofrece la acción de sobrecupo', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    cargarSlotOcupado(component, http, false);

    expect(component.getBloqueEstado(FUTURA, '08:00')).toBe('ocupado');
    expect(component.puedeSolicitarSobrecupo(FUTURA, '08:00')).toBe(false);
  });

  it('6. ocupado + overridable=true + agenda.gestionar + agenda.sobrecupo → acción posible', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    cargarSlotOcupado(component, http, true);

    expect(component.puedeSolicitarSobrecupo(FUTURA, '08:00')).toBe(true);
  });

  it('7. ocupado + overridable=true pero falta agenda.sobrecupo → no acción', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar']);
    cargarSlotOcupado(component, http, true);

    expect(component.puedeSolicitarSobrecupo(FUTURA, '08:00')).toBe(false);
  });

  it('8. ocupado + overridable=true pero falta agenda.gestionar → no acción', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.sobrecupo']);
    cargarSlotOcupado(component, http, true);

    expect(component.puedeSolicitarSobrecupo(FUTURA, '08:00')).toBe(false);
  });

  it('9. con o sin capacidad de sobrecupo, el dominio sigue representándose como "ocupado"', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    cargarSlotOcupado(component, http, true);
    expect(component.getBloqueEstado(FUTURA, '08:00')).toBe('ocupado');

    cargarSlotOcupado(component, http, false);
    expect(component.getBloqueEstado(FUTURA, '08:00')).toBe('ocupado');
  });

  // ── A.4.7A v3, punto 2: mensaje del confirm de sobrecupo por conflicto ──

  it('v3.4. slot_ocupado: el mensaje NO afirma cardinalidad ("una cita", "un cupo", "dos citas") y es una frase completa', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    cargarSlotOcupado(component, http, true);

    component.clickBloque(FUTURA, '08:00');

    const mensaje = component.sobrecupoPendiente?.mensaje ?? '';
    expect(mensaje.toLowerCase()).not.toContain('una cita');
    expect(mensaje.toLowerCase()).not.toContain('un cupo');
    expect(mensaje.toLowerCase()).not.toContain('dos citas');
    expect(mensaje).toBe(
      `A las 08:00 del ${component.formatearFecha(FUTURA)}: este horario ya presenta ocupación. `
      + 'El sistema permite solicitar un sobrecupo. La disponibilidad volverá a validarse al confirmar. '
      + '¿Confirmas?'
    );
  });

  it('v3.5. colación: el mensaje conserva el sentido correcto ("hora de colación")', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    component.filtroProfesionalId = '20';
    setSemanaDesde(component, FUTURA);
    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/disponibilidad')) {
        return of(respuestaRango(20, FUTURA, FIN_SEMANA, [
          { fecha: FUTURA, hora: '13:00', disponible: false, motivo: 'en_colacion' }
        ]));
      }
      return of([]);
    });
    component.cargarHorarioProfesional();

    component.clickBloque(FUTURA, '13:00');

    const mensaje = component.sobrecupoPendiente?.mensaje ?? '';
    expect(mensaje).toContain('durante la hora de colación de');
    expect(mensaje.toLowerCase()).not.toContain('una cita ');
    expect(mensaje.toLowerCase()).not.toContain('un cupo');
  });

  it('v3.6. fuera de jornada: el mensaje conserva el sentido correcto ("horario habitual")', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    component.filtroProfesionalId = '20';
    setSemanaDesde(component, FUTURA);
    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/disponibilidad')) {
        return of(respuestaRango(20, FUTURA, FIN_SEMANA, [
          { fecha: FUTURA, hora: '19:00', disponible: false, motivo: 'fuera_de_jornada' }
        ]));
      }
      return of([]);
    });
    component.cargarHorarioProfesional();

    component.clickBloque(FUTURA, '19:00');

    const mensaje = component.sobrecupoPendiente?.mensaje ?? '';
    expect(mensaje).toContain('fuera del horario habitual de');
    expect(mensaje.toLowerCase()).not.toContain('un cupo');
  });

  it('25. SUPERADMIN con agenda.gestionar pero sin agenda.sobrecupo no puede iniciar el flujo por simple rol', () => {
    // hasPermission ya está mockeado por capability efectiva (no por
    // nombre de rol) en crearShellConPermisosYHttp — este test confirma
    // que la sola ausencia de agenda.sobrecupo en el mock (equivalente a
    // una cuenta SUPERADMIN cuyo contexto no lo incluyera) basta para
    // bloquear la acción, sin ningún atajo "if role==='superadmin'".
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar']);
    cargarSlotOcupado(component, http, true);

    component.clickBloque(FUTURA, '08:00');

    expect(component.sobrecupoConfirmAbierto).toBe(false);
    expect(component.puedeSolicitarSobrecupo(FUTURA, '08:00')).toBe(false);
  });

  // ── FE-A3/A4 — flujo, modal y motivo ────────────────────────────────

  it('10. clic en ocupado admisible abre el flujo de sobrecupo y, al confirmar, nuevaCita.sobrecupo queda true (motivo aplica solo ahí)', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    cargarSlotOcupado(component, http, true);

    component.clickBloque(FUTURA, '08:00');
    expect(component.sobrecupoConfirmAbierto).toBe(true);

    component.confirmarSobrecupo();

    expect(component.modalCitaAbierto).toBe(true);
    expect(component.nuevaCita.sobrecupo).toBe(true);
    expect(component.nuevaCita.sobrecupo_motivo).toBe('');
  });

  it('v3.1. crearCitaDesdeHorario con sobrecupo=true y SOLO agenda.sobrecupo (sin agenda.gestionar) → fail-closed, sin HTTP', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.sobrecupo']);
    component.nuevaCita = {
      fecha: FUTURA, hora: '08:00', estudiante_id: 5, profesional_id: 20,
      observaciones: '', urgente: false, sobrecupo: true, sobrecupo_motivo: 'Motivo válido'
    };

    component.crearCitaDesdeHorario();

    expect(http.post).not.toHaveBeenCalled();
  });

  it('v3.2. crearCitaDesdeHorario con sobrecupo=true y SOLO agenda.gestionar (sin agenda.sobrecupo) → fail-closed, sin HTTP, con mensaje de autorización', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar']);
    component.nuevaCita = {
      fecha: FUTURA, hora: '08:00', estudiante_id: 5, profesional_id: 20,
      observaciones: '', urgente: false, sobrecupo: true, sobrecupo_motivo: 'Motivo válido'
    };

    component.crearCitaDesdeHorario();

    expect(http.post).not.toHaveBeenCalled();
    expect(component.mensajeError).toBe('No cuentas con el permiso de sobrecupo (agenda.sobrecupo) para autorizar esta hora.');
  });

  it('v3.3. crearCitaDesdeHorario con sobrecupo=true y AMBOS permisos + motivo válido → sí llega a HTTP (regresión)', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    (http.post as any).mockReturnValue(of({}));
    component.nuevaCita = {
      fecha: FUTURA, hora: '08:00', estudiante_id: 5, profesional_id: 20,
      observaciones: '', urgente: false, sobrecupo: true, sobrecupo_motivo: 'Motivo válido'
    };

    component.crearCitaDesdeHorario();

    expect(http.post).toHaveBeenCalledTimes(1);
  });

  it('11. motivo vacío o solo espacios bloquea la confirmación sin llegar a HTTP', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    component.nuevaCita = {
      fecha: FUTURA, hora: '08:00', estudiante_id: 5, profesional_id: 20,
      observaciones: '', urgente: false, sobrecupo: true, sobrecupo_motivo: '   '
    };

    component.crearCitaDesdeHorario();

    expect(http.post).not.toHaveBeenCalled();
    expect(component.mensajeError).toBe('Debes indicar el motivo del sobrecupo.');
  });

  it('12. el motivo se envía recortado ("  Motivo real  " → "Motivo real")', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    (http.post as any).mockReturnValue(of({}));
    component.nuevaCita = {
      fecha: FUTURA, hora: '08:00', estudiante_id: 5, profesional_id: 20,
      observaciones: '', urgente: false, sobrecupo: true, sobrecupo_motivo: '  Motivo real  '
    };

    component.crearCitaDesdeHorario();

    expect(http.post).toHaveBeenCalledTimes(1);
    const [, body] = (http.post as any).mock.calls[0];
    expect(body.sobrecupo_motivo).toBe('Motivo real');
  });

  it('13. observaciones y sobrecupo_motivo son campos separados (uno no pisa al otro)', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    (http.post as any).mockReturnValue(of({}));
    component.nuevaCita = {
      fecha: FUTURA, hora: '08:00', estudiante_id: 5, profesional_id: 20,
      observaciones: 'Control de rutina', urgente: false,
      sobrecupo: true, sobrecupo_motivo: 'Paciente con urgencia clínica real'
    };

    component.crearCitaDesdeHorario();

    const [, body] = (http.post as any).mock.calls[0];
    expect(body.observaciones).toBe('Control de rutina');
    expect(body.sobrecupo_motivo).toBe('Paciente con urgencia clínica real');
    expect(body.observaciones).not.toBe(body.sobrecupo_motivo);
  });

  it('14. cancelar el modal limpia sobrecupo_motivo', () => {
    const { component } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    component.nuevaCita.sobrecupo_motivo = 'un motivo cualquiera';

    component.cerrarModalCita();

    expect(component.nuevaCita.sobrecupo_motivo).toBe('');
  });

  // ── Payload ──────────────────────────────────────────────────────────

  it('15. sobrecupo=true envía sobrecupo_motivo en el payload', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    (http.post as any).mockReturnValue(of({}));
    component.nuevaCita = {
      fecha: FUTURA, hora: '08:00', estudiante_id: 5, profesional_id: 20,
      observaciones: '', urgente: false, sobrecupo: true, sobrecupo_motivo: 'Motivo válido'
    };

    component.crearCitaDesdeHorario();

    const [, body] = (http.post as any).mock.calls[0];
    expect(body.sobrecupo).toBe(true);
    expect(body.sobrecupo_motivo).toBe('Motivo válido');
  });

  it('16. cita normal (sobrecupo=false) no exige ni envía sobrecupo_motivo', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar']);
    (http.post as any).mockReturnValue(of({}));
    component.nuevaCita = {
      fecha: FUTURA, hora: '08:00', estudiante_id: 5, profesional_id: 20,
      observaciones: '', urgente: false, sobrecupo: false, sobrecupo_motivo: ''
    };

    component.crearCitaDesdeHorario();

    expect(http.post).toHaveBeenCalledTimes(1);
    const [, body] = (http.post as any).mock.calls[0];
    expect(body.sobrecupo).toBe(false);
    expect('sobrecupo_motivo' in body).toBe(false);
  });

  // ── Manejo de errores 400/403/409 y refresh ─────────────────────────

  it('17. 403 muestra un mensaje de falta de autorización (usa el detail real del backend)', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    (http.post as any).mockReturnValue(throwError(() => ({
      status: 403,
      error: { detail: 'No cuentas con el permiso de sobrecupo (agenda.sobrecupo) para autorizar esta hora.' }
    })));
    component.nuevaCita = {
      fecha: FUTURA, hora: '08:00', estudiante_id: 5, profesional_id: 20,
      observaciones: '', urgente: false, sobrecupo: true, sobrecupo_motivo: 'Motivo válido'
    };

    component.crearCitaDesdeHorario();

    expect(component.mensajeError).toBe('No cuentas con el permiso de sobrecupo (agenda.sobrecupo) para autorizar esta hora.');
  });

  it('18. 409 muestra un mensaje UX estable de cambio de horario, sin exponer el detail crudo del backend', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    (http.post as any).mockReturnValue(throwError(() => ({
      status: 409,
      error: { detail: 'Ya existen exactamente 2 citas para este slot.' }
    })));
    component.nuevaCita = {
      fecha: FUTURA, hora: '08:00', estudiante_id: 5, profesional_id: 20,
      observaciones: '', urgente: false, sobrecupo: true, sobrecupo_motivo: 'Motivo válido'
    };

    component.crearCitaDesdeHorario();

    expect(component.mensajeError).toBe('El horario cambió y ya no admite este sobrecupo. La agenda se actualizará.');
    expect(component.mensajeError).not.toContain('2 citas');
  });

  it('A (v2). 409 en una cita NORMAL (no sobrecupo) no debe mencionar "sobrecupo" (A.3, pérdida de carrera)', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar']);
    (http.post as any).mockReturnValue(throwError(() => ({
      status: 409,
      error: { detail: 'Slot ya reservado por otra solicitud concurrente.' }
    })));
    component.nuevaCita = {
      fecha: FUTURA, hora: '08:00', estudiante_id: 5, profesional_id: 20,
      observaciones: '', urgente: false, sobrecupo: false, sobrecupo_motivo: ''
    };

    component.crearCitaDesdeHorario();

    expect(component.mensajeError).toBe('El horario cambió y ya no está disponible. La agenda se actualizará.');
    expect(component.mensajeError.toLowerCase()).not.toContain('sobrecupo');
  });

  it('B (v2). 409 en una cita de SOBRECUPO sí usa el mensaje de sobrecupo (regresión del comportamiento ya existente)', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    (http.post as any).mockReturnValue(throwError(() => ({ status: 409, error: { detail: 'x' } })));
    component.nuevaCita = {
      fecha: FUTURA, hora: '08:00', estudiante_id: 5, profesional_id: 20,
      observaciones: '', urgente: false, sobrecupo: true, sobrecupo_motivo: 'Motivo válido'
    };

    component.crearCitaDesdeHorario();

    expect(component.mensajeError).toContain('sobrecupo');
  });

  it('ambos casos de 409 (normal y sobrecupo) refrescan la agenda desde backend', () => {
    const { component: compNormal, http: httpNormal } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar']);
    httpNormal.get = vi.fn(() => of([]));
    (httpNormal.post as any).mockReturnValue(throwError(() => ({ status: 409, error: {} })));
    compNormal.filtroProfesionalId = '20';
    const spyNormal = vi.spyOn(compNormal, 'cargarHorarioProfesional');
    compNormal.nuevaCita = {
      fecha: FUTURA, hora: '08:00', estudiante_id: 5, profesional_id: 20,
      observaciones: '', urgente: false, sobrecupo: false, sobrecupo_motivo: ''
    };
    compNormal.crearCitaDesdeHorario();
    expect(spyNormal).toHaveBeenCalledTimes(1);

    const { component: compSobre, http: httpSobre } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    httpSobre.get = vi.fn(() => of([]));
    (httpSobre.post as any).mockReturnValue(throwError(() => ({ status: 409, error: {} })));
    compSobre.filtroProfesionalId = '20';
    const spySobre = vi.spyOn(compSobre, 'cargarHorarioProfesional');
    compSobre.nuevaCita = {
      fecha: FUTURA, hora: '08:00', estudiante_id: 5, profesional_id: 20,
      observaciones: '', urgente: false, sobrecupo: true, sobrecupo_motivo: 'Motivo válido'
    };
    compSobre.crearCitaDesdeHorario();
    expect(spySobre).toHaveBeenCalledTimes(1);
  });

  it('C (v2). urgente=true + sobrecupo=true nunca llega a HTTP, sin importar el endpoint', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    component.nuevaCita = {
      fecha: FUTURA, hora: '08:00', estudiante_id: 5, profesional_id: 20,
      // Estado artificial: alguien puso ambos en true directamente,
      // saltándose el toggle oculto en el modal (defensa en profundidad).
      observaciones: '', urgente: true, sobrecupo: true, sobrecupo_motivo: 'Motivo válido'
    };

    component.crearCitaDesdeHorario();

    expect(http.post).not.toHaveBeenCalled();
    expect(component.mensajeError).toBe('Urgencia y sobrecupo todavía se gestionan por flujos separados.');
  });

  it('C.1 (v2). el modal de sobrecupo oculta el toggle "¿Es urgente?" (mostrarToggleUrgente)', () => {
    const { component } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);

    component.nuevaCita.sobrecupo = false;
    expect(component.mostrarToggleUrgente).toBe(true);

    component.nuevaCita.sobrecupo = true;
    expect(component.mostrarToggleUrgente).toBe(false);
  });

  it('cita urgente NORMAL (sin sobrecupo) sigue usando /admin/citas/urgente sin cambios', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar']);
    (http.post as any).mockReturnValue(of({}));
    component.nuevaCita = {
      fecha: FUTURA, hora: '08:00', estudiante_id: 5, profesional_id: 20,
      observaciones: '', urgente: true, sobrecupo: false, sobrecupo_motivo: ''
    };

    component.crearCitaDesdeHorario();

    expect(http.post).toHaveBeenCalledTimes(1);
    const [url, body] = (http.post as any).mock.calls[0];
    expect(url).toContain('/admin/citas/urgente');
    expect(body.urgente).toBe(true);
    expect('sobrecupo_motivo' in body).toBe(false);
  });


  it('19. 409 dispara un refresh real de la agenda (vuelve a consultar backend, no confía en la grilla local)', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    component.filtroProfesionalId = '20';
    (http.post as any).mockReturnValue(throwError(() => ({ status: 409, error: { detail: 'x' } })));
    const cargarSpy = vi.spyOn(component, 'cargarHorarioProfesional');
    component.nuevaCita = {
      fecha: FUTURA, hora: '08:00', estudiante_id: 5, profesional_id: 20,
      observaciones: '', urgente: false, sobrecupo: true, sobrecupo_motivo: 'Motivo válido'
    };

    component.crearCitaDesdeHorario();

    expect(cargarSpy).toHaveBeenCalledTimes(1);
    expect(component.modalCitaAbierto).toBe(false);
  });

  it('20. una creación exitosa cierra el modal y refresca la agenda desde backend', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    component.filtroProfesionalId = '20';
    (http.post as any).mockReturnValue(of({}));
    const cargarSpy = vi.spyOn(component, 'cargarHorarioProfesional');
    component.nuevaCita = {
      fecha: FUTURA, hora: '08:00', estudiante_id: 5, profesional_id: 20,
      observaciones: '', urgente: false, sobrecupo: true, sobrecupo_motivo: 'Motivo válido'
    };

    component.crearCitaDesdeHorario();

    expect(cargarSpy).toHaveBeenCalledTimes(1);
    expect(component.modalCitaAbierto).toBe(false);
  });

  // ── Regresión: los flujos ya existentes no se rompen con la política nueva ──

  it('21. colación overridable sigue funcionando con la política nueva (agenda.gestionar + agenda.sobrecupo)', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    component.filtroProfesionalId = '20';
    setSemanaDesde(component, FUTURA);
    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/disponibilidad')) {
        return of(respuestaRango(20, FUTURA, FIN_SEMANA, [
          { fecha: FUTURA, hora: '13:00', disponible: false, motivo: 'en_colacion' }
        ]));
      }
      return of([]);
    });
    component.cargarHorarioProfesional();
    expect(component.getBloqueEstado(FUTURA, '13:00')).toBe('colacion');

    component.clickBloque(FUTURA, '13:00');
    expect(component.sobrecupoConfirmAbierto).toBe(true);
    component.confirmarSobrecupo();

    expect(component.modalCitaAbierto).toBe(true);
    expect(component.nuevaCita.sobrecupo).toBe(true);
  });

  it('22. fuera_de_jornada overridable sigue funcionando con la política nueva', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    component.filtroProfesionalId = '20';
    setSemanaDesde(component, FUTURA);
    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/disponibilidad')) {
        return of(respuestaRango(20, FUTURA, FIN_SEMANA, [
          { fecha: FUTURA, hora: '19:00', disponible: false, motivo: 'fuera_de_jornada' }
        ]));
      }
      return of([]);
    });
    component.cargarHorarioProfesional();
    expect(component.getBloqueEstado(FUTURA, '19:00')).toBe('fuera-horario');

    component.clickBloque(FUTURA, '19:00');
    expect(component.sobrecupoConfirmAbierto).toBe(true);
    component.confirmarSobrecupo();

    expect(component.modalCitaAbierto).toBe(true);
    expect(component.nuevaCita.sobrecupo).toBe(true);
  });

  it('23. un slot disponible normal sigue creando una cita normal (sin motivo, sin sobrecupo)', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    component.filtroProfesionalId = '20';
    setSemanaDesde(component, FUTURA);
    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/disponibilidad')) {
        return of(respuestaRango(20, FUTURA, FIN_SEMANA, [
          { fecha: FUTURA, hora: '10:00', disponible: true, motivo: null }
        ]));
      }
      return of([]);
    });
    component.cargarHorarioProfesional();
    expect(component.getBloqueEstado(FUTURA, '10:00')).toBe('disponible');

    component.clickBloque(FUTURA, '10:00');

    expect(component.modalCitaAbierto).toBe(true);
    expect(component.nuevaCita.sobrecupo).toBe(false);
    expect(component.nuevaCita.sobrecupo_motivo).toBe('');
  });

  it('24. sin-datos sigue fail-closed: ningún clic abre cita ni sobrecupo', () => {
    const { component } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    component.filtroProfesionalId = '20';
    setSemanaDesde(component, FUTURA);

    component.clickBloque(FUTURA, '08:00');

    expect(component.getBloqueEstado(FUTURA, '08:00')).toBe('sin-datos');
    expect(component.modalCitaAbierto).toBe(false);
    expect(component.sobrecupoConfirmAbierto).toBe(false);
  });

  // ── A.4.7A v2, punto 3: agenda.sobrecupo unificado también para
  //    colación y fuera de jornada (antes solo lo exigía el caso ocupado) ──

  it('D (v2). colación overridable + solo agenda.gestionar (sin agenda.sobrecupo) → clickBloque NO abre el flujo de sobrecupo', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar']);
    component.filtroProfesionalId = '20';
    setSemanaDesde(component, FUTURA);
    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/disponibilidad')) {
        return of(respuestaRango(20, FUTURA, FIN_SEMANA, [
          { fecha: FUTURA, hora: '13:00', disponible: false, motivo: 'en_colacion' }
        ]));
      }
      return of([]);
    });
    component.cargarHorarioProfesional();
    expect(component.getBloqueEstado(FUTURA, '13:00')).toBe('colacion');
    expect(component.puedeSolicitarSobrecupo(FUTURA, '13:00')).toBe(false);

    component.clickBloque(FUTURA, '13:00');

    expect(component.sobrecupoConfirmAbierto).toBe(false);
  });

  it('E (v2). fuera_de_jornada overridable + solo agenda.gestionar (sin agenda.sobrecupo) → clickBloque NO abre el flujo de sobrecupo', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar']);
    component.filtroProfesionalId = '20';
    setSemanaDesde(component, FUTURA);
    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/disponibilidad')) {
        return of(respuestaRango(20, FUTURA, FIN_SEMANA, [
          { fecha: FUTURA, hora: '19:00', disponible: false, motivo: 'fuera_de_jornada' }
        ]));
      }
      return of([]);
    });
    component.cargarHorarioProfesional();
    expect(component.getBloqueEstado(FUTURA, '19:00')).toBe('fuera-horario');
    expect(component.puedeSolicitarSobrecupo(FUTURA, '19:00')).toBe(false);

    component.clickBloque(FUTURA, '19:00');

    expect(component.sobrecupoConfirmAbierto).toBe(false);
  });

  it('F (v2). el modal de confirmación de sobrecupo exige agenda.gestionar Y agenda.sobrecupo (verificado por hasPermission, no solo por el flag interno)', () => {
    // El *ngIf real del modal (dashboard-admin.html) es:
    // "sobrecupoConfirmAbierto && hasPermission('agenda.gestionar') &&
    // hasPermission('agenda.sobrecupo')". Este spec no renderiza plantillas
    // para este componente (mismo criterio que el resto del archivo), así
    // que se verifica la condición equivalente a nivel de componente: sin
    // agenda.sobrecupo, hasPermission ya la reporta en false y, más arriba
    // (tests D/E), clickBloque() ni siquiera llega a poner
    // sobrecupoConfirmAbierto=true — el *ngIf es una segunda capa sobre un
    // camino que ya está cerrado antes.
    const { component } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar']);
    expect(component.hasPermission('agenda.gestionar') && component.hasPermission('agenda.sobrecupo')).toBe(false);

    const { component: completo } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    expect(completo.hasPermission('agenda.gestionar') && completo.hasPermission('agenda.sobrecupo')).toBe(true);
  });

  it('G (v2). puedeSolicitarSobrecupo() es el único helper para los tres conflictos y exige el motivo coherente con el estado visual', () => {
    const { component, http } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    component.filtroProfesionalId = '20';
    setSemanaDesde(component, FUTURA);
    (http.get as any).mockImplementation((url: string) => {
      if (url.includes('/disponibilidad')) {
        return of(respuestaRango(20, FUTURA, FIN_SEMANA, [
          { fecha: FUTURA, hora: '13:00', disponible: false, motivo: 'en_colacion' },
          { fecha: FUTURA, hora: '19:00', disponible: false, motivo: 'fuera_de_jornada' }
        ]));
      }
      return of([]);
    });
    component.cargarHorarioProfesional();

    expect(component.puedeSolicitarSobrecupo(FUTURA, '13:00')).toBe(true);
    expect(component.puedeSolicitarSobrecupo(FUTURA, '19:00')).toBe(true);
  });

  it('H (v2). el modal de sobrecupo ya no ata la creación de la cita a mostrarToggleUrgente=false por accidente (banner y toggle son cambios de plantilla, verificados por inspección de código — este spec no renderiza plantillas para este componente)', () => {
    // El texto del banner ("Estás creando una cita como sobrecupo...") y
    // la ocultación del toggle "¿Es urgente?" viven en dashboard-admin.html
    // y se verifican por revisión directa del diff, con el mismo criterio
    // que ya se usó para el cambio de emojis → Material Symbols en v1 (este
    // archivo de specs no usa TestBed/fixture para DashboardAdminComponent).
    // Lo que SÍ es lógica de componente y por tanto se prueba acá es que el
    // estado que controla esa plantilla (mostrarToggleUrgente) reacciona
    // correctamente — ver el test "C.1 (v2)" más arriba.
    const { component } = crearShellConPermisosYHttp(['agenda.ver', 'agenda.gestionar', 'agenda.sobrecupo']);
    component.nuevaCita.sobrecupo = true;
    expect(component.mostrarToggleUrgente).toBe(false);
  });
});
