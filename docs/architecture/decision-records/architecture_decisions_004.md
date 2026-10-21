# ADR 004: continuidad inferida y tasas auditables

Fecha: 2026-10-21. Estado: aceptado para la iteración 3 offline.

## Problema

Sin ifCounterDiscontinuityTime comparable, la política estricta de la iteración 1
siempre rechaza las tasas. La captura de la iteración 2 contiene contadores de 64
bits y uptime pero ninguna marca utilizable. Se requiere distinguir evidencia de
continuidad reportada por el agente de inferencia sin esa confirmación.

## Decisión

- Función pura `calculate_traffic_rate` y `ContinuityPolicy`, sin I/O. Salida
  derivada v2 con bits/s o null, estado, motivo, ambos orígenes, intervalo real,
  reloj utilizado y snapshot/hash de política. No se mutan las observaciones.
- `calculada`: dos marcas comparables e iguales, con las demás validaciones.
  Significa continuidad según lo reportado por el agente, no prueba física
  absoluta. Marcas distintas cortan la tasa aunque el contador no haya caído.
- `inferida`: contador de 64 bits no decreciente, misma entidad, ambas muestras
  good, uptime disponible no regresivo e intervalo aceptable; falta al menos una
  marca comparable. No se inventa marca cero. Un reset silencioso que no deja el
  contador final por debajo del anterior y no reinicia el agente es indetectable.
- `no_calculable`: motivo explícito. No se saltean ausencias para conectar dos
  muestras buenas. Un intento sin observación se representa mediante referencia
  de ausencia, con tiempo de finalización del poll claramente identificado.
- Tiempo de consulta UTC como base única. Se conserva observed_at, pero no se
  mezcla el reloj del dispositivo con el del colector ni se divide por el nominal.
- Counter64 decreciente sin marca: contador_decreciente, incluso junto al límite
  del contador. Con marcas comparables y cota, el modo confirmado puede resolver
  un wrap 32/64. La inferencia no se extiende a Counter32.
- Una cota opcional en octetos/s agrega rechazo de deltas físicamente imposibles
  y de intervalos que permitirían vueltas completas. No se inventa capacidad de
  un dispositivo y el valor no es un máximo aprendido de las observaciones.

## Ajuste conservador a la propuesta: wrap del uptime

sysUpTime usa TimeTicks: 32 bits en centésimas, módulo 2^32, unos 497 días. Ante
una caída se compara el delta modular con el intervalo real, con tolerancia de
2 s configurable. Si es compatible se devuelve wrap_uptime_ambiguo, sin tasa:
sin otra evidencia no podemos distinguirlo de un reinicio en ese intervalo.
Si no es compatible, el motivo es reinicio_agente. En ambos casos se rechaza solo
ese par; la muestra actual puede ser base del siguiente. No se presenta la
clasificación como diagnóstico físico probado.

## Defaults y evaluación

Nominal 30 s, ventana inclusiva 15–45 s, antigüedad máxima 90 s. Son candidatos,
no los criterios finales de aceptación de la corrida de 24 h. Policy inmutable,
snapshot completo y SHA-256 facilitan fijar antes de la corrida exactamente qué
se evaluará. Cambiar un umbral crea otra identidad de política. Un reprocesamiento
posterior con otros umbrales se declara exploratorio y no reemplaza la evaluación
preacordada.

## Compatibilidad y persistencia

Observation v1, SQLite user_version=1 y datos crudos se conservan: no se persisten
tasas ni se agregan columnas, por lo que no corresponde una migración de base.
El nuevo contrato de resultado es rate.schema_version=2. RatePolicy y el cálculo
estricto quedan disponibles para reproducir informes v1; history admite ambas
políticas y la CLI activa inferencia con --continuity-policy infer-64. latest usa
la política nueva. La futura salida de snmp_cli también la usa; en esta iteración
no se ejecuta ninguna adquisición. Los 54 tests anteriores se conservan intactos.

Consultas CLI en SQLite mode=ro. latest respeta as_of, incluye el último intento
aunque haya fallado y no rescata un dato bueno más viejo. Una muestra vencida
queda unknown y su tasa actual queda null/dato_antiguo. El histórico conserva el
valor matemático pasado y muestra su antigüedad, sin confundirlo con estado actual.

## Alternativas descartadas

Marca artificial cero; convertir cualquier decremento en wrap; aceptar wrap de
uptime como certeza; interpolar huecos; cambiar observaciones históricas para
agregar certeza que la fuente no dio; almacenar tasas sin su política; alterar la
política estricta silenciosamente; migrar SQLite sin cambios de datos persistidos.

## Fuentes

- [SMIv2, RFC 2578, TimeTicks](https://www.rfc-editor.org/rfc/rfc2578.html#section-7.1.8).
- [IF-MIB, RFC 2863, discontinuidades](https://www.rfc-editor.org/rfc/rfc2863.html#section-3.1.5).
