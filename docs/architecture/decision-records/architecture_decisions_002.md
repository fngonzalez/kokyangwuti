# ADR 002: núcleo local de telemetría, iteración 1

Fecha: 2026-10-21. Estado: aceptado para esta iteración.

## Problema y alcance

Conservar observaciones interpretables y consultar su evolución sin depender de
los equipos de red. Se implementa solo la entrada desde datos sintéticos. El
prototipo Netis de `src/functions.py` y los experimentos de `test/` se conservan.
No se importan: el prototipo produce una consulta HTTP al importarlo.

## Decisión

- Paquete aislado `src/telemetry/`, tests offline en `tests/` y biblioteca estándar.
- Un lote representa un intento finalizado sobre un dispositivo/interfaz, con una
  lista explícita de métricas esperadas. Polls y métricas tienen unidades distintas.
- SQLite con `user_version=1`, `application_id=KOKY`, claves foráneas e índices
  temporales. Migración inicial 0→1 solo en una base vacía. Una versión ajena o
  futura se rechaza. Cada poll es una transacción; no se almacena medio intento.
- Contadores unsigned de 64 bits dentro de JSON TEXT, reconstruidos como `int`;
  SQLite INTEGER tiene rango signed de 64 bits y no cubre todo Counter64.
- Intentos finalizados inmutables: replay idéntico no inserta; una colisión con
  contenido diferente falla. Reintentar la adquisición crea otro `poll_id`.
- El tiempo de consulta es la referencia temporal del cálculo. No se inventa el
  tiempo del equipo si la fuente no lo entrega.
- Identidad de inventario e identidad de encarnación separadas de índice/nombre.
  Una sustitución o una identidad incierta abre otra encarnación y corta la tasa.
- Tasas conservadoras: requieren uptime y marca de discontinuidad comparables,
  cota máxima en unidades del contador por segundo e intervalo aceptable. Una
  disminución con continuidad y capacidad compatibles admite un wrap. Si la cota
  permite una vuelta completa entre muestras, el resultado es ambiguo aunque el
  contador final sea mayor. No se inventa una cota para el equipo real.
- Los huecos conocidos por intentos fallidos se muestran como muestras ausentes;
  los períodos largos sin intentos se muestran como intervalos `gap`. No se
  interpreta ninguna ausencia como cero ni se interpolan valores.

La política de discontinuidades e identidad sigue la
[IF-MIB, RFC 2863, secciones 3.1.5 y 3.1.6](https://www.rfc-editor.org/rfc/rfc2863.html).
Un uptime que retrocede también puede indicar reinicio del agente o wrap del
propio TimeTicks. El núcleo invalida el intervalo y no afirma cuál fue la causa.

## Alternativas descartadas para esta iteración

- Reutilizar `functions.py` como punto de entrada: acopla el núcleo a una consulta
  real y al formato CGI del Netis.
- ORM, pandas o un motor temporal externo: agregan dependencias sin necesidad
  demostrada para este volumen y contrato.
- Guardar todos los números como REAL: perdería exactitud en contadores grandes.
- Inferir identidad únicamente desde ifIndex, nombre o MAC: no acredita
  continuidad de la entidad observada.
- Convertir toda caída de contador en wrap o rellenar huecos: fabrica tráfico.
- MQTT, Kafka, cloud, daemons y SNMP real: quedan fuera de esta iteración.

## Consecuencias y límites

Una tasa puede quedar sin calcular aunque existan dos números. Es una decisión
explícita de calidad. Una fuente sin marca de discontinuidad necesita una política
alternativa documentada antes de habilitar tasas. La identidad, los metadatos SNMP
y la cota física deberán verificarse en la iteración 2. No hay persistencia de
polls en curso: si un futuro colector muere antes de entregar el lote, queda un
hueco inferido, sin inventar un timeout. No se promete tolerancia a pérdida de
disco ni entrega exactly-once entre sistemas.
