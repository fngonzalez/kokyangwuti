# Iteración 3: continuidad y tasas de tráfico

## Objetivo y estado inicial

Implementar y probar inferencia conservadora y auditable de tasas, sin consultas
a equipos. Repositorio al empezar: dev, HEAD 97c8aa0. Permanecían modificados
`.gitignore` y `output/.gitignore`, junto con los archivos sin seguimiento de la
iteración 2. Se preservaron. Suite de inicio ejecutada: 54 tests OK.

Se releyeron el contrato v1, la política estricta, la persistencia y la guía de la
iteración 2. Los datos de origen no contienen tasas: se derivan al consultar. No
hay dependencias nuevas ni cambios de configuración de red.

## Uso de la política nueva

API pura:

```python
from src.telemetry import ContinuityPolicy, calculate_traffic_rate

policy = ContinuityPolicy(
    expected_interval_seconds=30,
    min_interval_seconds=15,
    max_interval_seconds=45,
    stale_after_seconds=90,
    uptime_wrap_tolerance_seconds=2,
    max_counter_rate=None,
)
rate = calculate_traffic_rate(previous, current, policy)
```

previous y current son observaciones consecutivas de la serie, o None si falta
una observación. La función no busca otros datos, no modifica sus argumentos y
no abre bases ni sockets. El llamador no debe filtrar fallas antes de construir
los pares. TelemetryStore.history respeta ese requisito incluyendo intentos sin
resultado y muestras explícitas missing/error/unknown.

| Estado | Condición |
|---|---|
| calculada | Ambas marcas disponibles e iguales; se superan los demás controles |
| inferida | Falta una marca comparable y Counter64 no decrece; no hay contradicción observable |
| no_calculable | Falla un control, falta evidencia necesaria o el último estado venció |

Cada rate v2 contiene value_bps (float o null), unit=bits/s, state, reason,
previous_sample y current_sample, elapsed_seconds, time_basis=queried_at,
schema_version=2, policy y policy_id (SHA-256). Las fuentes contienen el valor
crudo, la definición, identidad, calidad, los tres tiempos, uptime/discontinuidad,
poll_id y clave de observación. Una referencia de ausencia contiene el poll_id,
sus identificadores y el tiempo de fin del intento; no representa una lectura del
equipo. Si no existe ni siquiera un intento conocido, la referencia es null.

La precisión de los contadores crudos se conserva como enteros; bits/s es una
aproximación float derivada de delta entero y microsegundos reales. No se calcula
una tasa de tráfico sobre gauges, estados o unidades distintas de octets.

## Reglas y motivos

Se valida identidad de dispositivo, identidad lógica y encarnación de interfaz.
No se considera un mero cambio de índice o nombre como prueba de otra entidad.
Ambas muestras deben ser good y tener la misma definición de contador.

- identidad_cambiada: no se cruzan entidades.
- muestra_ausente / calidad_no_aceptable: no se rellenan ni saltean fallas.
- sin_muestra_anterior: falta la primera base de comparación.
- intervalo_anormal: delta de tiempo fuera de la ventana inclusiva o no positivo.
- uptime_ausente: no hay evidencia mínima de continuidad del agente.
- reinicio_agente: uptime retrocedió sin compatibilidad modular con un wrap.
- wrap_uptime_ambiguo: caída compatible con wrap; se rechaza por ambigüedad.
- discontinuidad_contador: las marcas presentes cambiaron.
- contador_decreciente: Counter64 disminuyó sin marca comparable; no se supone wrap.
- inferencia_requiere_64_bits: sin marca no se infiere continuidad en Counter32.
- delta_excede_cota / multiples_wraps_posibles: viola la cota física suministrada.
- cota_requerida_para_wrap: un contador confirmado necesita cota para resolver wraps.

Con marca comparable, Counter32/64 puede resolver una vuelta modular si una cota
descarta vueltas adicionales. Sin marca, la regla de inferencia exige Counter64
no decreciente aunque un wrap sea matemáticamente posible. Una sola marca o una
marca que aparece/desaparece no confirma continuidad: la tasa sigue inferida.

**Límite de inferencia:** si un contador se resetea, vuelve a crecer hasta superar
su valor anterior y el agente no se reinicia, estas señales no detectan el reset.
El resultado puede ser inferida y numéricamente distinto del tráfico real. No se
lo promociona a calculada. Incluso una marca reportada sin cambios depende de la
implementación correcta del agente; no constituye una prueba física absoluta.
Sin cota física no se pueden descartar matemáticamente vueltas completas de un
contador; Counter64 y una ventana corta reducen esa plausibilidad, no la prueban.

## Wrap de sysUpTime

TimeTicks vuelve a cero módulo 2^32 centésimas: 497.102696 días. Para una caída se
compara `(uptime_actual-uptime_anterior) mod 2^32 / 100` con el intervalo real.
La tolerancia candidata de 2 s contempla resolución/lectura del agente solo para
clasificar el caso. No se usa para aceptar un par ambiguo ni para cambiar su
duración. Compatible: wrap_uptime_ambiguo; no compatible: reinicio_agente. Ambos
son no_calculable. El siguiente par puede retomar desde la muestra actual.

Se usa siempre queried_at; observed_at se conserva sin mezclar dominios de reloj.
Ajustes del reloj del colector siguen siendo un límite: retrocesos o saltos fuera
de ventana se rechazan, pero un ajuste pequeño dentro de la ventana no siempre
es detectable sin un reloj monotónico adicional. No se agregó uno retroactivamente.

## Umbrales y corrida de 24 horas

| Parámetro | Default candidato | Lugar de configuración |
|---|---:|---|
| Nominal | 30 s | ContinuityPolicy / --expected-interval |
| Mínimo | 15 s | ContinuityPolicy / --min-interval |
| Máximo | 45 s | ContinuityPolicy / --max-interval |
| Antigüedad máxima | 90 s | ContinuityPolicy / --stale-after |
| Tolerancia de clasificación de wrap | 2 s | ContinuityPolicy / --uptime-wrap-tolerance |
| Cota del contador | sin cota inventada | --max-counter-rate, en octetos/s |

La ventana y la antigüedad son inclusivas: 15/45 s válidos y edad 90 s todavía
dentro del umbral. A edad >90 s el último estado queda unknown.

Estos valores no se declararon definitivos para 24 h. Antes de la corrida deben
acordarse y registrarse snapshot e identificador de política junto con el alcance
de ether1. La evaluación usará ese mismo policy_id. Cambiar un umbral cambia su
hash; recalcular después con otros valores es exploración y se informa como tal,
sin sustituir el criterio preacordado por uno ajustado tras mirar resultados.

## Histórico y último estado, sin adquisición

```powershell
.\.venv\Scripts\python.exe -B -m src.telemetry history --database output/telemetry-snmp.sqlite3 --device lab-mikrotik-01 --interface lab-ether1 --metric if_in_octets --start 2026-10-21T04:17:19Z --end 2026-10-21T04:17:50Z --as-of 2026-10-21T04:17:50Z --continuity-policy infer-64 --max-counter-rate 12500000
.\.venv\Scripts\python.exe -B -m src.telemetry latest --database output/telemetry-snmp.sqlite3 --device lab-mikrotik-01 --interface lab-ether1 --metric if_in_octets --as-of 2026-10-21T04:20:00Z --max-counter-rate 12500000
```

Ambos comandos abren SQLite con mode=ro; no consultan equipos ni cargan la
configuración SNMP. No ejecutar snmp_cli ni python -m src en esta iteración.
La cota del ejemplo corresponde al puerto físico de 100 Mb/s identificado en la
iteración 2, no a otras interfaces. Estos comandos reprocesan una captura histórica
existente y no prueban el estado actual de la red.

history devuelve [start,end), con la muestra anterior solo como contexto, pares
con fuentes y huecos explícitos. El modo nuevo se activa con
--continuity-policy infer-64. El default strict y RatePolicy/calculate_rate se
conservan para reproducir resultados v1, incluidos los 54 tests anteriores.
Pasar ContinuityPolicy a TelemetryStore.history activa la proyección v2 por API.

latest usa la política nueva. Selecciona el último intento hasta as_of inclusive,
aunque falte su resultado, en lugar de rescatar la última lectura buena. Informa
age_seconds, freshness y current_state. Sin datos: unknown/sin_muestras. Vencido:
unknown y tasa null/no_calculable/dato_antiguo; conserva el crudo para trazabilidad.
El histórico conserva la tasa matemática pasada con antigüedad visible.

La salida futura de snmp_cli usa también la política nueva con ventana 0.5–1.5
del intervalo elegido (máximo al menos 45 s), edad 3 intervalos y snapshot en cada
tasa. Solo se modificó esa derivación de resultados; no se ejecutó adquisición.

## Versiones, persistencia y validación

No cambian Observation v1, sus claves de deduplicación ni SQLite user_version=1.
No se almacenan tasas; se versionó el resultado derivado como v2, conservando los
crudos. Por eso no corresponde una migración de tablas ni reescribir datos.
El modo de solo lectura impide incluso una ingesta accidental desde esa conexión.

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p 'test_*.py' -v
```

Los tests nuevos usan datos sintéticos y bloquean sockets. Cubren normalidad,
contador decreciente, reinicio, wrap ambiguo de uptime y recuperación del siguiente
par, identidad, ventanas, huecos, secuencias de estados, cero legítimo, marcas,
wraps confirmados 32/64, cotas, reloj elegido, trazabilidad, fingerprint, último
estado viejo, fallas recientes, as_of, apertura ro e integridad del archivo.
Validación ejecutada el 2026-10-21 en el repositorio real:

- Suite inicial: 54 tests OK. Suite final con el comando anterior: **84 tests OK
  en 2.155 s**, sin omitidos: los 54 previos intactos y 30 nuevos.
- Los dos comandos de consulta documentados arriba se ejecutaron sobre la captura
  existente con SQLite en modo ro. RX: primera tasa no_calculable por
  sin_muestra_anterior; segunda **inferida, 1 970 773.047770324 bits/s**, intervalo
  real **30.081207 s**, motivo sin_marca_discontinuidad.
- latest al as_of indicado: edad **130.430632 s**, freshness=stale,
  current_state=unknown y tasa null/no_calculable/dato_antiguo.
- La base conserva user_version=1, **2 polls y 8 observaciones**. SHA-256 antes y
  después: `3317E7704368C7CDDD7DDE1C4719ECBF50ED4508832182926EF88A9DF5AD56F4`.
- Hashes sin cambios del contrato, política estricta, prototipo CGI y ambos
  archivos de tests anteriores. git diff --check sin errores; Git solo advierte
  de su conversión de LF a CRLF en archivos ya existentes.

Esto valida la política sintética y el reprocesamiento local; no prueba continuidad
real, estado actual del equipo ni cobertura de la interfaz. No hubo adquisición,
instalación de dependencias ni escritura sobre la base de captura.

Pendiente fuera de esta tarea: cable/cobertura de ether1; rotación y restricción
de comunidad; evento de cambio de estado; operación de 24 h. No se leyó ni copió
el archivo con la comunidad para esta tarea. No hay validación de red nueva.

Próximo paso único: confirmar ether1 y su alcance de cobertura, y dejar acordados
los umbrales finales y el policy_id antes de la corrida de 24 horas.
