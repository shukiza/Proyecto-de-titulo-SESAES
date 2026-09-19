import { ComponentFixture, TestBed } from '@angular/core/testing';
import { vi } from 'vitest';
import { AdminHorarioComponent } from './admin-horario';

describe('AdminHorarioComponent', () => {
  let component: AdminHorarioComponent;
  let fixture: ComponentFixture<AdminHorarioComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [AdminHorarioComponent]
    }).compileComponents();

    fixture = TestBed.createComponent(AdminHorarioComponent);
    component = fixture.componentInstance;

    fixture.componentRef.setInput(
      'formatearFechaFn',
      (fecha: string) => `FORMATO:${fecha}`
    );
    fixture.componentRef.setInput(
      'bloqueEstadoFn',
      () => 'disponible'
    );
    fixture.componentRef.setInput(
      'bloqueInfoFn',
      () => ''
    );
    fixture.componentRef.setInput(
      'esFeriadoFn',
      () => false
    );
    fixture.componentRef.setInput(
      'nombreFeriadoFn',
      () => ''
    );
  });

  it('debe crearse', () => {
    fixture.detectChanges();
    expect(component).toBeTruthy();
  });

  it('debe mostrar y emitir acciones sobre solicitudes pendientes', () => {
    const aprobarSpy = vi.spyOn(component.aprobarSolicitud, 'emit');
    const rechazarSpy = vi.spyOn(component.rechazarSolicitud, 'emit');

    const solicitud = {
      id: 1,
      profesional_nombre: 'Profesional SESAES',
      especialidad: 'Medicina',
      tipo: 'jornada',
      hora_inicio: '08:00',
      hora_fin: '17:00',
      fecha_solicitud: '2026-08-31T09:00:00'
    };

    fixture.componentRef.setInput('puedeGestionarAgenda', true);
    fixture.componentRef.setInput('solicitudesHorarioAdmin', [solicitud]);
    fixture.detectChanges();

    const botones = Array.from(
      fixture.nativeElement.querySelectorAll('button') as NodeListOf<HTMLButtonElement>
    );

    botones.find(b => b.textContent?.includes('Aprobar'))!.click();
    botones.find(b => b.textContent?.includes('Rechazar'))!.click();

    expect(aprobarSpy).toHaveBeenCalledWith(solicitud);
    expect(rechazarSpy).toHaveBeenCalledWith(solicitud);
  });

  it('debe emitir los cambios de filtros', () => {
    const especialidadSpy = vi.spyOn(
      component.filtroEspecialidadChange,
      'emit'
    );
    const profesionalSpy = vi.spyOn(
      component.filtroProfesionalIdChange,
      'emit'
    );

    component.filtroEspecialidadChange.emit('Medicina');
    component.filtroProfesionalIdChange.emit('10');

    expect(especialidadSpy).toHaveBeenCalledWith('Medicina');
    expect(profesionalSpy).toHaveBeenCalledWith('10');
  });

  it('debe mostrar el aviso cuando el profesional está bloqueado', () => {
    fixture.componentRef.setInput('filtroProfesionalId', '10');
    fixture.componentRef.setInput('profesionalActualBloqueado', true);
    fixture.componentRef.setInput('profesionalActual', {
      nombre: 'Profesional Bloqueado',
      estado: 'licencia'
    });

    fixture.detectChanges();

    const texto = fixture.nativeElement.textContent as string;
    expect(texto).toContain('Profesional Bloqueado');
    expect(texto).toContain('licencia');
  });

  it('debe renderizar la semana y emitir un clic de bloque', () => {
    const bloqueSpy = vi.spyOn(component.bloqueClick, 'emit');

    fixture.componentRef.setInput('filtroProfesionalId', '10');
    fixture.componentRef.setInput('semanaActual', [
      {
        nombre: 'Lun',
        num: 31,
        fecha: '2026-08-31',
        esHoy: true
      }
    ]);
    fixture.componentRef.setInput('semanaLabel', 'Agosto 2026');
    fixture.componentRef.setInput('horasGrilla', ['08:00']);

    fixture.detectChanges();

    const bloque = fixture.nativeElement.querySelector(
      '.bloque-celda'
    ) as HTMLDivElement;

    expect(bloque).toBeTruthy();
    bloque.click();

    expect(bloqueSpy).toHaveBeenCalledWith({
      fecha: '2026-08-31',
      hora: '08:00'
    });
  });

  it('debe emitir navegación semanal y Hoy', () => {
    const anteriorSpy = vi.spyOn(component.anterior, 'emit');
    const siguienteSpy = vi.spyOn(component.siguiente, 'emit');
    const hoySpy = vi.spyOn(component.hoy, 'emit');

    fixture.componentRef.setInput('filtroProfesionalId', '10');
    fixture.detectChanges();

    const botones = Array.from(
      fixture.nativeElement.querySelectorAll('button') as NodeListOf<HTMLButtonElement>
    );

    botones.find(b => b.getAttribute('aria-label') === 'Semana anterior')!.click();
    botones.find(b => b.getAttribute('aria-label') === 'Semana siguiente')!.click();
    botones.find(b => b.textContent?.trim() === 'Hoy')!.click();

    expect(anteriorSpy).toHaveBeenCalledTimes(1);
    expect(siguienteSpy).toHaveBeenCalledTimes(1);
    expect(hoySpy).toHaveBeenCalledTimes(1);
  });

  it('debe emitir Nueva Cita e Imprimir Agenda', () => {
    const nuevaSpy = vi.spyOn(component.abrirNuevaCita, 'emit');
    const imprimirSpy = vi.spyOn(component.imprimir, 'emit');

    fixture.componentRef.setInput('filtroProfesionalId', '10');
    fixture.componentRef.setInput('profesionalActualBloqueado', false);
    fixture.componentRef.setInput('puedeGestionarAgenda', true);
    fixture.detectChanges();

    const botones = Array.from(
      fixture.nativeElement.querySelectorAll('button') as NodeListOf<HTMLButtonElement>
    );

    botones.find(b => b.textContent?.includes('Nueva cita'))!.click();
    botones.find(b => b.textContent?.includes('Imprimir'))!.click();

    expect(nuevaSpy).toHaveBeenCalledTimes(1);
    expect(imprimirSpy).toHaveBeenCalledTimes(1);
  });

  it('debe mostrar las citas del día y emitir cancelación', () => {
    const cancelarSpy = vi.spyOn(component.cancelarCita, 'emit');

    const cita = {
      id: 20,
      estudiante: 'Ana Pérez',
      especialidad: 'Psicología',
      hora: '10:00',
      estado: 'pendiente',
      urgente: false
    };

    fixture.componentRef.setInput('filtroProfesionalId', '10');
    fixture.componentRef.setInput('diaSeleccionado', '2026-08-31');
    fixture.componentRef.setInput('citasDiaSeleccionado', [cita]);
    fixture.componentRef.setInput('puedeGestionarAgenda', true);

    fixture.detectChanges();

    expect(fixture.nativeElement.textContent).toContain('Ana Pérez');

    const cancelar = fixture.nativeElement.querySelector(
      '.detalle-acciones button'
    ) as HTMLButtonElement;

    cancelar.click();

    expect(cancelarSpy).toHaveBeenCalledWith(cita);
  });

  it('debe emitir cierre del detalle del día', () => {
    const diaSpy = vi.spyOn(component.diaSeleccionadoChange, 'emit');

    fixture.componentRef.setInput('filtroProfesionalId', '10');
    fixture.componentRef.setInput('diaSeleccionado', '2026-08-31');
    fixture.componentRef.setInput('citasDiaSeleccionado', []);

    fixture.detectChanges();

    const cerrar = fixture.nativeElement.querySelector(
      '.detalle-cerrar'
    ) as HTMLButtonElement;

    expect(cerrar).toBeTruthy();
    cerrar.click();

    expect(diaSpy).toHaveBeenCalledWith(null);
  });

  it('read-only oculta solicitudes, Nueva Cita y cancelación', () => {
    const solicitud = {
      id: 30,
      profesional_nombre: 'Profesional Read Only',
      especialidad: 'Medicina',
      tipo: 'jornada',
      hora_inicio: '08:00',
      hora_fin: '17:00',
      fecha_solicitud: '2026-09-07T09:00:00'
    };
    const cita = {
      id: 31,
      estudiante: 'Estudiante Read Only',
      especialidad: 'Medicina',
      hora: '11:00',
      estado: 'pendiente',
      urgente: false
    };

    fixture.componentRef.setInput('puedeGestionarAgenda', false);
    fixture.componentRef.setInput('solicitudesHorarioAdmin', [solicitud]);
    fixture.componentRef.setInput('filtroProfesionalId', '10');
    fixture.componentRef.setInput('diaSeleccionado', '2026-09-07');
    fixture.componentRef.setInput('citasDiaSeleccionado', [cita]);
    fixture.detectChanges();

    const texto = fixture.nativeElement.textContent as string;
    expect(texto).not.toContain('Aprobar');
    expect(texto).not.toContain('Rechazar');
    expect(texto).not.toContain('Nueva Cita');
    expect(fixture.nativeElement.querySelector('.detalle-acciones')).toBeNull();
  });

  it('AGENDA-A mantiene Semana como única vista habilitada', () => {
    fixture.detectChanges();

    const botones = Array.from(
      fixture.nativeElement.querySelectorAll('.agenda-view-switch button') as NodeListOf<HTMLButtonElement>
    );

    expect(botones.map(b => b.textContent?.trim())).toEqual(['Día', 'Semana', 'Mes']);
    expect(botones[0].disabled).toBe(true);
    expect(botones[1].disabled).toBe(false);
    expect(botones[1].classList.contains('active')).toBe(true);
    expect(botones[2].disabled).toBe(true);
  });

  it('A.4.7A: el bloque ocupado con sobrecupo disponible recibe la clase derivada y su ícono de affordance, sin dejar de ser "ocupado"', () => {
    fixture.componentRef.setInput('filtroProfesionalId', '10');
    fixture.componentRef.setInput('bloqueEstadoFn', () => 'ocupado');
    fixture.componentRef.setInput('bloqueSobrecupoDisponibleFn', () => true);
    fixture.componentRef.setInput('horasGrilla', ['08:00']);
    fixture.componentRef.setInput('semanaActual', [{ fecha: '2026-09-07', nombre: 'Lun', num: 7, esHoy: false }]);
    fixture.detectChanges();

    const celda = fixture.nativeElement.querySelector('.bloque-celda') as HTMLElement;
    expect(celda.classList.contains('ocupado')).toBe(true);
    expect(celda.classList.contains('ocupado-sobrecupo-disponible')).toBe(true);
    expect(celda.querySelector('.agenda-sobrecupo-affordance')).toBeTruthy();
  });

  it('A.4.7A: un bloque ocupado SIN sobrecupo disponible no recibe la clase derivada ni el ícono', () => {
    fixture.componentRef.setInput('filtroProfesionalId', '10');
    fixture.componentRef.setInput('bloqueEstadoFn', () => 'ocupado');
    fixture.componentRef.setInput('bloqueSobrecupoDisponibleFn', () => false);
    fixture.componentRef.setInput('horasGrilla', ['08:00']);
    fixture.componentRef.setInput('semanaActual', [{ fecha: '2026-09-07', nombre: 'Lun', num: 7, esHoy: false }]);
    fixture.detectChanges();

    const celda = fixture.nativeElement.querySelector('.bloque-celda') as HTMLElement;
    expect(celda.classList.contains('ocupado')).toBe(true);
    expect(celda.classList.contains('ocupado-sobrecupo-disponible')).toBe(false);
    expect(celda.querySelector('.agenda-sobrecupo-affordance')).toBeFalsy();
  });

  it('A.4.7A.1 — un bloque con cita normal + sobrecupo en el mismo slot renderiza AMBAS en la misma bloque-celda', () => {
    fixture.componentRef.setInput('filtroProfesionalId', '10');
    fixture.componentRef.setInput('bloqueEstadoFn', () => 'sobrecupo');
    fixture.componentRef.setInput('bloqueSobrecupoDisponibleFn', () => false);
    fixture.componentRef.setInput('horasGrilla', ['08:00']);
    fixture.componentRef.setInput('semanaActual', [{ fecha: '2026-09-07', nombre: 'Lun', num: 7, esHoy: false }]);
    // El orden determinista (normal antes de sobrecupo) es responsabilidad
    // de buscarCitasEnBloque()/getBloqueCitas() en dashboard-admin.ts; acá
    // el componente solo debe iterar fielmente lo que bloqueCitasFn() le
    // entregue — se le pasa ya en el orden esperado, como lo entregaría el
    // padre real.
    fixture.componentRef.setInput('bloqueCitasFn', () => [
      { id: 1, estudiante: 'Diego Soto', sobrecupo: false, urgente: false },
      { id: 2, estudiante: 'Carlos Muñoz', sobrecupo: true, urgente: false }
    ]);
    fixture.detectChanges();

    const celdas = fixture.nativeElement.querySelectorAll('.bloque-celda');
    expect(celdas.length).toBe(1); // una sola celda para las 08:00 — no se duplica la hora
    const celda = celdas[0] as HTMLElement;

    expect(celda.classList.contains('multi-cita')).toBe(true);

    const lineasCita = celda.querySelectorAll('.bloque-info');
    expect(lineasCita.length).toBe(2); // exactamente dos elementos de cita, no más

    expect(celda.textContent).toContain('Diego Soto');
    expect(celda.textContent).toContain('Carlos Muñoz');
    expect(celda.textContent).toContain('(Sobrecupo)');

    // La marca "(Sobrecupo)" pertenece a la línea de Carlos, no a la de Diego.
    expect(lineasCita[0].textContent).toContain('Diego Soto');
    expect(lineasCita[0].textContent).not.toContain('Sobrecupo');
    expect(lineasCita[1].textContent).toContain('Carlos Muñoz');
    expect(lineasCita[1].textContent).toContain('Sobrecupo');
  });

  it('A.4.7A.1 — un bloque con una sola cita NO recibe la clase multi-cita', () => {
    fixture.componentRef.setInput('filtroProfesionalId', '10');
    fixture.componentRef.setInput('bloqueEstadoFn', () => 'ocupado');
    fixture.componentRef.setInput('bloqueSobrecupoDisponibleFn', () => false);
    fixture.componentRef.setInput('horasGrilla', ['08:00']);
    fixture.componentRef.setInput('semanaActual', [{ fecha: '2026-09-07', nombre: 'Lun', num: 7, esHoy: false }]);
    fixture.componentRef.setInput('bloqueCitasFn', () => [
      { id: 1, estudiante: 'Diego Soto', sobrecupo: false, urgente: false }
    ]);
    fixture.detectChanges();

    const celda = fixture.nativeElement.querySelector('.bloque-celda') as HTMLElement;
    expect(celda.classList.contains('multi-cita')).toBe(false);
    expect(celda.querySelectorAll('.bloque-info').length).toBe(1);
    expect(celda.textContent).toContain('Diego Soto');
    expect(celda.textContent).not.toContain('Sobrecupo');
  });

  it('A.4.7A v2: bloqueTitulo condiciona colación/fuera de jornada a bloqueSobrecupoDisponibleFn (no promete una acción sin permiso)', () => {
    fixture.componentRef.setInput('bloqueSobrecupoDisponibleFn', (_f: string, h: string) => h === '08:00');
    fixture.detectChanges();

    component.bloqueEstadoFn = () => 'ocupado';
    expect(component.bloqueTitulo('2026-09-07', '08:00')).toBe('Horario ocupado — clic para solicitar sobrecupo');

    component.bloqueEstadoFn = () => 'sin-datos';
    expect(component.bloqueTitulo('2026-09-07', '08:00')).toBe('Disponibilidad aún no disponible');

    component.bloqueEstadoFn = () => 'cerrado-centro';
    expect(component.bloqueTitulo('2026-09-07', '08:00')).toBe('El centro no atiende este día');

    // Con capacidad de sobrecupo (hora 08:00, según el mock de arriba):
    // el título SÍ promete la acción.
    component.bloqueEstadoFn = () => 'fuera-horario';
    expect(component.bloqueTitulo('2026-09-07', '08:00')).toBe('Fuera del horario habitual — clic para solicitar sobrecupo');

    component.bloqueEstadoFn = () => 'colacion';
    expect(component.bloqueTitulo('2026-09-07', '08:00')).toBe('Hora de colación — clic para solicitar sobrecupo');

    // Sin capacidad de sobrecupo (otra hora, bloqueSobrecupoDisponibleFn
    // devuelve false): el título describe el bloqueo, pero NO promete una
    // acción que clickBloque() ya no permitiría ejecutar.
    component.bloqueEstadoFn = () => 'fuera-horario';
    expect(component.bloqueTitulo('2026-09-07', '09:00')).toBe('Fuera del horario habitual del profesional');

    component.bloqueEstadoFn = () => 'colacion';
    expect(component.bloqueTitulo('2026-09-07', '09:00')).toBe('Hora de colación del profesional');

    component.bloqueEstadoFn = () => 'disponible';
    expect(component.bloqueTitulo('2026-09-07', '08:00')).toBe('');
  });

  it('AGENDA-A calcula KPIs semanales solo con datos operativos reales', () => {
    fixture.componentRef.setInput('filtroProfesionalId', '10');
    fixture.componentRef.setInput('semanaActual', [
      { fecha: '2026-09-07' },
      { fecha: '2026-09-08' },
      { fecha: '2026-09-09' },
      { fecha: '2026-09-10' },
      { fecha: '2026-09-11' },
      { fecha: '2026-09-12' },
      { fecha: '2026-09-13' }
    ]);
    fixture.componentRef.setInput('citasHorario', [
      { fecha: '2026-09-07', estado: 'pendiente', urgente: false, sobrecupo: false },
      { fecha: '2026-09-08', estado: 'completada', urgente: false, sobrecupo: false },
      { fecha: '2026-09-09', estado: 'pendiente', urgente: true, sobrecupo: false },
      { fecha: '2026-09-10', estado: 'pendiente', urgente: false, sobrecupo: true },
      { fecha: '2026-09-11', estado: 'cancelada', urgente: true, sobrecupo: true },
      { fecha: '2026-09-12', estado: 'inasistencia', urgente: false, sobrecupo: false },
      { fecha: '2026-09-20', estado: 'completada', urgente: true, sobrecupo: true }
    ]);
    fixture.componentRef.setInput('diasCerrados', [
      { fecha: '2026-09-10' },
      { fecha: '2026-09-10' },
      { fecha: '2026-09-25' }
    ]);

    fixture.detectChanges();

    expect(component.citasProgramadasSemana).toBe(4);
    expect(component.atencionesRealizadasSemana).toBe(1);
    expect(component.urgenciasSemana).toBe(1);
    expect(component.sobrecuposSemana).toBe(1);
    expect(component.bloqueosSemana).toBe(1);
  });

  it('AGENDA-A no inventa KPIs antes de seleccionar profesional', () => {
    fixture.componentRef.setInput('filtroProfesionalId', '');
    fixture.componentRef.setInput('citasHorario', [
      { fecha: '2026-09-07', estado: 'completada' }
    ]);
    fixture.detectChanges();

    expect(component.citasProgramadasSemana).toBeNull();
    expect(component.atencionesRealizadasSemana).toBeNull();
    expect(fixture.nativeElement.textContent).toContain('Selecciona un profesional');
  });

  it('read-only bloquea handlers de mutación aunque se invoquen directamente', () => {
    const solicitud = { id: 40 };
    const cita = { id: 41, estado: 'pendiente' };
    const aprobarSpy = vi.spyOn(component.aprobarSolicitud, 'emit');
    const rechazarSpy = vi.spyOn(component.rechazarSolicitud, 'emit');
    const nuevaSpy = vi.spyOn(component.abrirNuevaCita, 'emit');
    const cancelarSpy = vi.spyOn(component.cancelarCita, 'emit');

    component.puedeGestionarAgenda = false;
    component.onAprobarSolicitud(solicitud);
    component.onRechazarSolicitud(solicitud);
    component.onAbrirNuevaCita();
    component.onCancelarCita(cita);

    expect(aprobarSpy).not.toHaveBeenCalled();
    expect(rechazarSpy).not.toHaveBeenCalled();
    expect(nuevaSpy).not.toHaveBeenCalled();
    expect(cancelarSpy).not.toHaveBeenCalled();
  });
  it('A.2B usa sin-datos como fallback seguro cuando no existe fuente de disponibilidad', () => {
    const aislado = new AdminHorarioComponent();
    expect(aislado.bloqueEstadoFn('2026-09-07', '08:00')).toBe('sin-datos');
  });

  it('A.2B representa sin-datos de forma neutra y no lo etiqueta como disponible', () => {
    fixture.componentRef.setInput('filtroProfesionalId', '10');
    fixture.componentRef.setInput('semanaActual', [
      { nombre: 'Lun', num: 7, fecha: '2026-09-07', esHoy: false }
    ]);
    fixture.componentRef.setInput('horasGrilla', ['08:00']);
    fixture.componentRef.setInput('bloqueEstadoFn', () => 'sin-datos');

    fixture.detectChanges();

    const bloque = fixture.nativeElement.querySelector('.bloque-celda') as HTMLDivElement;
    expect(bloque.classList.contains('sin-datos-bloque')).toBe(true);
    expect(bloque.classList.contains('disponible')).toBe(false);
    expect(bloque.getAttribute('aria-disabled')).toBe('true');
    expect(bloque.getAttribute('title')).toContain('Disponibilidad');
    expect(bloque.textContent).not.toContain('+ Disponible');
  });

});
