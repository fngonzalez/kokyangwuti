# Iteración 2: SNMP de lectura sobre una interfaz

## Alcance

Adaptador v2c y ejecución finita, integrados con el contrato v1 y TelemetryStore.
No se modifica el prototipo Netis ni el núcleo de datos. No se configuran servicios
del sistema, RouterOS, MQTT, Kafka ni un colector 24/7.

Archivos de implementación: `src/telemetry/snmp.py` y `snmp_cli.py`.
La CLI offline `python -m src.telemetry` mantiene su comportamiento anterior.
No ejecutar `python -m src`: pertenece al prototipo histórico que consulta CGI.

## Preparación y configuración local

Desde la raíz del repositorio, Python 3.11+:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-snmp.txt
```

PySNMP 7.1.30 y pyasn1 0.6.4 son dependencias de adquisición; no se requieren
para importar el núcleo. No se necesitan pytest, dotenv, pandas ni servicios.

`config/snmp.example.json` es una plantilla sin credenciales y con dirección
reservada de documentación. La configuración real está en
`config/snmp.local.json`, ignorada por `config/.gitignore`. Completar la comunidad
solamente ahí o en la variable de proceso `KOKYANGWUTI_SNMP_COMMUNITY`, que tiene
precedencia sobre el archivo. Nunca enviarla por argumento, imprimir settings ni
guardar respuestas completas en logs. El adaptador no carga automáticamente el
`.env` histórico y no tiene una comunidad por defecto.

El archivo debe contener un objeto JSON completo. `host`, `port`, `version`,
`timeout_seconds` y `retries` definen el transporte. La versión implementada es
`2c`. `interface_index` es un localizador, no la identidad. `device_id`,
`interface_id` e `interface_identity` son asignaciones de inventario. Los campos
`expected_sys_object_id`, `expected_interface_name` y `expected_interface_mac`
fijan los metadatos leídos mediante inspect. El adaptador exige esa vinculación
antes de capturar. Guardar/respaldar el archivo de inventario de forma apropiada;
no regenerar la encarnación en cada ejecución. Si hay reemplazo o identidad
incierta, verificar y asignar otra encarnación antes de retomar.

## Identificar y vincular una interfaz

```powershell
.\.venv\Scripts\python.exe -B -m src.telemetry.snmp_cli inventory --config config/snmp.local.json
.\.venv\Scripts\python.exe -B -m src.telemetry.snmp_cli inspect --config config/snmp.local.json --interface-index 1
```

inventory recorre solo el subárbol ifName y descarta el sucesor que marca su fin;
por defecto consulta como máximo 16 filas. No descubre conexiones físicas ni
asociaciones Wi-Fi. inspect devuelve metadatos públicos de la interfaz indicada y
qué anchos de contador pudo leer. Se elige 64 si está disponible. Si no, se puede
seleccionar explícitamente 32 en el archivo; un cambio de definición corta tasas.

En esta sesión se eligió **ether1, índice 1, identidad lógica lab-ether1**, como
interfaz piloto. Equipo observado: RouterOS RB750r2. Su relación física con el
NGA/ISP sigue sin comprobar. MAC, nombre y sysObjectID son comprobaciones
operativas contra reasignación de índice; no acreditan identidad universal.

## Capturar y consultar

```powershell
.\.venv\Scripts\python.exe -B -m src.telemetry.snmp_cli poll --config config/snmp.local.json --count 2 --interval 30 --max-counter-rate 12500000 --database output/telemetry-snmp.sqlite3
```

Este comando finaliza después de dos intentos. El intervalo se espera entre el
fin de un intento y el inicio del siguiente; la separación real incluye el tiempo
de consulta. No se divide el delta por 30 fijo. La cota de 12500000 octetos/s se
eligió para este puerto Ethernet físico de 100 Mb/s, verificado por lectura y por
la especificación del RB750r2. No copiarla a otras interfaces sin verificarla.

Se consultan y persisten cuatro métricas: if_in_octets, if_out_octets,
if_oper_status e if_speed. El JSON de salida incluye intentos, conciliación e
histórico de las cuatro series. La base real está separada de los fixtures y
queda ignorada en Git. El exit code es 0 si todos los polls obtuvieron todas sus
métricas buenas, 1 si hubo un error/resultado parcial y 2 ante configuración o
persistencia inválida. Una advertencia de continuidad no convierte un valor crudo
correcto en inválido; su tasa puede seguir siendo no calculable.

Los metadatos uptime/discontinuidad viajan en cada observación. observed_at es
null porque el agente no aporta un timestamp de origen por contador. queried_at
se toma al regresar la PDU; ingested_at, al preparar el lote. Una PDU no garantiza
que el agente lea todos los OIDs en el mismo instante.

Cada nuevo poll tiene UUID distinto, incluso al volver a ejecutar el comando.
Reinsertar un Capture ya obtenido con el mismo UUID es idempotente. Las
retransmisiones de PySNMP pertenecen al mismo intento lógico; no son nuevos polls.
Un timeout produce un intento error sin observaciones inventadas. Un OID ausente
produce calidad missing y valor null. Tipo ASN incompatible/rango inválido se
marca error. Un cambio de nombre/MAC/clase de equipo genera unknown en todas las
métricas y interface_binding_mismatch, conservando el vínculo de inventario para
auditar el fallo. No se reasigna otra interfaz automáticamente.

Si el almacenamiento falla, el comando termina con error. Un lote ya construido
puede reingestarse por la API del núcleo; no existe spool durable previo al commit.
Si el proceso muere antes de entregar un lote, el histórico solo puede mostrar
el hueco sin registros. No se promete ausencia de pérdidas ni exactly-once.

## Limitación comprobada de continuidad

En ether1 se leyeron contadores de 64 bits y uptime, pero no una marca
ifCounterDiscontinuityTime utilizable. Se persiste discontinuity_ticks=null.
El primer par carece de antecedente y los pares siguientes devuelven
discontinuity_unknown. No se sustituyó el valor por cero ni se relajó RatePolicy.
La adquisición puede estar validada aunque la tasa todavía no lo esté.

Resolver esto requiere una política explícita para esta fuente: verificar otra
evidencia de continuidad o admitir estimaciones claramente marcadas con sus
límites. No basta con asumir que uptime detecta todos los resets de contadores.

## Pruebas y límites

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p 'test_*.py' -v
```

54 tests pasaron en el repositorio real, sin skips: 33 del núcleo y 21 del
adaptador. Las nuevas pruebas usan un cliente falso y tipos ASN reales de la
dependencia instalada; no consultan equipos. Cubren contrato/tiempos, tipos y
rango unsigned64, timeout persistido, respuestas parciales, rechazo de identidad
incompatible, continuidad ausente, modo32 explícito, reloj regresivo, recorrido
acotado, configuración y excepciones de biblioteca sin filtración de secretos,
cierre de transporte ante fallo y dos polls integrados con SQLite.

## Evidencia real de esta sesión (2026-10-21)

- Rama al comenzar: dev, HEAD 97c8aa0, worktree limpio.
- Inventario SNMP: RouterOS RB750r2, siete interfaces devueltas y fin del subárbol
  antes del límite configurado. Interfaz piloto ether1, índice 1, estado up y
  velocidad reportada 100000000 bit/s. Lectura disponible de Counter32 y Counter64.
- Prueba real: dos polls success, ocho métricas esperadas, ocho filas good,
  cero resultados ausentes y cero errores de adquisición. Se usó Counter64.
- Tiempos UTC de recepción: 2026-10-21T04:17:19.488161Z y
  2026-10-21T04:17:49.569368Z; separación real 30.081207 s.
- RX crudo: 131869946511 y 131877356915 octetos. TX crudo: 13545105182 y
  13545570529 octetos. Uptime: 168648200 y 168651300 TimeTicks.
- En ambas muestras: discontinuity_ticks=null y advertencia
  discontinuity_unavailable. Las tasas siguen sin calcularse; el segundo punto
  devuelve discontinuity_unknown. No se publica como tasa confiable el cociente
  que podría obtenerse ignorando esa ausencia.
- Datos persistidos en output/telemetry-snmp.sqlite3, fuera de Git. La configuración
  real y su comunidad permanecen únicamente en el archivo local ignorado.

Estado: adquisición y persistencia reales validadas para esta interfaz y estos dos
intentos. Pendiente: política de continuidad para tasas, identificación del cable,
revalidación de identidad ante cambios y operación prolongada. No se cambió ningún
servicio o configuración del MikroTik, ni se hicieron commits o push.

SNMPv2c no cifra la comunidad. La comunidad compartida en la conversación debe
revisarse/rotarse y sus permisos/orígenes deben comprobarse; no se cambió la red.
El adaptador solo ofrece GET/GETNEXT, pero eso no demuestra por sí solo que la
comunidad del router tenga deshabilitada la escritura. No se hicieron SET para
comprobarlo. Tampoco se validaron 24 horas, reinicios físicos o fallas inducidas.

Próximo paso único: resolver y documentar la política de continuidad para este
RouterOS antes de declarar confiables las tasas reales.

Comprobación adicional de recuperación: se reabrió la base real, se reingestaron
los dos lotes originales con sus mismos UUID y se insertaron 0 filas nuevas.
La conciliación permaneció en 2 polls y 8 observaciones; PRAGMA integrity_check
devolvió ok. El contrato, las tasas, la persistencia, la CLI offline y el
prototipo CGI conservaron sus hashes de inicio. La suite final ejecutó 54 tests
sin fallas ni skips. git diff --check no detectó errores de whitespace.
