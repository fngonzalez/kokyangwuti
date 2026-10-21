# Kokyangwuti: núcleo offline de telemetría

## Alcance y ubicación

Implementado: contrato, SQLite, replay idempotente, tasas conservadoras,
histórico, conciliación e importación de fixtures. Sin SNMP real ni conexiones
de red. `src/telemetry` no importa `src/functions.py`, `src/__main__.py` ni `test/`.
Requiere Python 3.11+; validación realizada con Python 3.14. No hay dependencias
externas.

## Reproducir desde la raíz del repositorio

Usar solo el paquete nuevo. `python -m src` ejecutaría el prototipo anterior y
puede contactar al Netis; ese comando no forma parte de esta iteración.

```powershell
python -B -m unittest discover -s tests -p 'test_*.py' -v
python -B -m src.telemetry ingest tests/fixtures/session.json --database output/telemetry.sqlite3
python -B -m src.telemetry ingest tests/fixtures/session.json --database output/telemetry.sqlite3
python -B -m src.telemetry history --database output/telemetry.sqlite3 --device synthetic-router --interface inventory-uplink --metric if_in_octets --start 2026-10-21T12:00:00Z --end 2026-10-21T12:03:01Z --as-of 2026-10-21T12:03:01Z --max-counter-rate 10000
```

La segunda ingesta inserta cero observaciones. El fixture representa cinco polls,
cuatro observaciones buenas, un timeout, un hueco sin intentos y un retroceso de
uptime. Los identificadores y valores son sintéticos. No representan mediciones
del MikroTik real. La cota de 10000 octetos/s es exclusiva de ese ejemplo.

La ruta se elige por `--database`, luego `KOKYANGWUTI_DB_PATH`, luego
`output/telemetry.sqlite3`. No se requieren credenciales. La base predeterminada
y sus archivos auxiliares quedan ignorados por Git. Rutas UNC se rechazan; no
usar unidades de red mapeadas. No hay configuración SNMP en esta iteración.

## Contrato v1

`Metric`: `name`, `kind` (`counter`, `gauge`, `state`), `unit` y `counter_bits`
(32/64 para contador). Los tipos son parte del contrato; un cambio corta la tasa.

`Observation`:

| Campo | Significado |
|---|---|
| `device_id` | Identidad asignada por inventario, no una IP reutilizable |
| `interface_id` | Identidad lógica de inventario bajo la que se consulta el histórico |
| `interface_identity` | Encarnación de la entidad concreta; cambia ante reemplazo o continuidad incierta |
| `interface_index`, `interface_name` | Localizadores/etiquetas de la fuente, opcionales; no prueban identidad |
| `metric`, `raw_value` | Definición y valor crudo, sin derivar tasas sobre la entrada |
| `quality` | `good`, `missing`, `error`, `unknown`; no-good exige valor null |
| `observed_at` | Tiempo del equipo asociado al dato, solo si la fuente lo entrega; si no, null |
| `queried_at` | Tiempo UTC del colector al recibir/asignar el resultado; base del cálculo |
| `ingested_at` | Tiempo UTC de primera aceptación por el pipeline, anterior a la escritura durable; se preserva en replay |
| `uptime_ticks` | sysUpTime en centésimas de segundo (TimeTicks unsigned de 32 bits), opcional |
| `discontinuity_ticks` | ifCounterDiscontinuityTime, en el mismo dominio de uptime; opcional |
| `poll_id`, `schema_version` | Intento lógico y versión del contrato |

La encarnación es un dato de inventario provisto por quien produce la observación,
no un hash automático del nombre, índice o MAC. El núcleo valida su presencia y
detecta cambios; no descubre hardware. No confundir id lógico de inventario con
ifIndex. Un adaptador futuro deberá definir cuándo renovar la encarnación.

Se exigen tiempos con zona; se normalizan a UTC con precisión de microsegundos.
`queried_at` cae dentro del intento. Sin timestamp del equipo no sabemos el
instante exacto de actualización del contador ni si varios OIDs se leyeron de
forma atómica. Las mediciones con relojes diferentes no se mezclan en la tasa.

Clave de deduplicación: SHA-256 del JSON canónico de
`[schema_version,poll_id,device_id,interface_id,interface_identity,metric.name]`.
La misma identidad con otro valor, tiempo de consulta, calidad o metadato provoca
`ConflictError`; nunca se sobrescribe silenciosamente. Solo `ingested_at` queda
fuera del contenido comparado, porque un replay puede ocurrir después. El primer
tiempo recibido permanece. El `poll_id` debe ser globalmente único y persistente
para el replay del mismo intento; cada nueva consulta/reintento usa otro ID.

## Persistencia, errores y conciliación

`polls` registra intentos finalizados y `error_code` sanitizado. No guardar en él
credenciales, cuerpos HTTP ni respuestas completas de dispositivos.
`poll_metrics` declara las métricas esperadas por intento. `observations` conserva
los resultados recibidos, su identidad, tiempos y payload completo.

Una transacción confirma las tres tablas. Un intento exitoso debe tener todas sus
métricas `good`; uno parcial, alguna; uno fallido, ninguna. Un error puede no tener
filas de observación o tener resultados explícitos `error`/`missing`. El poll
finalizado es inmutable: un replay con otra lista de resultados se rechaza. Cada
poll es atómico; un archivo de varios polls puede quedar importado hasta el último
poll confirmado si luego falla otro. Reimportarlo permite continuar sin duplicar.

`reconcile()` devuelve unidades separadas:

- `polls`, `polls_success`, `polls_partial`, `polls_error`: intentos lógicos únicos.
- `metric_slots_expected`: métricas solicitadas, sumadas sobre intentos.
- `observation_rows`: filas realmente persistidas (incluye estados no-good).
- `metric_results_good`, `metric_results_not_good`: resultados por calidad.
- `metric_slots_without_row`: solicitudes sin fila de resultado.
- `metric_slots_without_good_result`: solicitudes sin dato utilizable.

Invariantes: polls = success + partial + error; expected = rows + without_row;
rows = good + not_good. Replays no son nuevos intentos de consulta. El resultado
de cada `ingest()` distingue polls nuevos, filas insertadas y duplicados.

La migración 0→1 crea tablas solo en una base vacía. `user_version` controla la
versión y `application_id` identifica la aplicación. No se alteran bases ajenas ni
versiones futuras. SQLite conserva el commit transaccional; no hay cola durable
previa, backups automáticos ni garantía ante pérdida física del almacenamiento.

## Tasas, intervalos y antigüedad

Se calcula delta de enteros / diferencia real de `queried_at`, no /30 fijo.
`value` está en unidades del contador por segundo. Para `unit=octets`,
`bits_per_second` multiplica por ocho. El contador crudo conserva exactitud; la
tasa es un float aproximado.

Política inicial del ejemplo: intervalo esperado 30 s, mínimo 1 s, máximo 45 s,
antigüedad 90 s. Son parámetros, no una frecuencia garantizada de adquisición.
`max_counter_rate` expresa una cota en unidades del contador/s y se suministra por
métrica consultada. No es una predicción ni se deduce del máximo observado.

Un CounterN vuelve a cero módulo 2^N. Se acepta un delta modular solo si uptime y
marca de discontinuidad no indican ruptura y la cota descarta más de una vuelta.
Si la cota permite una vuelta completa entre lecturas, la tasa es ambigua incluso
con contador creciente. Uptime ausente, discontinuidad desconocida, identidad
distinta, definición distinta, valor incompatible con la cota o intervalo anormal
producen tasa null con `reason`. Uptime regresivo se marca `uptime_regressed`:
compatible con reinicio del equipo/agente o wrap del propio uptime, sin afirmar
una causa física. Una marca de discontinuidad que cambió corta también la tasa.
Se toma la observación actual como nueva base para el siguiente par.

Los metadatos de continuidad deben ser fiables. Un reinicio que no se refleja en
ellos no puede detectarse mágicamente. La implementación no corrige ajustes de
reloj: intervalos no positivos se rechazan; saltos positivos pequeños requieren
validación del reloj del colector en la adquisición real. sysUpTime mide la vida
del agente de gestión y no garantiza por sí solo continuidad del hardware.

## Histórico

`history(device_id,interface_id,metric,start,end,policy,as_of)` devuelve [start,end)
ordenado por tiempo de consulta. Usa una muestra anterior al inicio para derivar
la primera tasa, sin incluirla en la salida. Ingestar fuera de orden no cambia el
orden temporal del histórico. Un resultado ausente se ubica en `finished_at` del
poll: ese tiempo identifica el intento, no un dato observado por el equipo.

- `observation`: valor crudo, contrato y tasa calculada o motivo de indisponibilidad.
- `missing`: intento conocido sin resultado para la métrica, con estado/error.
- `gap`: período sin intentos registrados cuando el intervalo supera el máximo;
  comienza en la siguiente consulta nominal esperada. Los extremos se recortan a
  la ventana consultada. Si no hay datos/contexto, toda la ventana es desconocida.

Después de un poll sin resultado no se conecta la última muestra buena con la
siguiente. `age_seconds` y `freshness` se calculan respecto de `as_of`; un dato
antiguo tiene `current_state=unknown` sin alterar su calidad histórica. `as_of`
debe ser igual o posterior al fin de la consulta. En un hueco la ausencia es
explícita, sin tasa ni valor cero.

## Continuidad de la iteración

Objetivo: validar el núcleo offline. Estado de implementación: implementado.
La evidencia ejecutada se informa al entregar la iteración; los fixtures y tests
permiten reproducirla. Pendiente: identidad real de interfaces, OIDs soportados,
cota física, relojes y adquisición SNMP; persistencia y operación real de 24 h.
Las credenciales detectadas previamente en material histórico y experimentos
deben revisarse/rotarse si continúan vigentes. No se copiaron al núcleo.

Único próximo paso propuesto: adaptador SNMP de solo lectura que produzca este
contrato y valide metadatos e identidad de una interfaz, en una iteración 2
autorizada específicamente para contactar el equipo.

## Validación ejecutada (2026-10-21)

Estado: núcleo implementado y validado offline, con Python 3.14.8.

- En el repositorio real: `python -B -m unittest discover -s tests -p 'test_*.py' -v`.
  Resultado: 33 tests, 0 fallas, 0 errores (0.346 s en la corrida de entrega).
- Los tests bloquean `socket.socket` para detectar conexiones accidentales. Cubren
  idempotencia, conflictos, delta con tiempo real, wraps 32/64, continuidad,
  identidad, muestras ausentes, intervalos, staleness, históricos y recuperación
  después de un fallo SQLite sintético que revierte todo el poll.
- CLI ejecutada además en procesos separados con una base temporal local:
  5 polls (4 success, 1 error), 5 métricas esperadas, 4 filas buenas, 1 resultado
  ausente. Reimportar el mismo fixture insertó 0 filas. Histórico: 10 octetos/s
  en el par normal; timeout y hueco explícitos; tasa null ante uptime regresivo.
- El estado Git mantiene rama dev y HEAD 6ae8c17. Comparación SHA-256 de los
  archivos preexistentes leídos: `.gitignore`, `src/functions.py`,
  `src/__main__.py` y los tres experimentos de `test/` sin cambios por esta tarea.
- No se ejecutaron los experimentos históricos, no se contactaron dispositivos,
  no se instalaron dependencias y no se hicieron commits ni push.

Pendiente: adquisición real y validación en equipo; no se declara validado el
MVP completo ni una operación de 24 horas. Próximo paso único: iteración 2 con
adquisición SNMP de lectura autorizada, conservando este contrato.
