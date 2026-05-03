"""
feriados.py — Cálculo de días hábiles en Chile

Combina la librería `holidays` (auto-actualizada) con un sistema de override
manual por si la ley cambia los feriados durante el año.

Uso:
    from feriados import calcular_deadline_habil
    from datetime import datetime

    inicio = datetime(2026, 5, 2, 14, 0)  # Sábado 14:00
    deadline = calcular_deadline_habil(inicio, dias=3)  # Salta sáb/dom/festivos
    # deadline = miércoles siguiente 14:00
"""
from datetime import datetime, timedelta, date

# Intentar importar la librería holidays. Si no está disponible, usar lista hardcoded.
try:
    import holidays as _holidays_lib
    _CHILE_HOLIDAYS = _holidays_lib.country_holidays("CL")
    _USANDO_LIB = True
except ImportError:
    _CHILE_HOLIDAYS = {}
    _USANDO_LIB = False
    print("[feriados] Librería 'holidays' no instalada, usando fallback hardcoded")


# Lista de feriados hardcoded como fallback (actualizar anualmente si no hay librería)
_FERIADOS_HARDCODED_2026 = {
    date(2026, 1, 1),   # Año Nuevo
    date(2026, 4, 3),   # Viernes Santo
    date(2026, 4, 4),   # Sábado Santo
    date(2026, 5, 1),   # Día del Trabajo
    date(2026, 5, 21),  # Glorias Navales
    date(2026, 6, 21),  # Día de los Pueblos Indígenas
    date(2026, 6, 29),  # San Pedro y San Pablo (lunes)
    date(2026, 7, 16),  # Virgen del Carmen
    date(2026, 8, 15),  # Asunción de la Virgen
    date(2026, 9, 18),  # Independencia Nacional
    date(2026, 9, 19),  # Glorias del Ejército
    date(2026, 10, 12), # Encuentro de Dos Mundos
    date(2026, 10, 31), # Día de las Iglesias Evangélicas
    date(2026, 11, 1),  # Todos los Santos
    date(2026, 12, 8),  # Inmaculada Concepción
    date(2026, 12, 25), # Navidad
}

# Override manual: feriados extra agregados por el usuario (vía endpoint)
# Se carga desde BD cuando se inicializa
_OVERRIDE_FERIADOS = set()
_OVERRIDE_NO_FERIADOS = set()  # días que la ley sacó de la lista


def init_feriados_override():
    """Crea tabla en BD para overrides manuales del calendario de feriados."""
    try:
        from inventario import get_conn
        conn = get_conn(); cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS feriados_override (
                fecha DATE PRIMARY KEY,
                tipo TEXT NOT NULL,
                nombre TEXT,
                creado_por TEXT,
                creado_at TIMESTAMP DEFAULT NOW()
            )
        """)
        conn.commit()
        # Cargar overrides existentes a memoria
        cur.execute("SELECT fecha, tipo FROM feriados_override")
        for fecha, tipo in cur.fetchall():
            if tipo == "agregar":
                _OVERRIDE_FERIADOS.add(fecha)
            elif tipo == "quitar":
                _OVERRIDE_NO_FERIADOS.add(fecha)
        cur.close(); conn.close()
    except Exception as e:
        print(f"[feriados] init override error: {e}")


def es_feriado(fecha):
    """¿Es feriado en Chile? Considera librería + overrides + fallback."""
    if isinstance(fecha, datetime):
        fecha = fecha.date()
    # Override manual "no es feriado" tiene prioridad máxima
    if fecha in _OVERRIDE_NO_FERIADOS:
        return False
    # Override manual "es feriado" siempre suma
    if fecha in _OVERRIDE_FERIADOS:
        return True
    # Librería oficial
    if _USANDO_LIB:
        return fecha in _CHILE_HOLIDAYS
    # Fallback hardcoded
    return fecha in _FERIADOS_HARDCODED_2026


def es_dia_habil(fecha):
    """¿Es día hábil? (No es sáb/dom/feriado)"""
    if isinstance(fecha, datetime):
        fecha_d = fecha.date()
    else:
        fecha_d = fecha
    # weekday(): 0=lunes ... 6=domingo
    if fecha.weekday() >= 5:  # sábado o domingo
        return False
    if es_feriado(fecha_d):
        return False
    return True


def calcular_deadline_habil(fecha_inicio, dias_habiles=3):
    """Calcula deadline sumando N días hábiles (lunes-viernes, sin feriados)
    a una fecha de inicio. Conserva la hora del día.

    Lógica:
    - Si el día de inicio NO es hábil, el primer día hábil cuenta como día 1
    - Si el día de inicio SÍ es hábil, el siguiente día hábil cuenta como día 1
    - Sigue contando días hábiles hasta acumular el total

    Ejemplo:
    - inicio = viernes 14:00, dias=3 → deadline = miércoles 14:00
    - inicio = lunes 10:00, dias=3 → deadline = jueves 10:00
    - inicio = sábado 14:00, dias=3 → deadline = miércoles 14:00 (cuenta desde el lunes)
    """
    if isinstance(fecha_inicio, str):
        fecha_inicio = datetime.fromisoformat(fecha_inicio.replace("Z", "+00:00"))

    fecha_actual = fecha_inicio
    dias_contados = 0

    while dias_contados < dias_habiles:
        fecha_actual += timedelta(days=1)
        if es_dia_habil(fecha_actual):
            dias_contados += 1

    return fecha_actual


def horas_habiles_restantes(deadline):
    """Cuántas HORAS faltan para el deadline (puede ser negativo si ya venció)."""
    if isinstance(deadline, str):
        deadline = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    ahora = datetime.now()
    delta = deadline - ahora
    return delta.total_seconds() / 3600


def descripcion_tiempo_restante(deadline):
    """Texto legible: '2d 5h restantes', '14h restantes', 'Vencido hace 3h'"""
    horas = horas_habiles_restantes(deadline)
    if horas < 0:
        horas_abs = abs(horas)
        if horas_abs < 24:
            return f"Vencido hace {int(horas_abs)}h"
        return f"Vencido hace {int(horas_abs/24)}d"
    if horas < 24:
        return f"{int(horas)}h restantes"
    dias = int(horas / 24)
    horas_resto = int(horas % 24)
    if horas_resto > 0:
        return f"{dias}d {horas_resto}h restantes"
    return f"{dias}d restantes"


def color_urgencia(deadline):
    """Devuelve un color según urgencia del deadline."""
    horas = horas_habiles_restantes(deadline)
    if horas < 0: return "vencido"      # rojo oscuro
    if horas < 4: return "critico"      # rojo
    if horas < 24: return "urgente"     # naranja
    if horas < 48: return "atencion"    # amarillo
    return "normal"                     # verde


# Llamar al cargar el módulo
try:
    init_feriados_override()
except Exception as e:
    print(f"[feriados] No se pudo inicializar override: {e}")
