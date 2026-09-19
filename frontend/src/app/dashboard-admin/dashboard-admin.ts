import { Component, OnInit, ViewChild, ViewEncapsulation, ChangeDetectorRef } from '@angular/core';
import { CommonModule, DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { HttpClient } from '@angular/common/http';
import { environment } from '../config';
import { obtenerFeriado } from '../shared/feriados-chile';
import { ToastService } from '../shared/toast/toast.service';
import { AuthService } from '../auth.service';
import { Permission } from '../shared/auth/permission.model';
import { AdminCitasComponent } from './citas/admin-citas';
import { AdminProfesionalesComponent } from './profesionales/admin-profesionales';
import { AdminPerfilComponent } from './perfil/admin-perfil';
import { AdminHistorialComponent } from './historial/admin-historial';
import { AdminReportesComponent } from './reportes/admin-reportes';
import { AdminEstudiantesComponent } from './estudiantes/admin-estudiantes';
import { AdminInicioComponent } from './inicio/admin-inicio';
import { AdminHorarioComponent } from './horario/admin-horario';
import { AdminConfiguracionComponent, ConfigTab } from './configuracion/admin-configuracion';
import { AdminAdministradoresComponent } from './administradores/admin-administradores';
import * as XLSX from 'xlsx';


interface SlotDisponibilidadBackend {
  hora: string;
  disponible: boolean;
  motivo: string | null;
  overridable_con_sobrecupo: boolean;
}

interface DiaDisponibilidadBackend {
  fecha: string;
  slots: SlotDisponibilidadBackend[];
}

interface RespuestaDisponibilidadRango {
  profesional_id: number;
  fecha_inicio: string;
  fecha_fin: string;
  duracion_min: number;
  dias: DiaDisponibilidadBackend[];
}

const API = environment.apiUrl;

@Component({
  selector: 'app-dashboard-admin',
  standalone: true,
  imports: [CommonModule, FormsModule, DatePipe, AdminCitasComponent, AdminProfesionalesComponent, AdminPerfilComponent, AdminHistorialComponent, AdminReportesComponent, AdminEstudiantesComponent, AdminInicioComponent, AdminHorarioComponent, AdminConfiguracionComponent, AdminAdministradoresComponent],
  templateUrl: './dashboard-admin.html',
  styleUrl: './dashboard-admin.css',
  encapsulation: ViewEncapsulation.None
})
export class DashboardAdminComponent implements OnInit {
// Referencia al hijo para cerrar sus modales tras una operación CRUD exitosa
// (el estado de los modales vive en el hijo; el shell sigue ejecutando el HTTP).
@ViewChild(AdminProfesionalesComponent) adminProfesionalesRef?: AdminProfesionalesComponent;
// Referencia al hijo Mi Perfil: el shell la usa (Fase 3.4D) para restablecer
// el estado de edición del hijo tras un guardado exitoso, ya que ese estado
// (adminPerfilEnEdicion, fotoAdminCambiada, contraseñas) ahora vive allí.
@ViewChild(AdminPerfilComponent) adminPerfilRef?: AdminPerfilComponent;

sidebarMovilAbierta = false;

toggleSidebarMovil(): void {
  this.sidebarMovilAbierta = !this.sidebarMovilAbierta;
}
  seccionActiva = 'inicio';
  temaOscuro    = false;

  // mensajeExito / mensajeError quedan como getters/setters por compatibilidad
  // con el resto del código (decenas de lugares hacen "this.mensajeExito = '...'").
  // Al asignarles un valor, disparan automaticamente un toast -- asi no hubo
  // que reescribir cada uno de esos lugares uno por uno.
  private _mensajeExito = '';
  set mensajeExito(valor: string) { this._mensajeExito = valor; if (valor) this.toast.success(valor); }
  get mensajeExito(): string { return this._mensajeExito; }

  private _mensajeError = '';
  set mensajeError(valor: string) { this._mensajeError = valor; if (valor) this.toast.error(valor); }
  get mensajeError(): string { return this._mensajeError; }

  get tituloSeccion(): string {
    const map: Record<string, string> = {
      inicio: 'Panel Administrativo SESAES', horario: 'Agenda / Calendario clínico',
      citas: 'Gestión de Citas', profesional: 'Gestión de Profesionales',
      estudiantes: 'Estudiantes', historial: 'Historial de Atenciones',
      reportes: 'Reportes', configuracion: 'Configuración del Sistema',
      miperfil: 'Mi Perfil', administradores: 'Administradores'
    };
    return map[this.seccionActiva] ?? 'SESAES';
  }

  get subtituloSeccion(): string {
    const map: Record<string, string> = {
      inicio: 'Gestiona profesionales, horarios y reservas de bienestar estudiantil.',
      horario: 'Visualización de citas, disponibilidad y resumen operativo.',
      citas: 'Revisa, prioriza y cancela las citas agendadas en el centro.',
      profesional: 'Administra el personal médico, psicólogos y especialistas del centro de salud.',
      estudiantes: 'Consulta estudiantes por nombre, RUT o carrera y revisa su ficha.',
      historial: 'Consulta las atenciones que ya ocurrieron y exporta reportes para la CGR.',
      reportes: 'Analiza el funcionamiento del servicio: demanda, cancelaciones y prioridades.',
      configuracion: 'Reglas generales del sistema: citas, horarios, usuarios y seguridad.',
      miperfil: 'Tu información personal y credenciales de acceso.',
      administradores: 'Gestiona las cuentas ADMIN y SUPERADMIN del sistema.'
    };
    return map[this.seccionActiva] ?? '';
  }

  constructor(
    private router: Router,
    private http: HttpClient,
    private cdr: ChangeDetectorRef,
    private toast: ToastService,
    private auth: AuthService
  ) {}

  hasPermission(permission: Permission): boolean {
    return this.auth.hasPermission(permission);
  }

  // ── Identidad real del shell/topbar (SA-1.1) ──────────────────
  // Fuente de verdad: AuthService / datos de sesión (nombre, foto_url,
  // rol). NUNCA ConfiguracionCentro.nombre_admin / foto_admin_url.
  // Desde SA-1.2, Mi Perfil (ver AdminPerfilComponent) también usa
  // Usuario como fuente de identidad (vía GET/PATCH /usuarios/me) y,
  // tras guardar, sincroniza esta misma sesión mediante
  // AuthService.actualizarIdentidadSesion() — por eso ambos quedan
  // consistentes sin exigir logout/login.

  get nombreUsuario(): string {
    return this.auth.getNombre() || 'Administrador/a';
  }

  get fotoUsuarioUrl(): string | null {
    return this.auth.getFotoUrl();
  }

  // Mapeo visual del rol real de la sesión. Nunca infiere el rol desde
  // permisos ni muestra el string técnico "superadmin" tal cual.
  get rolVisual(): string {
    return this.auth.getRol() === 'superadmin' ? 'Superadministrador' : 'Administrador';
  }

  // Iniciales derivadas del nombre real de la sesión.
  get inicialesUsuario(): string {
    const nombre = this.auth.getNombre();
    if (!nombre) return 'AD';

    const palabras = nombre.trim().split(/\s+/).filter(Boolean);
    if (palabras.length === 0) return 'AD';

    const primera = palabras[0][0] ?? '';
    const segunda = palabras.length > 1
      ? (palabras[1][0] ?? '')
      : (palabras[0][1] ?? '');

    const iniciales = (primera + segunda).toUpperCase();
    return iniciales || 'AD';
  }
  get puedeExportarCgr(): boolean {
    return this.hasPermission('reportes.cgr.exportar');
  }

  get puedeConfigGeneral(): boolean {
    return this.hasPermission('configuracion.gestionar');
  }

  get puedeConfigCitas(): boolean {
    return this.hasPermission('configuracion.gestionar');
  }

  get puedeConfigHorarios(): boolean {
    return this.hasPermission('agenda.gestionar');
  }

  get puedeConfigUsuarios(): boolean {
    return this.hasPermission('usuarios.gestionar');
  }

  get puedeVerAuditoria(): boolean {
    return this.hasPermission('auditoria.ver');
  }

  puedeAccederSeccion(seccion: string): boolean {
    switch (seccion) {
      case 'inicio':
      case 'miperfil':
        return true;
      case 'horario':
      case 'citas':
        return this.hasPermission('agenda.ver');
      case 'profesional':
        return this.hasPermission('profesionales.ver');
      case 'estudiantes':
        return this.hasPermission('usuarios.ver');
      case 'historial':
      case 'reportes':
        return this.hasPermission('reportes.ver');
      case 'configuracion':
        return (
          this.puedeConfigGeneral ||
          this.puedeConfigHorarios ||
          this.puedeConfigUsuarios ||
          this.puedeVerAuditoria
        );
      // SA-4 — Administradores: gobernanza de cuentas ADMIN/SUPERADMIN.
      // Único permiso que abre esta sección; hoy solo SUPERADMIN lo
      // tiene por defecto (ver ROLE_DEFAULT_PERMISSIONS). No hay ruta
      // nueva ni dashboard nuevo: ADMIN y SUPERADMIN siguen compartiendo
      // /dashboard/admin y la diferencia es puramente de permiso.
      case 'administradores':
        return this.hasPermission('roles.gestionar');
      default:
        return false;
    }
  }

  puedeAccederTabConfig(tab: ConfigTab): boolean {
    switch (tab) {
      case 'general':
      case 'citas':
        return this.hasPermission('configuracion.gestionar');
      case 'horarios':
        return this.hasPermission('agenda.gestionar');
      case 'usuarios':
        return this.hasPermission('usuarios.gestionar');
      case 'seguridad':
        return this.hasPermission('auditoria.ver');
      default:
        return false;
    }
  }

  private asegurarTabConfigPermitida(): void {
    if (this.puedeAccederTabConfig(this.configTabActiva)) return;

    const primeraPermitida: ConfigTab | undefined = (
      ['general', 'citas', 'horarios', 'usuarios', 'seguridad'] as ConfigTab[]
    ).find(tab => this.puedeAccederTabConfig(tab));

    if (primeraPermitida) this.configTabActiva = primeraPermitida;
  }

  contextoAdministrativoCargando = true;
  contextoAdministrativoListo = false;

  private limpiarDatosVisiblesDeAccesoAnterior(): void {
    // Si cambia el rol/perfil durante una sesion, no dejamos datos
    // privilegiados anteriores visibles mientras llega el nuevo contexto.
    this.estadisticas = {
      reservas_hoy: 0,
      profesionales_activos: 0,
      horas_disponibles: 0,
      urgentes: 0
    };

    this.resumenDia = [];
    this.actividadReciente = [];
    this.proximasCitas = [];
    this.graficoEspecialidad = [];
    this.graficoSemana = [];

    this.profesionales = [];

    this.notificaciones = [];
    this.notifNoLeidas = 0;
    this.notifPanelAbierto = false;

    this.busquedaGlobal = '';
    this.resultadosBusquedaGlobal = {
      profesionales: [],
      estudiantes: []
    };
    this.buscandoGlobal = false;

    this.estudiantesAdmin = [];
    this.estudiantesTotal = 0;
    this.estudiantesCargando = false;
    this.fichaEstudianteSeleccionado = null;
    this.modalPerfilEstudianteAbierto = false;

    this.historialAdmin = [];
    this.estadisticasEstudiante = null;

    // Si el contexto efectivo cambia mientras había una acción de Agenda
    // abierta, cerramos cualquier superficie de mutación antes de volver a
    // evaluar permisos. Así un modal antiguo no sobrevive a una degradación
    // de permisos durante la misma sesión.
    this.modalCitaAbierto = false;
    this.sobrecupoConfirmAbierto = false;
    this.sobrecupoPendiente = null;

    // A.2B.2: cualquier cambio de contexto invalida también la agenda
    // operacional que pudo haberse cargado con permisos/scope anteriores.
    this.citasHorario = [];
    this.invalidarDisponibilidadRango();
  }

  private cargarContextoAdministrativoEfectivo(): void {
    this.contextoAdministrativoCargando = true;
    this.contextoAdministrativoListo = false;

    this.limpiarDatosVisiblesDeAccesoAnterior();

    this.auth.cargarAccesoAdministrativo().subscribe({
      next: () => {
        this.contextoAdministrativoCargando = false;
        this.contextoAdministrativoListo = true;

        // El backend acaba de confirmar rol, perfil, permisos y alcance.
        // Si una degradacion deja la seccion actual fuera de alcance,
        // volvemos a Inicio sin intentar cargarla.
        if (!this.puedeAccederSeccion(this.seccionActiva)) {
          this.seccionActiva = 'inicio';
        }

        if (this.seccionActiva === 'configuracion') {
          this.asegurarTabConfigPermitida();
        }

        // Solo ahora se permiten requests administrativos derivados
        // de permisos.
        this.cargarDatos();
        this.cdr.detectChanges();
      },

      error: () => {
        // AuthService ya borro su snapshot antes de la request y tambien
        // ante error. Por tanto hasPermission() permanece fail-closed.
        this.contextoAdministrativoCargando = false;
        this.contextoAdministrativoListo = false;

        this.mensajeError =
          'No se pudo cargar el acceso administrativo actual.';

        this.cdr.detectChanges();
      }
    });
  }

  ngOnInit(): void {
    const temaGuardado =
      localStorage.getItem('admin_tema_oscuro');

    if (temaGuardado === 'true') {
      this.temaOscuro = true;
    }

    // Esta preparacion no consulta informacion protegida.
    this.generarSemanaActual();

    // SA-10: no ejecutar cargarDatos() antes de conocer el contexto
    // efectivo actual del backend.
    this.cargarContextoAdministrativoEfectivo();
  }

  cargarDatos(): void {
    if (this.hasPermission('reportes.ver')) {
      this.cargarEstadisticas();
      this.cargarGraficoEspecialidad();
      this.cargarGraficoSemana();
      this.cargarHistorial();
    }

    if (this.hasPermission('agenda.ver')) {
      this.cargarProximasCitas();
      this.cargarResumenDia();
      this.cargarDiasCerrados();
    }

    if (this.hasPermission('agenda.gestionar')) {
      this.cargarSolicitudesHorarioAdmin();
    }

    if (this.hasPermission('profesionales.ver')) {
      this.cargarProfesionales();
    } else if (this.hasPermission('agenda.ver')) {
      this.cargarProfesionalesAgenda();
    }

    if (this.hasPermission('usuarios.gestionar')) {
      this.cargarNotificaciones();
    }

    if (this.hasPermission('auditoria.ver')) {
      this.cargarActividadReciente();
    }

    this.cargarConfiguracionCentro();
  }

  onSesionAdministrativaActualizada(): void {
    // actualizarRolSesion() invalida el snapshot anterior en AuthService.
    // Volvemos a consultar backend antes de habilitar cualquier capacidad.
    this.cargarContextoAdministrativoEfectivo();
  }

  navegarA(seccion: string): void {
  if (!this.puedeAccederSeccion(seccion)) {
    this.toast.error('No tienes permisos para acceder a esta sección.');
    return;
  }

  this.seccionActiva = seccion;
  this.mensajeExito  = '';
  this.mensajeError  = '';
  this.notifPanelAbierto = false;
  this.sidebarMovilAbierta = false;
  this.busquedaGlobal = '';
  this.resultadosBusquedaGlobal = { profesionales: [], estudiantes: [] };

  if (seccion === 'configuracion') {
    this.asegurarTabConfigPermitida();
    if (this.puedeConfigGeneral) this.cargarConfiguracionCentro();
    if (this.puedeConfigCitas) this.cargarConfiguracionCitas();
    if (this.puedeConfigHorarios) this.cargarDiasCerrados();
    if (this.puedeVerAuditoria) this.cargarAuditoria();
  }

  if (seccion === 'estudiantes' && this.hasPermission('usuarios.ver')) {
    this.cargarEstudiantes();
  }

  if (seccion === 'reportes' && this.hasPermission('reportes.ver')) {
    this.cargarEstadisticas();
    this.cargarGraficoEspecialidad();
    this.cargarGraficoSemana();
    this.cargarHistorial();
  }

  if (seccion === 'miperfil') this.cargarMiPerfil();
}

  // ══════════════════════════════════════
  // BÚSQUEDA GLOBAL (topbar)
  // ══════════════════════════════════════
  busquedaGlobal = '';
  resultadosBusquedaGlobal: { profesionales: any[]; estudiantes: any[] } = { profesionales: [], estudiantes: [] };
  buscandoGlobal = false;

  get hayResultadosBusquedaGlobal(): boolean {
    return this.resultadosBusquedaGlobal.profesionales.length > 0 || this.resultadosBusquedaGlobal.estudiantes.length > 0;
  }

  buscarGlobal(): void {
    const q = this.busquedaGlobal.trim().toLowerCase();
    if (q.length < 2) { this.resultadosBusquedaGlobal = { profesionales: [], estudiantes: [] }; return; }
    const profs = this.hasPermission('profesionales.ver')
      ? this.profesionales.filter(p =>
          p.nombre?.toLowerCase().includes(q) || p.especialidad?.toLowerCase().includes(q)
        ).slice(0, 5)
      : [];

    this.resultadosBusquedaGlobal = { profesionales: profs, estudiantes: [] };

    if (!this.hasPermission('usuarios.ver')) {
      this.buscandoGlobal = false;
      return;
    }

    this.buscandoGlobal = true;
    this.http.get<any[]>(`${API}/admin/estudiantes?q=${encodeURIComponent(q)}`).subscribe({
      next: (data) => {
        this.resultadosBusquedaGlobal = { profesionales: profs, estudiantes: (data ?? []).slice(0, 5) };
        this.buscandoGlobal = false;
        this.cdr.detectChanges();
      },
      error: () => { this.buscandoGlobal = false; }
    });
  }

  irAProfesionalDesdeBusqueda(p: any): void {
    if (!this.hasPermission('profesionales.ver')) return;
    this.busquedaGlobal = '';
    this.resultadosBusquedaGlobal = { profesionales: [], estudiantes: [] };
    this.navegarA('profesional');
    // La búsqueda local de la tabla ahora vive en AdminProfesionalesComponent;
    // se espera al próximo tick para que el *ngIf del hijo ya lo haya creado.
    setTimeout(() => {
      if (this.adminProfesionalesRef) this.adminProfesionalesRef.busquedaProfesional = p.nombre;
    }, 0);
  }

  irAEstudianteDesdeBusqueda(e: any): void {
    if (!this.hasPermission('usuarios.ver')) return;
    this.busquedaGlobal = '';
    this.resultadosBusquedaGlobal = { profesionales: [], estudiantes: [] };
    this.navegarA('estudiantes');
    setTimeout(() => this.verPerfilEstudianteAdmin(e), 0);
  }

  cerrarSesion(): void { this.auth.logout(); this.router.navigate(['/login']); }

  toggleTema(): void {
    this.temaOscuro = !this.temaOscuro;
    localStorage.setItem('admin_tema_oscuro', String(this.temaOscuro));
  }

  minVal(a: number, b: number): number { return Math.min(a, b); }

  formatearFecha(fecha: string): string {
    if (!fecha) return '';
    const [anio, mes, dia] = fecha.split('-').map(Number);
    const meses = ['Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre'];
    const diasN = ['Domingo','Lunes','Martes','Miércoles','Jueves','Viernes','Sábado'];
    const d = new Date(anio, mes-1, dia);
    return `${diasN[d.getDay()]} ${dia} de ${meses[mes-1]}`;
  }

  private convertirA24h(hora: string): string {
    if (!hora) return '';
    if (!hora.includes('AM') && !hora.includes('PM')) return hora.substring(0,5);
    const [time, period] = hora.trim().split(' ');
    let [h, m] = time.split(':').map(Number);
    if (period === 'PM' && h !== 12) h += 12;
    if (period === 'AM' && h === 12) h = 0;
    return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}`;
  }

  // ══════════════════════════════════════
  // NOTIFICACIONES ADMIN — selección múltiple
  // ══════════════════════════════════════

  notificaciones: any[] = [];
  notifPanelAbierto     = false;
  notifNoLeidas         = 0;

  cargarNotificaciones(): void {
    this.http.get<any[]>(`${API}/admin/notificaciones`).subscribe({
      next: (data) => {
        this.notificaciones = (data ?? []).map(n => ({ ...n, seleccionada: false }));
        this.notifNoLeidas  = this.notificaciones.filter(n => !n.leida).length;
        this.cdr.detectChanges();
      },
      error: () => {}
    });
  }

  toggleNotificaciones(): void { this.notifPanelAbierto = !this.notifPanelAbierto; }

  marcarLeida(n: any): void {
    if (n.leida) return;
    this.http.patch(`${API}/notificaciones/${n.id}/leer`, {}).subscribe({
      next: () => {
        n.leida = true;
        this.notifNoLeidas = Math.max(0, this.notifNoLeidas - 1);
        this.cdr.detectChanges();
      }
    });
  }

  marcarTodasLeidas(): void {
    const usuarioId = this.auth.getUsuarioId();
    if (usuarioId === null) return;

    this.http.patch(`${API}/notificaciones/leer-todas/${usuarioId}`, {}).subscribe({
      next: () => {
        this.notificaciones.forEach(n => n.leida = true);
        this.notifNoLeidas = 0;
        this.cdr.detectChanges();
      }
    });
  }

  get notifSeleccionadas(): any[] { return this.notificaciones.filter(n => n.seleccionada); }
  get hayNotifSeleccionadas(): boolean { return this.notifSeleccionadas.length > 0; }
  get todasNotifSeleccionadas(): boolean {
    return this.notificaciones.length > 0 && this.notificaciones.every(n => n.seleccionada);
  }

  toggleSeleccionarTodasNotif(): void {
    const nuevoValor = !this.todasNotifSeleccionadas;
    this.notificaciones.forEach(n => n.seleccionada = nuevoValor);
  }

  marcarSeleccionadasLeidas(): void {
    const seleccionadas = this.notifSeleccionadas.filter(n => !n.leida);
    if (!seleccionadas.length) return;
    seleccionadas.forEach(n => {
      this.http.patch(`${API}/notificaciones/${n.id}/leer`, {}).subscribe({
        next: () => {
          n.leida = true;
          n.seleccionada = false;
          this.notifNoLeidas = Math.max(0, this.notifNoLeidas - 1);
          this.cdr.detectChanges();
        }
      });
    });
  }

  eliminarNotificacion(n: any): void {
    this.http.delete(`${API}/notificaciones/${n.id}`).subscribe({
      next: () => {
        this.notificaciones = this.notificaciones.filter(x => x.id !== n.id);
        this.cdr.detectChanges();
      }
    });
  }

  eliminarNotifSeleccionadas(): void {
    const seleccionadas = this.notifSeleccionadas;
    if (!seleccionadas.length) return;
    if (!confirm(`¿Eliminar ${seleccionadas.length} notificación(es) seleccionada(s)?`)) return;
    seleccionadas.forEach(n => {
      this.http.delete(`${API}/notificaciones/${n.id}`).subscribe({
        next: () => {
          if (!n.leida) this.notifNoLeidas = Math.max(0, this.notifNoLeidas - 1);
          this.notificaciones = this.notificaciones.filter(x => x.id !== n.id);
          this.cdr.detectChanges();
        },
        error: (err) => { this.mensajeError = err?.error?.detail || 'No se pudo eliminar una de las notificaciones.'; setTimeout(() => this.mensajeError = '', 3000); }
      });
    });
  }

  // ══════════════════════════════════════
  // ESTADÍSTICAS
  // ══════════════════════════════════════

  estadisticas = { reservas_hoy: 0, profesionales_activos: 0, horas_disponibles: 0, urgentes: 0 };

  cargarEstadisticas(): void {
    this.http.get<any>(`${API}/admin/estadisticas`).subscribe({
      next: (data) => {
        this.estadisticas = data;
        this.cdr.detectChanges();
      },
      error: () => {}
    });
  }

  // ══════════════════════════════════════
  // DISPONIBILIDAD HOY
  // ══════════════════════════════════════

  resumenDia: any[] = [];
  actualizandoDisponibilidad = false;

  cargarResumenDia(): void {
    if (!this.hasPermission('agenda.ver')) {
      this.resumenDia = [];
      this.actualizandoDisponibilidad = false;
      return;
    }
    this.actualizandoDisponibilidad = true;
    this.http.get<any[]>(`${API}/admin/resumen-dia`).subscribe({
      next: (data) => {
        this.resumenDia = data ?? [];
        this.actualizandoDisponibilidad = false;
        this.cdr.detectChanges();
      },
      error: () => {
        this.actualizandoDisponibilidad = false;
        this.cdr.detectChanges();
      }
    });
  }


  // ══════════════════════════════════════
  // ACTIVIDAD RECIENTE
  // ══════════════════════════════════════

  actividadReciente: any[] = [];

  cargarActividadReciente(): void {
    if (!this.hasPermission('auditoria.ver')) {
      this.actividadReciente = [];
      return;
    }

    this.http.get<any[]>(`${API}/admin/auditoria`).subscribe({
      next: (data) => {
        this.actividadReciente = (data ?? []).slice(0,5).map(a => ({
          mensaje: a.accion + (a.detalle ? ': ' + a.detalle : ''),
          tiempo:  this.tiempoRelativo(a.fecha),
          tipo:    this.tipoAuditoria(a.accion)
        }));
        this.cdr.detectChanges();
      },
      error: () => { this.actividadReciente = []; }
    });
  }

  private tiempoRelativo(fechaStr: string): string {
    if (!fechaStr) return '';
    const diff = Date.now() - new Date(fechaStr).getTime();
    const min  = Math.floor(diff / 60000);
    if (min < 1)  return 'Hace un momento';
    if (min < 60) return `Hace ${min} min`;
    const h = Math.floor(min / 60);
    if (h < 24)  return `Hace ${h} h`;
    return `Hace ${Math.floor(h/24)} días`;
  }

  private tipoAuditoria(accion: string): string {
    if (!accion) return 'info';
    const a = accion.toLowerCase();
    if (a.includes('cancel')) return 'cancelacion';
    if (a.includes('complet')) return 'completada';
    return 'info';
  }

  // ══════════════════════════════════════
  // PRÓXIMAS CITAS
  // ══════════════════════════════════════

  proximasCitas: any[] = [];

  cargarProximasCitas(): void {
    if (!this.hasPermission('agenda.ver')) {
      this.proximasCitas = [];
      return;
    }
    this.http.get<any[]>(`${API}/admin/proximas-citas`).subscribe({
      next: (data) => {
        this.proximasCitas = data ?? [];
        this.cdr.detectChanges();
      },
      error: () => {}
    });
  }

  marcarInasistencia(cita: any): void {
    if (!this.hasPermission('agenda.gestionar')) return;
    if (!confirm(`¿Marcar inasistencia del estudiante ${cita.estudiante}?`)) return;
    this.http.patch(`${API}/admin/citas/${cita.id}/cancelar`, { motivo: 'inasistencia' }).subscribe({
      next: () => {
        this.proximasCitas = this.proximasCitas.filter(c => c.id !== cita.id);
        const citaH = this.citasHorario.find(c => c.id === cita.id);
        if (citaH) citaH.estado = 'inasistencia';
        this.mensajeExito = 'Inasistencia registrada.';
        setTimeout(() => this.mensajeExito = '', 3000);
        this.cdr.detectChanges();
      },
      error: () => { this.mensajeError = 'Error al registrar inasistencia.'; setTimeout(() => this.mensajeError = '', 3000); }
    });
  }

  cancelarCitaAdmin(cita: any): void {
    if (!this.hasPermission('agenda.gestionar')) return;
    if (!confirm(`¿Cancelar la cita de ${cita.estudiante}?`)) return;
    this.http.patch(`${API}/admin/citas/${cita.id}/cancelar`, { motivo: 'Cancelada por administrador' }).subscribe({
      next: () => {
        this.proximasCitas = this.proximasCitas.filter(c => c.id !== cita.id);
        // Se actualiza el estado en el mismo lugar (no se elimina), para que el panel
        // de Horario siga mostrando la cita con su estado real en vez de hacerla desaparecer.
        const citaH = this.citasHorario.find(c => c.id === cita.id);
        if (citaH) citaH.estado = 'cancelada';
        this.mensajeExito = 'Cita cancelada. El estudiante fue notificado.';
        setTimeout(() => this.mensajeExito = '', 3000);
        this.cdr.detectChanges();
      },
      error: () => { this.mensajeError = 'Error al cancelar cita.'; setTimeout(() => this.mensajeError = '', 3000); }
    });
  }

  // ══════════════════════════════════════
  // GRÁFICOS
  // El shell conserva datos/HTTP/exportación porque graficoEspecialidad
  // también alimenta Reportes. La renderización Chart.js vive en Inicio.
  // ══════════════════════════════════════

  graficoEspecialidad: any[] = [];
  graficoSemana: any[] = [];
  filtroGraficoMes = '';
  filtroGraficoAnio = new Date().getFullYear();
  filtroGraficoCarrera = '';

  cargarGraficoEspecialidad(): void {
    let url = `${API}/admin/graficos/especialidad?anio=${this.filtroGraficoAnio}`;
    if (this.filtroGraficoMes) {
      url += `&mes=${this.filtroGraficoMes}`;
    }
    if (this.filtroGraficoCarrera) {
      url += `&carrera=${encodeURIComponent(this.filtroGraficoCarrera)}`;
    }

    this.http.get<any[]>(url).subscribe({
      next: (data) => {
        this.graficoEspecialidad = data ?? [];
        this.cdr.detectChanges();
      },
      error: () => {}
    });
  }

  cargarGraficoSemana(): void {
    this.http.get<any[]>(`${API}/admin/graficos/semana`).subscribe({
      next: (data) => {
        this.graficoSemana = data ?? [];
        this.cdr.detectChanges();
      },
      error: () => {}
    });
  }

  exportarEspecialidadExcel(): void {
    this.exportarComoXlsx(
      this.graficoEspecialidad.map(d => ({
        Especialidad: d.especialidad,
        Cantidad: d.cantidad,
        Porcentaje: d.porcentaje + '%'
      })),
      'citas_por_especialidad',
      'Citas por especialidad'
    );
  }

  exportarSemanaExcel(): void {
    this.exportarComoXlsx(
      this.graficoSemana.map(d => ({
        Día: d.dia,
        Fecha: d.fecha,
        Cantidad: d.cantidad
      })),
      'citas_por_semana',
      'Citas por semana'
    );
  }
  // ══════════════════════════════════════
  // EXPORTACIÓN CGR
  // ══════════════════════════════════════

  exportarCGR2025(): void {
    if (!this.puedeExportarCgr) return;
    window.open(`${API}/admin/exportar/cgr?anio=2025`, '_blank');
  }

  exportarCGR2026(): void {
    if (!this.puedeExportarCgr) return;
    window.open(`${API}/admin/exportar/cgr?anio=2026&fecha_fin=2026-05-31`, '_blank');
  }

  // Selector de año dinámico para CGR (reemplaza los botones fijos 2025/2026)
  cgrAnio     = new Date().getFullYear();
  cgrFechaFin = '';

  get cgrAniosDisponibles(): number[] {
    const actual = new Date().getFullYear();
    const anios: number[] = [];
    for (let a = actual + 1; a >= actual - 3; a--) anios.push(a);
    return anios;
  }

  exportarCGR(): void {
    if (!this.puedeExportarCgr) return;

    let url = `${API}/admin/exportar/cgr?anio=${this.cgrAnio}`;
    if (this.cgrFechaFin) url += `&fecha_fin=${this.cgrFechaFin}`;
    window.open(url, '_blank');
  }
  exportarListadoAlumnos(): void {
    if (!this.puedeExportarCgr) return;
    window.open(`${API}/admin/exportar/alumnos`, '_blank');
  }

  // ══════════════════════════════════════
  // HORARIO
  // ══════════════════════════════════════
  readonly horarioBloqueEstadoFn = (fecha: string, hora: string): string =>
    this.getBloqueEstado(fecha, hora);

  readonly horarioBloqueInfoFn = (fecha: string, hora: string): string =>
    this.getBloqueInfo(fecha, hora);

  // A.4.7A.1 — la grilla ahora consume esto en vez de horarioBloqueInfoFn
  // para poder renderizar más de una cita por celda.
  readonly horarioBloqueCitasFn = (fecha: string, hora: string): any[] =>
    this.getBloqueCitas(fecha, hora);

  // A.4.7A — affordance visual: el dominio sigue diciendo 'ocupado'
  // (getBloqueEstado no cambia), esta función solo informa si ADEMÁS
  // existe la posibilidad de un sobrecupo real sobre ese slot ocupado.
  readonly horarioBloqueSobrecupoDisponibleFn = (fecha: string, hora: string): boolean =>
    this.puedeSolicitarSobrecupo(fecha, hora);

  readonly horarioFormatearFechaFn = (fecha: string): string =>
    this.formatearFecha(fecha);

  readonly horarioEsFeriadoFn = (fecha: string | undefined): boolean =>
    this.esFeriado(fecha);

  readonly horarioNombreFeriadoFn = (fecha: string | undefined): string =>
    this.nombreFeriado(fecha);

  semanaActual:    any[]         = [];
  diaSeleccionado: string | null = null;
  filtroProfesionalId            = '';
  filtroEspecialidad             = '';
  semanaLabel                    = '';
  modalCitaAbierto               = false;

  // Horario general de atención del centro — la grilla siempre muestra el
  // día completo, sin importar el horario particular de cada profesional.
  // Las horas fuera del horario propio del profesional se muestran en gris
  // (ver getBloqueEstado) pero el admin puede forzar una cita ahí igual,
  // quedando marcada como "sobrecupo".
  private readonly CENTRO_HORA_INICIO = '08:00';
  private readonly CENTRO_HORA_FIN    = '18:00';

  get horasGrilla(): string[] {
    const prof = this.profesionales.find(p => String(p.id) === String(this.filtroProfesionalId));
    const duracion = prof?.duracion_min || 60;
    const [hIni, mIni] = this.CENTRO_HORA_INICIO.split(':').map(Number);
    const [hFin, mFin] = this.CENTRO_HORA_FIN.split(':').map(Number);
    const horas: string[] = [];
    let minutos = hIni * 60 + mIni;
    const finMin = hFin * 60 + mFin;
    while (minutos < finMin) {
      const h = Math.floor(minutos / 60).toString().padStart(2,'0');
      const m = (minutos % 60).toString().padStart(2,'0');
      horas.push(`${h}:${m}`);
      minutos += duracion;
    }
    return horas;
  }

  // Profesional actualmente seleccionado en el filtro de Horario
  get profesionalActual(): any {
    return this.profesionales.find(p => String(p.id) === String(this.filtroProfesionalId)) ?? null;
  }

  // Si el profesional no está activo (licencia/inasistencia), su agenda entera se muestra bloqueada
  get profesionalActualBloqueado(): boolean {
    const p = this.profesionalActual;
    return !!p && p.estado && p.estado !== 'activo';
  }

  get citasHorarioAgenda(): any[] {
    return this.citasHorario;
  }

  private citasHorario: any[] = [];

  // A.2B.2 — disponibilidad de Semana. El backend es la única fuente de
  // verdad para decidir si un slot sin cita está disponible o bloqueado.
  // Se indexa por fecha+hora para lookup O(1) desde la grilla.
  private disponibilidadPorFecha: Record<string, Record<string, SlotDisponibilidadBackend>> = {};
  disponibilidadCargando = false;
  disponibilidadError = false;
  private disponibilidadRequestId = 0;

  busquedaEstudiante          = '';
  resultadosEstudiante: any[] = [];
  estudianteSeleccionado: any = null;
  buscandoEstudiante          = false;

  get profesionalesFiltrados(): any[] {
    return this.filtroEspecialidad
      ? this.profesionales.filter(p => p.especialidad === this.filtroEspecialidad)
      : this.profesionales;
  }

  filtrarProfesionales(): void {
    this.filtroProfesionalId = '';
    this.diaSeleccionado = null;
    this.citasHorario = [];
    this.invalidarDisponibilidadRango();
  }

  private invalidarDisponibilidadRango(): number {
    this.disponibilidadRequestId += 1;
    this.disponibilidadPorFecha = {};
    this.disponibilidadCargando = false;
    this.disponibilidadError = false;
    return this.disponibilidadRequestId;
  }

  private indexarDisponibilidadRango(
    respuesta: RespuestaDisponibilidadRango
  ): Record<string, Record<string, SlotDisponibilidadBackend>> {
    const mapa: Record<string, Record<string, SlotDisponibilidadBackend>> = {};

    for (const dia of respuesta.dias ?? []) {
      const fecha = String(dia?.fecha ?? '');
      if (!fecha || !Array.isArray(dia?.slots)) continue;

      const slots: Record<string, SlotDisponibilidadBackend> = {};
      for (const slot of dia.slots) {
        const hora = String(slot?.hora ?? '').substring(0, 5);
        if (!hora) continue;
        slots[hora] = slot;
      }
      mapa[fecha] = slots;
    }

    return mapa;
  }

  private respuestaDisponibilidadCorresponde(
    respuesta: RespuestaDisponibilidadRango | null | undefined,
    profesionalId: number,
    fechaInicio: string,
    fechaFin: string
  ): respuesta is RespuestaDisponibilidadRango {
    return !!respuesta
      && Number(respuesta.profesional_id) === profesionalId
      && respuesta.fecha_inicio === fechaInicio
      && respuesta.fecha_fin === fechaFin
      && Array.isArray(respuesta.dias);
  }

  cargarHorarioProfesional(): void {
    const requestId = this.invalidarDisponibilidadRango();
    this.citasHorario = [];

    if (!this.hasPermission('agenda.ver')) return;

    const profesionalId = Number(this.filtroProfesionalId);
    const fechaInicio = String(this.semanaActual[0]?.fecha ?? '');
    const fechaFin = String(this.semanaActual[this.semanaActual.length - 1]?.fecha ?? '');

    if (!Number.isInteger(profesionalId) || profesionalId <= 0 || !fechaInicio || !fechaFin) return;

    // Citas reales y disponibilidad se consultan una vez por selección/rango.
    // El mismo token evita que respuestas de un profesional/semana anterior
    // vuelvan a pintar la grilla después de una navegación rápida.
    this.http.get<any[]>(`${API}/agenda/profesional/${profesionalId}/citas`).subscribe({
      next: (data) => {
        if (requestId !== this.disponibilidadRequestId) return;
        this.citasHorario = data ?? [];
        this.cdr.detectChanges();
      },
      error: () => {
        if (requestId !== this.disponibilidadRequestId) return;
        this.citasHorario = [];
        this.cdr.detectChanges();
      }
    });

    this.disponibilidadCargando = true;
    const url = `${API}/agenda/profesional/${profesionalId}/disponibilidad`
      + `?fecha_inicio=${encodeURIComponent(fechaInicio)}`
      + `&fecha_fin=${encodeURIComponent(fechaFin)}`;

    this.http.get<RespuestaDisponibilidadRango>(url).subscribe({
      next: (respuesta) => {
        if (requestId !== this.disponibilidadRequestId) return;

        if (!this.respuestaDisponibilidadCorresponde(
          respuesta,
          profesionalId,
          fechaInicio,
          fechaFin
        )) {
          this.disponibilidadPorFecha = {};
          this.disponibilidadCargando = false;
          this.disponibilidadError = true;
          this.cdr.detectChanges();
          return;
        }

        this.disponibilidadPorFecha = this.indexarDisponibilidadRango(respuesta);
        this.disponibilidadCargando = false;
        this.disponibilidadError = false;
        this.cdr.detectChanges();
      },
      error: () => {
        if (requestId !== this.disponibilidadRequestId) return;
        this.disponibilidadPorFecha = {};
        this.disponibilidadCargando = false;
        this.disponibilidadError = true;
        this.cdr.detectChanges();
      }
    });
  }

  generarSemanaActual(): void {
    const hoy = new Date();
    const lunes = new Date(hoy);
    lunes.setDate(hoy.getDate() - ((hoy.getDay() + 6) % 7));
    this.buildSemana(lunes);
  }

  private buildSemana(lunes: Date): void {
    const nombres = ['Lun','Mar','Mié','Jue','Vie','Sáb','Dom'];
    const hoyStr  = this.toDateStr(new Date());
    this.semanaActual = Array.from({ length: 7 }, (_, i) => {
      const d = new Date(lunes); d.setDate(lunes.getDate() + i);
      const f = this.toDateStr(d);
      return { nombre: nombres[i], num: d.getDate(), fecha: f, esHoy: f === hoyStr };
    });

    // Corrección A.2B v2, punto 1: si el día previamente seleccionado ya no
    // pertenece a la semana recién construida (navegación de semana), se
    // limpia — de lo contrario el panel contextual seguía mostrando un día
    // de la semana anterior aunque ya no fuera visible en la grilla.
    if (this.diaSeleccionado && !this.semanaActual.some(d => d.fecha === this.diaSeleccionado)) {
      this.diaSeleccionado = null;
    }

    const mesesN = ['Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre'];
    const inicio = this.parseDateStrLocal(this.semanaActual[0].fecha);
    const fin = this.parseDateStrLocal(this.semanaActual[6].fecha);
    const mesInicio = mesesN[inicio.getMonth()];
    const mesFin = mesesN[fin.getMonth()];

    if (inicio.getFullYear() === fin.getFullYear() && inicio.getMonth() === fin.getMonth()) {
      this.semanaLabel = `Semana ${inicio.getDate()} – ${fin.getDate()} ${mesInicio} ${inicio.getFullYear()}`;
    } else if (inicio.getFullYear() === fin.getFullYear()) {
      this.semanaLabel = `Semana ${inicio.getDate()} ${mesInicio} – ${fin.getDate()} ${mesFin} ${inicio.getFullYear()}`;
    } else {
      this.semanaLabel = `Semana ${inicio.getDate()} ${mesInicio} ${inicio.getFullYear()} – ${fin.getDate()} ${mesFin} ${fin.getFullYear()}`;
    }
  }

  private toDateStr(d: Date): string {
    return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
  }

  // Convierte "YYYY-MM-DD" a Date usando la zona horaria LOCAL, no UTC.
  // new Date("YYYY-MM-DD") se interpreta como medianoche UTC y al convertir
  // a hora de Chile retrocede un día — por eso NUNCA se debe usar así.
  private parseDateStrLocal(fechaStr: string): Date {
    const [anio, mes, dia] = fechaStr.split('-').map(Number);
    return new Date(anio, mes - 1, dia);
  }

  semanaAnterior(): void {
    const lunes = this.parseDateStrLocal(this.semanaActual[0].fecha); lunes.setDate(lunes.getDate() - 7);
    this.buildSemana(lunes); if (this.filtroProfesionalId) this.cargarHorarioProfesional();
  }

  semanaSiguiente(): void {
    const lunes = this.parseDateStrLocal(this.semanaActual[0].fecha); lunes.setDate(lunes.getDate() + 7);
    this.buildSemana(lunes); if (this.filtroProfesionalId) this.cargarHorarioProfesional();
  }

  irAHoy(): void { this.generarSemanaActual(); if (this.filtroProfesionalId) this.cargarHorarioProfesional(); }

// ¿La hora indicada cae dentro del horario de almuerzo del profesional actual?
  private esHoraDeAlmuerzo(hora: string): boolean {
    const prof = this.profesionalActual;
    if (!prof || !prof.hora_almuerzo_inicio || !prof.hora_almuerzo_fin) return false;
    const h = hora.substring(0, 5);
    return h >= prof.hora_almuerzo_inicio && h < prof.hora_almuerzo_fin;
  }

  // ¿La hora indicada cae fuera del horario declarado por el profesional?
  // (pero SÍ dentro del horario general del centro, por eso igual aparece
  // en la grilla — solo que en gris, y solo agendable por el admin como sobrecupo)
  private esFueraDeHorarioProfesional(hora: string): boolean {
    const prof = this.profesionalActual;
    if (!prof) return false;
    const inicio = prof.horario_inicio || this.CENTRO_HORA_INICIO;
    const fin    = prof.horario_fin    || this.CENTRO_HORA_FIN;
    const h = hora.substring(0, 5);
    return h < inicio || h >= fin;
  }

  // A.4.7A.1 — orden determinista de presentación dentro de un mismo
  // bloque (no depende del orden en que backend entregue el arreglo).
  // Regla explícita, documentada, y aplicada SIEMPRE — nunca inferida
  // de la posición de llegada:
  //   1) urgente primero — es la prioridad clínica más alta; ya era el
  //      estado de mayor prioridad visual en getBloqueEstado() para un
  //      único slot (ver arriba), y se mantiene consistente al listar
  //      varias citas del mismo bloque.
  //   2) normal (ni urgente ni sobrecupo) a continuación.
  //   3) sobrecupo al final — es la cita añadida POR SOBRE la capacidad
  //      normal del slot, por eso se lista después de lo que ya ocupaba
  //      el cupo original.
  // urgente y sobrecupo son mutuamente excluyentes por regla de negocio
  // ya existente (mostrarToggleUrgente / crearCitaDesdeHorario), así
  // que 1) y 3) nunca compiten por la misma cita.
  // Desempate (dos citas del mismo rango, p. ej. dos normales en el
  // mismo slot — caso ya soportado por backend): por id ascendente: es
  // un valor estable e independiente del orden de llegada del arreglo.
  // Si faltara id, por nombre de estudiante — nunca por posición.
  private rangoOrdenCita(c: any): number {
    if (c?.urgente)   return 0;
    if (c?.sobrecupo) return 2;
    return 1;
  }

  private compararCitasBloque(a: any, b: any): number {
    const diffRango = this.rangoOrdenCita(a) - this.rangoOrdenCita(b);
    if (diffRango !== 0) return diffRango;

    const idA = Number(a?.id);
    const idB = Number(b?.id);
    if (Number.isFinite(idA) && Number.isFinite(idB) && idA !== idB) {
      return idA - idB;
    }
    return String(a?.estudiante ?? '').localeCompare(String(b?.estudiante ?? ''));
  }

  // A.4.7A.1 — antes este helper usaba Array.find() y devolvía solo la
  // PRIMERA cita que calzara con fecha+hora. Con soporte de sobrecupo
  // real (A.4.4), un mismo slot puede tener legítimamente más de una
  // cita operativa (p. ej. una normal + una sobrecupo forzada encima).
  // find() descartaba silenciosamente todo lo que no fuera la primera,
  // por lo que la grilla semanal solo pintaba una de las dos aunque el
  // panel contextual (que sí usa filter() sobre citasHorario) mostrara
  // ambas correctamente. filter() es ahora la única fuente de verdad
  // para "qué hay en este bloque", igual que ya lo era para el panel;
  // el .sort() aplica el orden determinista de arriba, así que el
  // resultado NUNCA depende del orden en que backend entregó el
  // arreglo original.
  private buscarCitasEnBloque(fecha: string, hora: string): any[] {
    return this.citasHorario
      .filter(c => {
        if (c.fecha !== fecha) return false;
        if (c.estado === 'cancelada' || c.estado === 'inasistencia') return false;
        return this.convertirA24h(c.hora) === hora.substring(0,5);
      })
      .sort((a, b) => this.compararCitasBloque(a, b));
  }

  esDiaCerrado(fecha: string): boolean {
    return this.diasCerrados.some(d => d.fecha === fecha);
  }

  private buscarDisponibilidadEnBloque(
    fecha: string,
    hora: string
  ): SlotDisponibilidadBackend | null {
    return this.disponibilidadPorFecha[fecha]?.[hora.substring(0, 5)] ?? null;
  }

  private claseVisualParaMotivo(slot: SlotDisponibilidadBackend): string {
    if (slot.disponible) return 'disponible';

    switch (slot.motivo) {
      case 'dia_cerrado':
      case 'fin_de_semana':
        return 'cerrado-centro';
      case 'en_colacion':
        return 'colacion';
      case 'fuera_de_jornada':
        return 'fuera-horario';
      case 'slot_ocupado':
        return 'ocupado';
      case 'profesional_inactivo':
      case 'fecha_pasada':
      case 'hora_pasada':
      case 'hora_fuera_de_grilla':
        return 'bloqueado';
      default:
        // Motivo desconocido o respuesta incompleta: no inferir disponibilidad.
        return 'sin-datos';
    }
  }

  getBloqueEstado(fecha: string, hora: string): string {
    // Una cita operacional real tiene prioridad visual. Así, si las dos
    // lecturas llegan en distinto orden, nunca se oculta una reserva real
    // detrás de un estado de disponibilidad.
    //
    // A.4.7A.1 — con más de una cita en el mismo bloque, la prioridad
    // (urgente > sobrecupo > ocupado) se evalúa sobre TODAS las citas,
    // no solo sobre la primera: si cualquiera de ellas es urgente o
    // sobrecupo, el bloque debe reflejarlo aunque la otra cita sea una
    // reserva normal.
    const citas = this.buscarCitasEnBloque(fecha, hora);
    if (citas.length > 0) {
      if (citas.some(c => c.urgente))   return 'urgente';
      if (citas.some(c => c.sobrecupo)) return 'sobrecupo';
      return 'ocupado';
    }

    const slot = this.buscarDisponibilidadEnBloque(fecha, hora);
    if (!slot) return 'sin-datos';

    return this.claseVisualParaMotivo(slot);
  }

  // A.4.7A.1 — fuente de verdad para el CONTENIDO del bloque (no solo su
  // color/estado). Reemplaza el uso de getBloqueInfo() en la grilla:
  // expone el arreglo completo de citas del slot para que la plantilla
  // pinte una línea por cada una, en vez de una sola cadena que solo
  // podía representar a la primera cita encontrada.
  getBloqueCitas(fecha: string, hora: string): any[] {
    return this.buscarCitasEnBloque(fecha, hora);
  }

  // Se mantiene por compatibilidad (otros consumidores/tests pueden
  // seguir llamándolo para obtener un resumen en texto plano), pero ya
  // no es lo que alimenta la grilla semanal: ahora concatena TODAS las
  // citas del bloque en vez de reportar solo la primera.
  getBloqueInfo(fecha: string, hora: string): string {
    const citas = this.buscarCitasEnBloque(fecha, hora);
    if (citas.length > 0) {
      return citas
        .map(c => c.sobrecupo ? `${c.estudiante} (Sobrecupo)` : c.estudiante)
        .join(' · ');
    }

    const slot = this.buscarDisponibilidadEnBloque(fecha, hora);
    if (slot?.motivo === 'en_colacion') return 'Colación';
    return '';
  }

  get citasDiaSeleccionado(): any[] {
    if (!this.diaSeleccionado) return [];
    return this.citasHorario.filter(c => c.fecha === this.diaSeleccionado);
  }

  // A.4.7A v2 — helper GENÉRICO de capacidad de sobrecupo, unificado para
  // los tres conflictos overridables reales (slot_ocupado, en_colacion,
  // fuera_de_jornada). El backend (evaluar_politica_sobrecupo, reglas
  // E/F) exige agenda.gestionar + agenda.sobrecupo para CUALQUIERA de
  // los tres, no solo para slot_ocupado — la v1 solo lo exigía para el
  // caso nuevo, dejando colación/fuera de jornada gateados solo por
  // agenda.gestionar en el resto de las capas.
  //
  // Para 'ocupado' en particular: el dominio de getBloqueEstado() sigue
  // devolviendo 'ocupado' cuando hay una Cita real (prioridad ya
  // existente, sin cambios) — por eso esta función consulta POR
  // SEPARADO disponibilidadPorFecha, para no perder el motivo/
  // overridable_con_sobrecupo que el early-return de Cita ocultaría si
  // se mirara solo getBloqueEstado().
  //
  // No duplica reglas de backend: solo lee overridable_con_sobrecupo
  // (ya calculado por backend) y verifica que el motivo real coincida
  // con el estado visual que el usuario está viendo, para no ofrecer
  // sobrecupo sobre un conflicto distinto del que se muestra en pantalla.
  puedeSolicitarSobrecupo(fecha: string, hora: string): boolean {
    if (!this.hasPermission('agenda.gestionar')) return false;
    if (!this.hasPermission('agenda.sobrecupo')) return false;

    const estado = this.getBloqueEstado(fecha, hora);
    if (estado !== 'ocupado' && estado !== 'colacion' && estado !== 'fuera-horario') return false;

    const slot = this.buscarDisponibilidadEnBloque(fecha, hora);
    if (!slot || slot.overridable_con_sobrecupo !== true) return false;

    if (estado === 'ocupado')       return slot.motivo === 'slot_ocupado';
    if (estado === 'colacion')      return slot.motivo === 'en_colacion';
    /* fuera-horario */             return slot.motivo === 'fuera_de_jornada';
  }

  // ══════════════════════════════════════
  // SOBRECUPO — forzar una cita fuera del horario habitual del profesional
  // ══════════════════════════════════════
  sobrecupoConfirmAbierto = false;
  sobrecupoPendiente: { fecha: string; hora: string; mensaje: string } | null = null;

  clickBloque(fecha: string, hora: string): void {
    const estado = this.getBloqueEstado(fecha, hora);

    // Corrección A.2B v2, punto 2: el panel contextual debe reflejar el día
    // en que se hizo clic aunque el bloque no sea agendable — por ejemplo
    // una cita histórica, un día cerrado o un slot ya pasado. Solo se omite
    // cuando ni siquiera hay datos de disponibilidad todavía ('sin-datos').
    if (estado !== 'sin-datos') {
      this.diaSeleccionado = fecha;
    }

    if (estado === 'sin-datos' || estado === 'bloqueado' || estado === 'cerrado-centro') return;

    // Un usuario con agenda.ver puede seleccionar
    // el dia, pero no abrir citas ni sobrecupos.
    if (!this.hasPermission('agenda.gestionar')) return;

    if (estado === 'disponible') {
      this.abrirModalNuevaCitaConFechaHora(fecha, hora, false);
    } else if (estado === 'colacion' || estado === 'fuera-horario') {
      // A.4.7A v2 — capa 2: ahora exige TAMBIÉN agenda.sobrecupo (antes
      // solo miraba overridable_con_sobrecupo, dejando pasar a cualquier
      // cuenta con agenda.gestionar).
      if (!this.puedeSolicitarSobrecupo(fecha, hora)) return;

      // A.4.7A v3 — el mensaje completo se arma acá (única fuente de
      // verdad), en vez de concatenar "fuera de {{motivoTexto}} ..." en
      // el template: esa construcción funcionaba para estos dos casos
      // pero no era generalizable a slot_ocupado sin producir una frase
      // gramaticalmente incorrecta (ver caso 'ocupado' más abajo).
      const profesionalNombre = this.profesionalActual?.nombre ?? 'el profesional';
      const fechaFormateada = this.formatearFecha(fecha);
      const mensaje = estado === 'colacion'
        ? `Estás agendando a las ${hora} del ${fechaFormateada}, durante la hora de colación de ${profesionalNombre}. Esta cita quedará marcada como sobrecupo. ¿Confirmas?`
        : `Estás agendando a las ${hora} del ${fechaFormateada}, fuera del horario habitual de ${profesionalNombre}. Esta cita quedará marcada como sobrecupo. ¿Confirmas?`;

      this.sobrecupoPendiente = { fecha, hora, mensaje };
      this.sobrecupoConfirmAbierto = true;
    } else if (estado === 'ocupado') {
      // A.4.7A — sobrecupo real sobre un slot ya ocupado (A.4.4). Capa 2
      // de la defensa en profundidad: puedeSolicitarSobrecupo() ya
      // exige agenda.gestionar + agenda.sobrecupo + que backend siga
      // informando ese slot como overridable en este mismo instante.
      if (!this.puedeSolicitarSobrecupo(fecha, hora)) return;

      // A.4.7A v3 — para slot_ocupado la oración "fuera de un cupo ya
      // ocupado en la agenda de X" no es correcta ni neutral: acá NO
      // hay un horario "fuera de jornada", hay ocupación dentro de la
      // jornada normal. Mensaje propio, sin afirmar cardinalidad (no se
      // dice "una cita", "un cupo" ni "dos citas" — el contrato del
      // frontend no expone conteo, eso lo decide backend en A.4.4).
      const fechaFormateada = this.formatearFecha(fecha);
      const mensaje = `A las ${hora} del ${fechaFormateada}: este horario ya presenta ocupación. `
        + 'El sistema permite solicitar un sobrecupo. La disponibilidad volverá a validarse al confirmar. '
        + '¿Confirmas?';

      this.sobrecupoPendiente = { fecha, hora, mensaje };
      this.sobrecupoConfirmAbierto = true;
    }
  }

  cancelarSobrecupo(): void {
    this.sobrecupoConfirmAbierto = false;
    this.sobrecupoPendiente = null;
  }

  confirmarSobrecupo(): void {
    // A.4.7A — capa 3: agenda.sobrecupo es exigido por
    // evaluar_politica_sobrecupo() (backend) para CUALQUIER sobrecupo
    // efectivo, no solo el de slot ocupado — colación y fuera de
    // jornada también lo requieren desde A.4.3. Antes de este cambio
    // solo se validaba agenda.gestionar acá, dejando que una cuenta con
    // agenda.gestionar pero sin agenda.sobrecupo llegara hasta el modal
    // de cita para recién enterarse por un 403 del backend.
    if (!this.hasPermission('agenda.gestionar') || !this.hasPermission('agenda.sobrecupo')) {
      this.cancelarSobrecupo();
      return;
    }
    if (!this.sobrecupoPendiente) return;
    const { fecha, hora } = this.sobrecupoPendiente;
    this.sobrecupoConfirmAbierto = false;
    this.sobrecupoPendiente = null;
    this.abrirModalNuevaCitaConFechaHora(fecha, hora, true);
  }

  // Corrección A.2B v2, punto 3: una fecha histórica (ya pasada) nunca debe
  // abrir "Nueva cita" — ni siquiera si diaSeleccionado quedó apuntando a
  // ella (p. ej. el botón de la barra de herramientas usa diaSeleccionado
  // directamente, sin pasar por clickBloque()).
  private esFechaPasada(fecha: string): boolean {
    if (!fecha) return false;
    return fecha < this.toDateStr(new Date());
  }

  abrirModalNuevaCita(): void { this.abrirModalNuevaCitaConFechaHora(this.diaSeleccionado ?? '', '', false); }

  abrirModalNuevaCitaConFechaHora(fecha: string, hora: string, esSobrecupo: boolean = false): void {
    if (!this.hasPermission('agenda.gestionar')) return;
    // A.4.7A — capa 3b: si el flujo que abre este modal ya es de
    // sobrecupo (viene de confirmarSobrecupo()), re-verificar acá
    // también agenda.sobrecupo. Redundante con confirmarSobrecupo() por
    // diseño (defensa en profundidad, no un único punto de fallo).
    if (esSobrecupo && !this.hasPermission('agenda.sobrecupo')) return;
    if (this.profesionalActualBloqueado) return;
    if (this.esFechaPasada(fecha)) return;
    this.nuevaCita = {
      fecha, hora, estudiante_id: null, profesional_id: Number(this.filtroProfesionalId),
      observaciones: '', urgente: false, sobrecupo: esSobrecupo,
      // A.4.1/A.4.7A — motivo HUMANO del sobrecupo, campo separado de
      // observaciones (que es el motivo clínico de la consulta). Se
      // reinicia en cada apertura para que un motivo anterior nunca
      // quede reutilizado por accidente.
      sobrecupo_motivo: ''
    };
    this.busquedaEstudiante     = '';
    this.resultadosEstudiante   = [];
    this.estudianteSeleccionado = null;
    this.modalCitaAbierto       = true;
  }

  cerrarModalCita(): void {
    this.modalCitaAbierto = false;
    this.busquedaEstudiante = '';
    this.resultadosEstudiante = [];
    this.estudianteSeleccionado = null;
    // A.4.7A — limpiar el motivo de sobrecupo al cancelar, para que no
    // sobreviva a la próxima apertura del modal (defensa adicional a la
    // que ya hace abrirModalNuevaCitaConFechaHora al reconstruir
    // nuevaCita completo).
    this.nuevaCita.sobrecupo_motivo = '';
  }

  nuevaCita: any = {
    fecha: '', hora: '', estudiante_id: null, profesional_id: null,
    observaciones: '', urgente: false, sobrecupo: false, sobrecupo_motivo: ''
  };

  // A.4.7A v2 — punto 2: urgencia (A.4.5) y sobrecupo son flujos
  // separados; el toggle "¿Es urgente?" se oculta por completo mientras
  // la cita en curso es un sobrecupo, para que no sea posible activarlo
  // desde la UI (la defensa real está en crearCitaDesdeHorario(), esto
  // es solo la capa de presentación).
  get mostrarToggleUrgente(): boolean {
    return !this.nuevaCita.sobrecupo;
  }

  buscarEstudiante(): void {
    const q = this.busquedaEstudiante.trim();
    if (q.length < 2) { this.resultadosEstudiante = []; return; }
    this.buscandoEstudiante = true;
    this.http.get<any[]>(`${API}/admin/estudiantes?q=${encodeURIComponent(q)}`).subscribe({
      next: (data) => {
        this.resultadosEstudiante = data ?? [];
        this.buscandoEstudiante = false;
        this.cdr.detectChanges();
      },
      error: () => { this.resultadosEstudiante = []; this.buscandoEstudiante = false; this.cdr.detectChanges(); }
    });
  }

  seleccionarEstudiante(est: any): void {
    this.estudianteSeleccionado  = est;
    this.nuevaCita.estudiante_id = est.id;
    this.busquedaEstudiante      = est.nombre + ' — ' + est.rut;
    this.resultadosEstudiante    = [];
  }

  creandoCita = false;

  crearCitaDesdeHorario(): void {
    if (!this.hasPermission('agenda.gestionar')) return;
    if (this.creandoCita) return; // evita doble envío por doble clic
    if (!this.nuevaCita.estudiante_id) {
      this.mensajeError = 'Debes seleccionar un estudiante.';
      setTimeout(() => this.mensajeError = '', 3000); return;
    }

    // A.4.7A v2 — punto 2: urgencia (A.4.5, todavía pendiente) y
    // sobrecupo son flujos separados y NO deben combinarse. El toggle
    // "¿Es urgente?" ya se oculta en el modal cuando sobrecupo=true
    // (mostrarToggleUrgente), pero esto es la defensa en profundidad:
    // si de cualquier forma nuevaCita quedara con ambos en true, no se
    // manda ninguna request (ni a /citas ni a /admin/citas/urgente).
    if (this.nuevaCita.sobrecupo && this.nuevaCita.urgente) {
      this.mensajeError = 'Urgencia y sobrecupo todavía se gestionan por flujos separados.';
      setTimeout(() => this.mensajeError = '', 3000);
      return;
    }

    // A.4.7A — capa 4 (última, justo antes del POST): agenda.gestionar +
    // agenda.sobrecupo + motivo se vuelven a exigir acá mismo, sin
    // confiar en que las capas anteriores (render/clickBloque/
    // confirmarSobrecupo) no hayan sido saltadas — p. ej. si algo
    // externo pusiera nuevaCita.sobrecupo=true directamente. Backend
    // sigue siendo la autoridad final; esto es solo para no mandar una
    // request que el backend rechazaría, y dar el mensaje de validación
    // localmente.
    let sobrecupoMotivoNormalizado: string | null = null;
    if (this.nuevaCita.sobrecupo) {
      // A.4.7A v3 — esta capa debe ser AUTOSUFICIENTE: comprueba ambos
      // permisos explícitamente acá mismo, sin depender de que el
      // guard de agenda.gestionar del inicio de la función siga
      // existiendo o mantenga su posición actual. Si falta cualquiera
      // de los dos, no se manda la request y se muestra el mismo
      // mensaje de autorización.
      if (!this.hasPermission('agenda.gestionar') || !this.hasPermission('agenda.sobrecupo')) {
        this.mensajeError = 'No cuentas con el permiso de sobrecupo (agenda.sobrecupo) para autorizar esta hora.';
        setTimeout(() => this.mensajeError = '', 3000);
        return;
      }
      const motivo = String(this.nuevaCita.sobrecupo_motivo ?? '').trim();
      if (!motivo) {
        this.mensajeError = 'Debes indicar el motivo del sobrecupo.';
        setTimeout(() => this.mensajeError = '', 3000);
        return;
      }
      sobrecupoMotivoNormalizado = motivo;
    }

    this.creandoCita = true;
    const endpoint = this.nuevaCita.urgente ? `${API}/admin/citas/urgente` : `${API}/citas`;
    const payload: any = {
      estudiante_id: this.nuevaCita.estudiante_id, profesional_id: this.nuevaCita.profesional_id,
      fecha: this.nuevaCita.fecha, hora: this.nuevaCita.hora,
      observaciones: this.nuevaCita.observaciones, urgente: this.nuevaCita.urgente,
      sobrecupo: this.nuevaCita.sobrecupo || false
    };
    // Campo separado de observaciones (motivo clínico) — nunca se
    // reutiliza uno por otro. Para cita normal se omite por completo
    // (ni null ni string vacío), siguiendo el estilo real ya usado en
    // este payload (el resto de campos opcionales tampoco se envían
    // "apagados").
    if (this.nuevaCita.sobrecupo && sobrecupoMotivoNormalizado) {
      payload.sobrecupo_motivo = sobrecupoMotivoNormalizado;
    }

    // A.4.7A v2 — punto 1: el mensaje de 409 depende de si ESTA cita es
    // sobrecupo o no. Se calcula acá (antes del subscribe) porque
    // cerrarModalCita()/next del éxito no tocan nuevaCita.sobrecupo,
    // pero por claridad se fija el valor exacto que corresponde a esta
    // request en particular, sin depender del estado del componente en
    // el momento en que llega la respuesta.
    const esSobrecupo = this.nuevaCita.sobrecupo === true;

    this.http.post<any>(endpoint, payload).subscribe({
      next: () => {
        this.cerrarModalCita(); this.cargarHorarioProfesional();
        this.mensajeExito = 'Cita creada correctamente.';
        setTimeout(() => this.mensajeExito = '', 3000);
        this.creandoCita = false;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.creandoCita = false;
        const status = err?.status;
        const detail = err?.error?.detail;

        // A.4.7A — mapeo de error explícito (400/403/409). El 409
        // SIEMPRE usa un mensaje UX estable propio, sin importar qué
        // traiga el detail del backend: no forma parte del contrato
        // público afirmar cuántas citas existen ya en ese slot. La
        // palabra "sobrecupo" solo aparece si ESTA cita era realmente
        // un sobrecupo — una cita normal que pierde la carrera A.3 no
        // tiene nada que ver con sobrecupo y no debe mencionarlo. Para
        // 400/403, el detail del backend YA es texto pensado para
        // mostrarse (ver _mapear_denegacion_sobrecupo) — se usa tal
        // cual cuando es un string seguro; si no lo es (objeto,
        // ausente), se cae a un mensaje genérico y nunca se renderiza
        // "[object Object]".
        if (status === 409) {
          this.mensajeError = esSobrecupo
            ? 'El horario cambió y ya no admite este sobrecupo. La agenda se actualizará.'
            : 'El horario cambió y ya no está disponible. La agenda se actualizará.';
        } else if (typeof detail === 'string' && detail.trim()) {
          this.mensajeError = detail;
        } else if (status === 403) {
          this.mensajeError = 'No cuentas con autorización para realizar esta acción.';
        } else {
          this.mensajeError = 'No se pudo crear la cita.';
        }
        setTimeout(() => this.mensajeError = '', 3000);

        // Si alguien más tomó esa hora (o agotó el cupo de sobrecupo)
        // justo antes, refrescamos la grilla para que la celda ya no
        // aparezca con una capacidad que ya no existe — la grilla local
        // nunca es la fuente de verdad, siempre se vuelve a consultar
        // backend. Esto aplica igual para una cita normal (A.3) que
        // para un sobrecupo.
        if (status === 409) {
          this.cerrarModalCita();
          this.cargarHorarioProfesional();
        }
        this.cdr.detectChanges();
      }
    });
  }

  imprimirAgenda(): void {
    const grilla = document.querySelector('.horario-card') as HTMLElement;
    if (!grilla) { window.print(); return; }
    const contenido = grilla.innerHTML;
    const ventana = window.open('', '_blank');
    if (ventana) {
      ventana.document.write(`<html><head><title>Agenda</title>
        <style>body{font-family:sans-serif;font-size:11px;} table{border-collapse:collapse;width:100%} td,th{border:1px solid #ccc;padding:4px;}</style>
        </head><body>${contenido}</body></html>`);
      ventana.document.close();
      ventana.print();
    }
  }
 // ══════════════════════════════════════
  // SOLICITUDES DE HORARIO (colación y jornada)
  // ══════════════════════════════════════

  solicitudesHorarioAdmin: any[] = [];

  cargarSolicitudesHorarioAdmin(): void {
    if (!this.hasPermission('agenda.gestionar')) {
      this.solicitudesHorarioAdmin = [];
      return;
    }
    this.http.get<any[]>(`${API}/admin/solicitudes-horario?estado=pendiente`).subscribe({
      next: (data) => {
        this.solicitudesHorarioAdmin = data ?? [];
        this.cdr.detectChanges();
      },
      error: () => {}
    });
  }

  aprobarSolicitudHorario(s: any): void {
    if (!this.hasPermission('agenda.gestionar')) return;
    this.http.patch(`${API}/admin/solicitudes-horario/${s.id}/aprobar`, {}).subscribe({
      next: () => {
        this.solicitudesHorarioAdmin = this.solicitudesHorarioAdmin.filter(x => x.id !== s.id);
        this.cargarProfesionales();
        if (this.filtroProfesionalId) this.cargarHorarioProfesional();
        this.mensajeExito = `Solicitud de ${s.profesional_nombre} aprobada.`;
        setTimeout(() => this.mensajeExito = '', 3000);
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.mensajeError = err?.error?.detail || 'No se pudo aprobar la solicitud.';
        setTimeout(() => this.mensajeError = '', 3000);
      }
    });
  }

  rechazarSolicitudHorario(s: any): void {
    if (!this.hasPermission('agenda.gestionar')) return;
    const motivo = prompt('Motivo del rechazo (opcional):') || '';
    this.http.patch(`${API}/admin/solicitudes-horario/${s.id}/rechazar`, { motivo }).subscribe({
      next: () => {
        this.solicitudesHorarioAdmin = this.solicitudesHorarioAdmin.filter(x => x.id !== s.id);
        this.mensajeExito = `Solicitud de ${s.profesional_nombre} rechazada.`;
        setTimeout(() => this.mensajeExito = '', 3000);
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.mensajeError = err?.error?.detail || 'No se pudo rechazar la solicitud.';
        setTimeout(() => this.mensajeError = '', 3000);
      }
    });
  }
  // ══════════════════════════════════════
  // PROFESIONALES
  // ══════════════════════════════════════

  // profesionales[] sigue viviendo aquí: es la ÚNICA fuente de verdad,
  // compartida además por Inicio, Horario y Configuración (Fase 3.4A/3.4C).
  profesionales: any[] = [];

  // Especialidades dinámicas desde el backend (usadas también en Horario,
  // Historial y Reportes, no exclusivas de Gestión de Profesionales).
  get especialidades(): string[] {
    const fromProfs = this.profesionales.map(p => p.especialidad).filter(Boolean);
    const base = ['Medicina General','Psicología','Kinesiología','Odontología','Nutrición','Oftalmología','Psicopedagogía'];
    return Array.from(new Set([...base, ...fromProfs]));
  }

  get profesionalesActivos(): number { return this.profesionales.filter(p => p.estado === 'activo').length; }
  get profesionalesConIncidencia(): number { return this.profesionales.filter(p => ['enfermo','inasistencia','licencia'].includes(p.estado)).length; }

  cargarProfesionales(): void {
    if (!this.hasPermission('profesionales.ver')) {
      this.profesionales = [];
      return;
    }

    this.http.get<any[]>(`${API}/admin/profesionales`).subscribe({
      next: (data) => {
        this.profesionales = data ?? [];
        this.cdr.detectChanges();
      },
      error: () => { this.profesionales = []; }
    });
  }

  private cargarProfesionalesAgenda(): void {
    if (!this.hasPermission('agenda.ver')) {
      this.profesionales = [];
      return;
    }

    this.http.get<any[]>(`${API}/agenda/profesionales`).subscribe({
      next: (data) => {
        this.profesionales = data ?? [];
        this.cdr.detectChanges();
      },
      error: () => { this.profesionales = []; }
    });
  }

  // ══════════════════════════════════════
  // Handlers de los @Output de AdminProfesionalesComponent (Fase 3.4C).
  // El hijo NO ejecuta HTTP: solo emite la intención. El shell sigue siendo
  // quien llama al backend y actualiza profesionales[] (única fuente usada
  // también por Inicio/Horario/Configuración).
  // ══════════════════════════════════════

  onCrearProfesional(payload: any): void {
    if (!this.hasPermission('profesionales.gestionar')) return;
    this.http.post(`${API}/admin/profesionales`, payload).subscribe({
      next: () => {
        this.adminProfesionalesRef?.cerrarModal();
        this.cargarProfesionales();
        this.mensajeExito = 'Profesional creado. Contraseña temporal: prof123';
        setTimeout(() => this.mensajeExito = '', 5000);
        this.cdr.detectChanges();
      },
      error: (err) => { this.mensajeError = err?.error?.detail || 'No se pudo crear el profesional.'; setTimeout(() => this.mensajeError = '', 3000); }
    });
  }

  onCambiarEstadoProfesional(payload: { profesional: any; nuevoEstado: string; motivo: string }): void {
    if (!this.hasPermission('profesionales.gestionar')) return;
    const cancelarCitas = ['licencia','inasistencia'].includes(payload.nuevoEstado)
      ? confirm('¿Cancelar las citas de hoy y notificar estudiantes?') : false;
    this.http.patch(`${API}/admin/profesionales/${payload.profesional.id}/estado`, {
      estado: payload.nuevoEstado, motivo: payload.motivo || '',
      cancelar_citas: cancelarCitas
    }).subscribe({
      next: () => {
        this.adminProfesionalesRef?.cerrarModalAcciones();
        this.cargarProfesionales();
        this.mensajeExito = 'Estado actualizado.'; setTimeout(() => this.mensajeExito = '', 3000);
        this.cdr.detectChanges();
      },
      error: () => { this.mensajeError = 'No se pudo actualizar el estado.'; setTimeout(() => this.mensajeError = '', 3000); }
    });
  }

  onCambiarDuracionProfesional(payload: { profesional: any; duracionMin: number }): void {
    if (!this.hasPermission('profesionales.gestionar')) return;
    this.http.patch(`${API}/admin/profesionales/${payload.profesional.id}`, { duracion_min: payload.duracionMin }).subscribe({
      next: () => {
        this.cargarProfesionales();
        this.mensajeExito = `Duración actualizada a ${payload.duracionMin} min.`; setTimeout(() => this.mensajeExito = '', 3000);
        this.cdr.detectChanges();
      },
      error: () => { this.mensajeError = 'No se pudo actualizar la duración.'; setTimeout(() => this.mensajeError = '', 3000); }
    });
  }

  /**
   * tratamiento: null se envía explícito en el body (no se omite el campo)
   * para que el backend distinga "limpiar tratamiento" de "no tocarlo".
   */
  onCambiarTratamientoProfesional(payload: { profesional: any; tratamiento: string | null }): void {
    if (!this.hasPermission('profesionales.gestionar')) return;
    this.http.patch(`${API}/admin/profesionales/${payload.profesional.id}`, { tratamiento: payload.tratamiento }).subscribe({
      next: () => {
        this.cargarProfesionales();
        this.cargarResumenDia();
        this.mensajeExito = 'Prefijo actualizado.'; setTimeout(() => this.mensajeExito = '', 3000);
        this.cdr.detectChanges();
      },
      error: () => { this.mensajeError = 'No se pudo actualizar el prefijo.'; setTimeout(() => this.mensajeError = '', 3000); }
    });
  }

  /**
   * color: null se envía explícito en el body (no se omite el campo) para
   * que el backend distinga "quitar color" de "no tocarlo". Recarga
   * también resumenDia porque Disponibilidad Hoy en Inicio usa
   * color_identificador (vía nombreConTratamiento/badge) y debe quedar
   * sincronizado de inmediato.
   */
  onCambiarColorIdentificador(payload: { profesional: any; color: string | null }): void {
    if (!this.hasPermission('profesionales.gestionar')) return;
    this.http.patch(`${API}/admin/profesionales/${payload.profesional.id}`, { color_identificador: payload.color }).subscribe({
      next: () => {
        this.cargarProfesionales();
        this.cargarResumenDia();
        this.mensajeExito = 'Color identificador actualizado.'; setTimeout(() => this.mensajeExito = '', 3000);
        this.cdr.detectChanges();
      },
      error: () => { this.mensajeError = 'No se pudo actualizar el color.'; setTimeout(() => this.mensajeError = '', 3000); }
    });
  }

  onEliminarProfesional(p: any): void {
    if (!this.hasPermission('profesionales.gestionar')) return;
    if (!confirm(`¿Eliminar a ${p.nombre}?`)) return;
    if (!confirm('Se cancelarán TODAS sus citas pendientes y se notificará a los estudiantes. ¿Confirmar?')) return;
    this.http.delete(`${API}/admin/profesionales/${p.id}`).subscribe({
      next: () => {
        this.adminProfesionalesRef?.cerrarModalAcciones();
        this.cargarProfesionales();
        this.mensajeExito = 'Profesional eliminado.'; setTimeout(() => this.mensajeExito = '', 3000);
        this.cdr.detectChanges();
      },
      error: () => { this.mensajeError = 'No se pudo eliminar.'; setTimeout(() => this.mensajeError = '', 3000); }
    });
  }

  // Restaura exactamente el feedback de validación que existía antes de la
  // extracción (Corrección previa a aprobación, Fase 3.4C): el hijo solo
  // valida y emite el mensaje; el shell sigue siendo dueño de mensajeError/toast.
  onErrorValidacionProfesional(mensaje: string): void {
    this.mensajeError = mensaje;
    setTimeout(() => this.mensajeError = '', 3000);
  }

  // ══════════════════════════════════════
  // HISTORIAL
  // ══════════════════════════════════════

  // ═══ CITAS ═══
  // El listado, filtros, paginación y carga de datos de esta sección viven
  // ahora en AdminCitasComponent (Fase 3.4B). Lo que permanece aquí son las
  // acciones compartidas con otras secciones del shell (Inicio, Horario,
  // Historial), comunicadas por AdminCitasComponent mediante Outputs.

  marcarPrioridadCita(cita: any, urgente: boolean): void {
    if (!this.hasPermission('agenda.gestionar')) return;
    this.http.patch<any>(`${API}/admin/citas/${cita.id}/prioridad`, { urgente }).subscribe({
      next: (res) => {
        cita.urgente = res.urgente;
        this.toast.success(urgente ? 'Cita marcada como urgente' : 'Se quitó la prioridad urgente');
        this.cdr.detectChanges();
      },
      error: () => this.toast.error('No se pudo cambiar la prioridad de la cita')
    });
  }

  // ═══ ESTUDIANTES (listado + ficha con historial) ═══
  estudiantesAdmin:      any[] = [];
  estudiantesTotal             = 0;
  estudiantesPagina            = 1;
  estudiantesPorPagina          = 20;
  estudiantesFiltroQ           = '';
  estudiantesFiltroCarrera     = '';
  estudiantesCargando           = false;

  modalPerfilEstudianteAbierto  = false;
  perfilEstudianteCargando      = false;
  fichaEstudianteSeleccionado: any   = null;

  cargarEstudiantes(): void {
    if (!this.hasPermission('usuarios.ver')) {
      this.estudiantesAdmin = [];
      this.estudiantesTotal = 0;
      this.estudiantesCargando = false;
      return;
    }
    this.estudiantesCargando = true;
    let url = `${API}/admin/estudiantes/listado?pagina=${this.estudiantesPagina}&por_pagina=${this.estudiantesPorPagina}&`;
    if (this.estudiantesFiltroQ)       url += `q=${encodeURIComponent(this.estudiantesFiltroQ)}&`;
    if (this.estudiantesFiltroCarrera) url += `carrera=${encodeURIComponent(this.estudiantesFiltroCarrera)}&`;
    this.http.get<any>(url).subscribe({
      next: (res) => {
        this.estudiantesAdmin = res?.estudiantes ?? [];
        this.estudiantesTotal = res?.total ?? 0;
        this.estudiantesCargando = false;
        this.cdr.detectChanges();
      },
      error: () => { this.estudiantesCargando = false; }
    });
  }

  buscarEstudiantesAdmin(): void { this.estudiantesPagina = 1; this.cargarEstudiantes(); }

  limpiarFiltrosEstudiantes(): void {
    this.estudiantesFiltroQ = ''; this.estudiantesFiltroCarrera = ''; this.estudiantesPagina = 1;
    this.cargarEstudiantes();
  }


  irAPaginaEstudiantes(pagina: number): void {
    this.estudiantesPagina = pagina;
    this.cargarEstudiantes();
  }

  verPerfilEstudianteAdmin(est: any): void {
    if (!this.hasPermission('usuarios.ver')) return;
    this.modalPerfilEstudianteAbierto = true;
    this.perfilEstudianteCargando = true;
    this.fichaEstudianteSeleccionado = null;
    this.http.get<any>(`${API}/admin/estudiantes/${est.id}/perfil`).subscribe({
      next: (data) => { this.fichaEstudianteSeleccionado = data; this.perfilEstudianteCargando = false; this.cdr.detectChanges(); },
      error: () => { this.perfilEstudianteCargando = false; this.toast.error('No se pudo cargar la ficha del estudiante'); }
    });
  }

  cerrarModalPerfilEstudiante(): void {
    this.modalPerfilEstudianteAbierto = false;
    this.fichaEstudianteSeleccionado = null;
  }

  historialAdmin:      any[] = [];
  histFiltroEstudiante       = '';
  histFiltroDesde            = '';
  histFiltroHasta            = '';
  histFiltroEspecialidad     = '';
  histFiltroEstado           = '';
  histFiltroCarrera          = '';
  estadisticasEstudiante: any = null;

  // Modal de detalle de cita (botón del ojo en la tabla de historial)
  modalDetalleCitaAbierto = false;
  citaDetalle: any = null;

  verDetalleCita(h: any): void {
    this.citaDetalle = h;
    this.modalDetalleCitaAbierto = true;
  }

  cerrarModalDetalleCita(): void {
    this.modalDetalleCitaAbierto = false;
    this.citaDetalle = null;
  }

  cargarHistorial(): void {
    let url = `${API}/admin/historial?`;
    if (this.histFiltroEstudiante)   url += `estudiante=${encodeURIComponent(this.histFiltroEstudiante)}&`;
    if (this.histFiltroDesde)        url += `fecha_inicio=${this.histFiltroDesde}&`;
    if (this.histFiltroHasta)        url += `fecha_fin=${this.histFiltroHasta}&`;
    if (this.histFiltroEspecialidad) url += `especialidad=${encodeURIComponent(this.histFiltroEspecialidad)}&`;
    if (this.histFiltroEstado)       url += `estado=${this.histFiltroEstado}&`;
    if (this.histFiltroCarrera)      url += `carrera=${encodeURIComponent(this.histFiltroCarrera)}&`;
    this.http.get<any[]>(url).subscribe({
      next: (data) => {
        this.historialAdmin = data ?? [];
        if (this.histFiltroEstudiante.trim()) this.calcularEstadisticasEstudiante(this.historialAdmin);
        else this.estadisticasEstudiante = null;
        this.cdr.detectChanges();
      },
      error: () => {}
    });
  }

  calcularEstadisticasEstudiante(citas: any[]): void {
    if (!citas.length) { this.estadisticasEstudiante = null; return; }
    this.estadisticasEstudiante = {
      nombre: citas[0]?.estudiante ?? this.histFiltroEstudiante,
      total: citas.length,
      completadas:   citas.filter(c => c.estado === 'completada').length,
      canceladas:    citas.filter(c => c.estado === 'cancelada').length,
      inasistencias: citas.filter(c => c.estado === 'inasistencia').length
    };
  }


  limpiarFiltrosHistorial(): void {
    this.histFiltroEstudiante = ''; this.histFiltroDesde = ''; this.histFiltroHasta = '';
    this.histFiltroEspecialidad = ''; this.histFiltroEstado = ''; this.histFiltroCarrera = '';
    this.estadisticasEstudiante = null; this.cargarHistorial();
  }

  descargarPdf(citaId: number): void { window.open(`${API}/citas/${citaId}/pdf`, '_blank'); }
  exportarHistorialPdf(): void { alert('Exportar PDF: pendiente.'); }

  exportarHistorialExcel(): void {
    this.exportarComoExcel(this.historialAdmin.map(h => ({
      Estudiante: h.estudiante, RUT: h.rut, Carrera: h.carrera,
      Especialidad: h.especialidad, Profesional: h.profesional,
      Fecha: h.fecha, Hora: h.hora, Estado: h.estado
    })), 'historial_citas');
  }

  // ══════════════════════════════════════
  // CONFIGURACIÓN
  // ══════════════════════════════════════

  configCentro: any = {
    nombre_centro: 'SESAES', direccion: 'José Pedro Alessandri 1200, Ñuñoa',
    telefono: '', correo_contacto: '', horario_atencion: 'Lunes a Viernes 08:00–18:00',
    nombre_admin: 'Admin SESAES', foto_admin_url: null
  };

  // ══════════════════════════════════════
  // CONFIGURACIÓN → tabs (General / Citas / Horarios / Usuarios y roles / Seguridad)
  // ══════════════════════════════════════
  configTabActiva: ConfigTab = 'general';

  cambiarTabConfig(tab: ConfigTab): void {
    if (!this.puedeAccederTabConfig(tab)) {
      this.toast.error('No tienes permisos para acceder a esta opción.');
      return;
    }

    this.configTabActiva = tab;

    if (tab === 'general') this.cargarConfiguracionCentro();
    if (tab === 'citas') this.cargarConfiguracionCitas();
    if (tab === 'horarios') this.cargarDiasCerrados();
    if (tab === 'seguridad') this.cargarAuditoria();
  }

  // Reglas de citas (endpoint ya existente en backend: /admin/configuracion)
  configCitas: any = {
    duracion_turno_min: 20, agendamiento_por_pacientes: true,
    cancelacion_instantanea: false, sobreturnos_habilitados: true, cupos_por_turno: 4
  };
  guardandoConfigCitas = false;

  cargarConfiguracionCitas(): void {
    if (!this.hasPermission('configuracion.gestionar')) return;

    this.http.get<any>(`${API}/admin/configuracion`).subscribe({
      next: (data) => { this.configCitas = data ?? this.configCitas; this.cdr.detectChanges(); },
      error: () => {}
    });
  }

  guardarConfiguracionCitas(): void {
    if (!this.hasPermission('configuracion.gestionar')) return;

    this.guardandoConfigCitas = true;
    this.http.patch(`${API}/admin/configuracion`, {
      duracion_turno_min: Number(this.configCitas.duracion_turno_min),
      agendamiento_por_pacientes: this.configCitas.agendamiento_por_pacientes,
      cancelacion_instantanea: this.configCitas.cancelacion_instantanea,
      sobreturnos_habilitados: this.configCitas.sobreturnos_habilitados,
      cupos_por_turno: Number(this.configCitas.cupos_por_turno)
    }).subscribe({
      next: () => {
        this.guardandoConfigCitas = false;
        this.mensajeExito = 'Reglas de citas guardadas.'; setTimeout(() => this.mensajeExito = '', 3000);
        this.cdr.detectChanges();
      },
      error: () => {
        this.guardandoConfigCitas = false;
        this.mensajeError = 'No se pudo guardar la configuración de citas.'; setTimeout(() => this.mensajeError = '', 3000);
        this.cdr.detectChanges();
      }
    });
  }

  // Usuarios y roles (informativo — lista real de profesionales + admin actual)
  get usuariosDelSistema(): any[] {
    const admin = [{ nombre: this.configCentro.nombre_admin || 'Admin SESAES', rol: 'Administrador', estado: 'activo' }];
    const profs = this.profesionales.map(p => ({ nombre: p.nombre, rol: 'Profesional — ' + p.especialidad, estado: p.estado }));
    return [...admin, ...profs];
  }

  guardandoConfig         = false;

  // Edición de Información del Centro: campos bloqueados hasta presionar "Editar"
  centroEnEdicion      = false;
  configCentroOriginal: any = {};

  habilitarEdicionCentro(): void { this.centroEnEdicion = true; }

  cancelarEdicionCentro(): void {
    this.centroEnEdicion = false;
    this.configCentro.nombre_centro    = this.configCentroOriginal.nombre_centro;
    this.configCentro.telefono         = this.configCentroOriginal.telefono;
    this.configCentro.direccion        = this.configCentroOriginal.direccion;
    this.configCentro.correo_contacto  = this.configCentroOriginal.correo_contacto;
    this.configCentro.horario_atencion = this.configCentroOriginal.horario_atencion;
  }

  get centroModificado(): boolean {
    const o = this.configCentroOriginal;
    return this.configCentro.nombre_centro    !== o.nombre_centro
        || this.configCentro.telefono         !== o.telefono
        || this.configCentro.direccion        !== o.direccion
        || this.configCentro.correo_contacto  !== o.correo_contacto
        || this.configCentro.horario_atencion !== o.horario_atencion;
  }

  cargarConfiguracionCentro(): void {
    this.http.get<any>(`${API}/configuracion-centro`).subscribe({
      next: (data) => {
        this.configCentro = data ?? this.configCentro;
        this.configCentroOriginal = {
          nombre_centro: this.configCentro.nombre_centro, telefono: this.configCentro.telefono,
          direccion: this.configCentro.direccion, correo_contacto: this.configCentro.correo_contacto,
          horario_atencion: this.configCentro.horario_atencion
        };
        this.cdr.detectChanges();
      },
      error: () => {}
    });
  }

  guardarInfoCentro(): void {
    if (!this.puedeConfigGeneral) return;
    if (!this.centroModificado) return;
    this.guardandoConfig = true;
    this.http.patch(`${API}/configuracion-centro`, {
      nombre_centro: this.configCentro.nombre_centro, direccion: this.configCentro.direccion,
      telefono: this.configCentro.telefono, correo_contacto: this.configCentro.correo_contacto,
      horario_atencion: this.configCentro.horario_atencion
    }).subscribe({
      next: () => {
        this.guardandoConfig = false;
        this.centroEnEdicion = false;
        this.configCentroOriginal = {
          nombre_centro: this.configCentro.nombre_centro, telefono: this.configCentro.telefono,
          direccion: this.configCentro.direccion, correo_contacto: this.configCentro.correo_contacto,
          horario_atencion: this.configCentro.horario_atencion
        };
        this.mensajeExito = 'Información del centro guardada.'; setTimeout(() => this.mensajeExito = '', 3000);
        this.cdr.detectChanges();
      },
      error: () => { this.guardandoConfig = false; this.mensajeError = 'No se pudo guardar.'; setTimeout(() => this.mensajeError = '', 3000); this.cdr.detectChanges(); }
    });
  }

  // ══════════════════════════════════════
  // MI PERFIL (SA-1.2) — identidad real del Usuario autenticado.
  // Independiente de ConfiguracionCentro: fuente de verdad es
  // GET/PATCH /usuarios/me. AdminPerfilComponent NO ejecuta HTTP: solo
  // emite la intención; el shell dispara los PATCH y, tras éxito,
  // actualiza también la sesión de AuthService (nombre/foto) para que
  // el topbar (SA-1.1) refleje el cambio sin exigir logout/login.
  //
  // cargandoPerfil/perfilCargado: mientras el GET inicial no responde,
  // AdminPerfilComponent no debe permitir editar/guardar un objeto
  // vacío como si fueran datos reales del usuario (ver @Input
  // perfilCargado en admin-perfil.ts).
  // ══════════════════════════════════════
  usuarioPerfil: any = {};
  cargandoPerfil = false;
  perfilCargado  = false;

  cargarMiPerfil(): void {
    this.cargandoPerfil = true;
    this.perfilCargado  = false;
    this.http.get<any>(`${API}/usuarios/me`).subscribe({
      next: (data) => {
        this.usuarioPerfil = data;
        this.cargandoPerfil = false;
        this.perfilCargado  = true;
        this.cdr.detectChanges();
      },
      error: () => {
        this.cargandoPerfil = false;
        this.perfilCargado  = false;
        this.mensajeError = 'No se pudo cargar tu perfil.'; setTimeout(() => this.mensajeError = '', 3000);
        this.cdr.detectChanges();
      }
    });
  }

  // Handler del @Output de AdminPerfilComponent.
  //
  // Flujo determinista (corrige el bug de la entrega anterior, que
  // disparaba PATCH /usuarios/me ANTES de validar que las contraseñas
  // nuevas coincidieran):
  //   A. Valida localmente ANTES de cualquier PATCH.
  //      A.1. Si CUALQUIERA de los tres campos de contraseña viene con
  //           contenido, se considera que se intenta cambiar la
  //           contraseña: en ese caso deben venir LOS TRES. Si falta
  //           alguno, no se envía ningún PATCH (ni perfil ni
  //           contraseña).
  //      A.2. Solo con los tres presentes se valida que
  //           contrasena_nueva === contrasena_conf. Si no coinciden,
  //           tampoco se envía ningún PATCH.
  //   B. PATCH /usuarios/me.
  //   C. Solo si (B) tuvo éxito: sincroniza la sesión de AuthService.
  //   D. Solo si además se pidió cambio de contraseña: PATCH
  //      /configuracion-centro/cambiar-password, SIEMPRE después de
  //      que (B) confirmó éxito — nunca en paralelo, nunca si (B) falló.
  // Los 4 casos de mensaje (perfil solo / perfil+password OK /
  // perfil OK+password falla / perfil falla) se distinguen
  // explícitamente: nunca se afirma "Perfil y contraseña actualizados"
  // si una de las dos partes falló.
  onGuardarPerfilAdmin(payload: {
    nombre: string;
    telefono: string;
    foto_url?: string;
    contrasena_actual: string;
    contrasena_nueva: string;
    contrasena_conf: string;
  }): void {
    // A. Validación local — antes de tocar la red.
    // A.1. Cualquier campo de contraseña con contenido => se exigen los tres.
    const seSolicitaCambioPassword = !!payload.contrasena_actual || !!payload.contrasena_nueva || !!payload.contrasena_conf;
    if (seSolicitaCambioPassword) {
      const estanLosTresCampos = !!payload.contrasena_actual && !!payload.contrasena_nueva && !!payload.contrasena_conf;
      if (!estanLosTresCampos) {
        this.mensajeError = 'Completa todos los campos de contraseña.'; setTimeout(() => this.mensajeError = '', 3000);
        return;
      }
      // A.2. Los tres campos están presentes: recién aquí tiene sentido
      // comparar nueva vs. confirmación.
      if (payload.contrasena_nueva !== payload.contrasena_conf) {
        this.mensajeError = 'Las contraseñas nuevas no coinciden.'; setTimeout(() => this.mensajeError = '', 3000);
        return;
      }
    }

    const payloadUsuario: any = { nombre: payload.nombre, telefono: payload.telefono };
    if (payload.foto_url) payloadUsuario.foto_url = payload.foto_url;

    // B. PATCH /usuarios/me. (C) y (D) dependen de su resultado.
    this.http.patch<any>(`${API}/usuarios/me`, payloadUsuario).subscribe({
      next: (usuarioActualizado) => {
        this.usuarioPerfil = usuarioActualizado;

        // C. Perfil guardado con éxito: sincroniza el topbar (SA-1.1)
        // de inmediato, sin exigir logout/login. Correo y rol no se
        // tocan: no son editables desde Mi Perfil en esta fase.
        this.auth.actualizarIdentidadSesion({
          nombre: usuarioActualizado.nombre,
          foto_url: usuarioActualizado.foto_url
        });
        this.cdr.detectChanges();

        if (payload.contrasena_nueva) {
          // D. Cambio de contraseña solicitado: se dispara recién ahora,
          // nunca antes ni en paralelo con (B).
          this.http.patch(`${API}/configuracion-centro/cambiar-password`, {
            contrasena_actual: payload.contrasena_actual, contrasena_nueva: payload.contrasena_nueva
          }).subscribe({
            next: () => {
              // Caso: perfil OK + contraseña OK.
              this.mensajeExito = 'Perfil y contraseña actualizados correctamente.';
              this.adminPerfilRef?.finalizarEdicion(true);
              setTimeout(() => this.mensajeExito = '', 3000);
              this.cdr.detectChanges();
            },
            error: (err) => {
              // Caso: perfil OK + contraseña falla. Nunca se informa
              // como si ambos hubieran fallado o ambos tenido éxito:
              // el perfil YA se guardó, la contraseña NO cambió.
              this.mensajeExito = '';
              this.mensajeError = 'Perfil actualizado, pero la contraseña no se pudo cambiar: '
                + (err?.error?.detail || 'contraseña actual incorrecta.');
              this.adminPerfilRef?.finalizarEdicion(false);
              setTimeout(() => this.mensajeError = '', 4000);
              this.cdr.detectChanges();
            }
          });
        } else {
          // Caso: solo perfil, sin cambio de contraseña.
          this.adminPerfilRef?.finalizarEdicion(false);
          this.mensajeExito = 'Perfil actualizado correctamente.'; setTimeout(() => this.mensajeExito = '', 3000);
          this.cdr.detectChanges();
        }
      },
      error: () => {
        // Caso: el perfil falla -> nunca se intenta el cambio de
        // contraseña, aunque se hubiera solicitado.
        this.mensajeError = 'No se pudo actualizar el perfil.'; setTimeout(() => this.mensajeError = '', 3000);
        this.cdr.detectChanges();
      }
    });
  }

  esFeriado(fecha: string | undefined): boolean {
    return !!fecha && !!obtenerFeriado(fecha);
  }

  nombreFeriado(fecha: string | undefined): string {
    if (!fecha) return '';
    const f = obtenerFeriado(fecha);
    return f ? `Feriado: ${f.nombre}` : '';
  }

  // ══════════════════════════════════════
  // AUDITORÍA — selección múltiple
  // ══════════════════════════════════════

  auditoria: any[]  = [];
  auditFiltroDesde  = '';
  auditFiltroHasta  = '';
  cargandoAuditoria = false;

  // ══════════════════════════════════════
  // DÍAS CERRADOS (centro sin atención)
  // ══════════════════════════════════════
  diasCerrados: any[] = [];
  nuevoDiaCerrado: { fecha: string; motivo: string } = { fecha: '', motivo: '' };
  creandoDiaCerrado = false;
  modalCitasDiaCerradoAbierto = false;
  diaCerradoSeleccionado: any = null;
  citasDiaCerradoSeleccionado: any[] = [];
  cargandoCitasDiaCerrado = false;

  get hoyISO(): string { return new Date().toISOString().split('T')[0]; }

  cargarDiasCerrados(): void {
    if (!this.hasPermission('agenda.ver')) {
      this.diasCerrados = [];
      return;
    }
    this.http.get<any[]>(`${API}/admin/dias-cerrados`).subscribe({
      next: (data) => { this.diasCerrados = data ?? []; this.cdr.detectChanges(); },
      error: () => { this.diasCerrados = []; this.cdr.detectChanges(); }
    });
  }

  crearDiaCerrado(): void {
    if (!this.hasPermission('agenda.gestionar')) return;
    if (!this.nuevoDiaCerrado.fecha || !this.nuevoDiaCerrado.motivo || this.creandoDiaCerrado) return;
    this.creandoDiaCerrado = true;
    this.http.post<any>(`${API}/admin/dias-cerrados`, this.nuevoDiaCerrado).subscribe({
      next: (res) => {
        this.creandoDiaCerrado = false;
        this.nuevoDiaCerrado = { fecha: '', motivo: '' };
        this.mensajeExito = `Día cerrado correctamente. ${res.citas_canceladas} cita(s) cancelada(s) y notificada(s).`;
        setTimeout(() => this.mensajeExito = '', 5000);
        this.cargarDiasCerrados();
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.creandoDiaCerrado = false;
        this.mensajeError = err?.error?.detail || 'No se pudo cerrar el día.';
        setTimeout(() => this.mensajeError = '', 4000);
        this.cdr.detectChanges();
      }
    });
  }

  verCitasDiaCerrado(dia: any): void {
    if (!this.hasPermission('agenda.ver')) return;
    this.diaCerradoSeleccionado = dia;
    this.modalCitasDiaCerradoAbierto = true;
    this.cargandoCitasDiaCerrado = true;
    this.citasDiaCerradoSeleccionado = [];
    this.http.get<any[]>(`${API}/admin/dias-cerrados/${dia.id}/citas`).subscribe({
      next: (data) => {
        this.citasDiaCerradoSeleccionado = data ?? [];
        this.cargandoCitasDiaCerrado = false;
        this.cdr.detectChanges();
      },
      error: () => { this.cargandoCitasDiaCerrado = false; this.cdr.detectChanges(); }
    });
  }

  reabrirDiaCerrado(dia: any): void {
    if (!this.hasPermission('agenda.gestionar')) return;
    if (!confirm(`¿Reabrir el ${dia.fecha}? Las citas que ya se cancelaron NO se restauran automáticamente.`)) return;
    this.http.delete(`${API}/admin/dias-cerrados/${dia.id}`).subscribe({
      next: () => {
        this.mensajeExito = 'Día reabierto correctamente.';
        setTimeout(() => this.mensajeExito = '', 3000);
        this.cargarDiasCerrados();
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.mensajeError = err?.error?.detail || 'No se pudo reabrir el día.';
        setTimeout(() => this.mensajeError = '', 3000);
        this.cdr.detectChanges();
      }
    });
  }

  cargarAuditoria(): void {
    if (!this.hasPermission('auditoria.ver')) {
      this.auditoria = [];
      this.cargandoAuditoria = false;
      return;
    }

    this.cargandoAuditoria = true;
    let url = `${API}/admin/auditoria?`;
    if (this.auditFiltroDesde) url += `fecha_inicio=${this.auditFiltroDesde}&`;
    if (this.auditFiltroHasta) url += `fecha_fin=${this.auditFiltroHasta}&`;
    this.http.get<any[]>(url).subscribe({
      next: (data) => {
        this.auditoria = data ?? [];
        this.cargandoAuditoria = false;
        this.cdr.detectChanges();
      },
      error: () => { this.auditoria = []; this.cargandoAuditoria = false; this.cdr.detectChanges(); }
    });
  }

  // SA-5: la auditoría es append-only. Se retiraron eliminarAuditoria(),
  // auditoriaSeleccionada, hayAuditoriaSeleccionada,
  // todaAuditoriaSeleccionada, toggleSeleccionarTodaAuditoria() y
  // eliminarAuditoriaSeleccionada() — ya no existe ningún borrado ni
  // selección de registros de auditoría en el dashboard.

  exportarAuditoriaExcel(): void {
    if (!this.hasPermission('auditoria.ver')) return;

    this.exportarComoExcel(this.auditoria.map(a => ({
      'Fecha/Hora': a.fecha, Acción: a.accion, Detalle: a.detalle,
      Entidad: a.entidad, 'ID Entidad': a.entidad_id
    })), 'auditoria');
  }

  exportarAuditoriaPdf(): void { alert('Exportar PDF de auditoría: pendiente.'); }

  // ══════════════════════════════════════
  // UTILIDADES
  // ══════════════════════════════════════

  private exportarComoExcel(datos: any[], nombreArchivo: string): void {
    // NOTA (iteración Admin Inicio): este método genera un .xls que en
    // realidad es texto TSV, lo que hace que Excel muestre "posible
    // pérdida de datos" y rompa tildes/ñ (sin BOM/encoding real). Usado
    // hoy solo por exportarAuditoriaExcel() — Inicio ya migró a
    // exportarComoXlsx() (.xlsx real vía SheetJS). Pendiente migrar
    // Auditoría en su propia fase para no tocar ese módulo ahora.
    if (!datos.length) { alert('No hay datos para exportar.'); return; }
    const headers = Object.keys(datos[0]);
    const filas   = datos.map(fila => headers.map(h => fila[h] ?? '').join('\t'));
    const contenido = [headers.join('\t'), ...filas].join('\n');
    const blob = new Blob([contenido], { type: 'text/tab-separated-values;charset=utf-8;' });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href = url; a.download = `${nombreArchivo}_${new Date().toISOString().slice(0,10)}.xls`;
    a.click(); URL.revokeObjectURL(url);
  }

  /**
   * Genera un .xlsx real (formato binario/XML de Excel, no TSV disfrazado)
   * vía SheetJS. Preserva tildes/ñ correctamente porque el formato .xlsx
   * es UTF-8 nativo — no depende de BOM ni de que Excel adivine el encoding.
   */
  private exportarComoXlsx(datos: any[], nombreArchivo: string, nombreHoja: string): void {
    if (!datos.length) { alert('No hay datos para exportar.'); return; }
    const hoja  = XLSX.utils.json_to_sheet(datos);
    const libro = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(libro, hoja, nombreHoja);
    XLSX.writeFile(libro, `${nombreArchivo}_${new Date().toISOString().slice(0,10)}.xlsx`);
  }
}
