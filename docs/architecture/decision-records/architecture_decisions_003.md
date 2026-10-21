# ADR 003: adquisición SNMP v2c acotada a una interfaz

Fecha: 2026-10-21. Estado: aceptado para la iteración 2.

## Contexto

El núcleo v1 ya está confirmado en el commit 97c8aa0. Se agrega una fuente real
sin reemplazar el prototipo CGI, el contrato, las tasas, SQLite ni la CLI offline.
La comunidad de lectura existente permite usar SNMP v2c. No se cambia RouterOS.

## Decisiones

- PySNMP 7.1.30 y pyasn1 0.6.4 en un entorno virtual, fijados en
  `requirements-snmp.txt`. Importación diferida para mantener el núcleo offline
  utilizable sin dependencias. No se implementa un protocolo SNMP propio.
- API del cliente limitada a GET/GETNEXT; sin SET, REST ni escrituras remotas.
  OIDs numéricos sin resolución de MIBs externas. Destino IPv4 literal.
- `inventory` enumera nombres/índices mediante GETNEXT, con límite 16 por defecto
  y 64 como máximo. Se detiene al salir del subárbol o ante error; no consulta
  todas las métricas de la red. `inspect` verifica una interfaz concreta.
- `poll` consulta una interfaz por PDU y entrega `PollAttempt` + `Observation`
  del contrato v1. Contadores de 64 bits por defecto, 32 bits solo por selección
  explícita después de inspeccionar soporte. No hay fallback silencioso.
- Uptime, nombre, MAC y marca de discontinuidad se solicitan junto con contadores,
  estado operacional y velocidad. El agente puede leer internamente esos valores
  en distintos instantes: una sola PDU no garantiza snapshot atómico.
- La identidad lógica y su encarnación se asignan en inventario. El archivo local
  fija además sysObjectID, nombre y MAC esperados; discrepancia o metadatos de
  vinculación ausentes generan calidad unknown y error de vinculación, sin
  reasignar la identidad automáticamente. sysObjectID identifica una clase de
  equipo; MAC/nombre no son prueba criptográfica ni identidad universal.
- Sin timestamp del dato provisto por la fuente, observed_at=null. queried_at es
  UTC al regresar la respuesta SNMP; ingested_at, UTC al construir el lote para
  persistir. Uptime es un reloj relativo, no el timestamp observado de la muestra.
- Timeout de 2 s y una retransmisión como valores iniciales. Límites de timeout
  0.1–10 s, retransmisiones 0–2, más un deadline externo por operación. Un poll
  lógico puede comprender hasta 1+retries solicitudes de transporte. Una nueva
  adquisición tiene otro UUID; persistir otra vez el mismo lote conserva el UUID.
- Ejecución finita: 2 polls / 30 s por defecto, máximo 120 polls y una hora de
  espera nominal. Tiempo real incluye consultas, retransmisiones y persistencia.
  Sin servicio, scheduler, tareas del sistema ni control del equipo.

## Evidencia del equipo y consecuencia de calidad

La lectura real identificó RouterOS RB750r2. ether1 está up, reporta 100 Mb/s y
contadores 32/64 bits. El OID de discontinuidad consultado no produjo un TimeTicks
utilizable. No se inventa cero ni se modifica el núcleo para aceptar esa ausencia:
se conserva discontinuity_ticks=null y las tasas quedan no calculables con motivo
discontinuity_unknown. El snapshot de uptime sí permite registrar un retroceso,
pero por sí solo no detecta todos los resets de contadores de una interfaz.

Esta iteración valida adquisición/persistencia; no acredita el MVP completo de
tasas confiables ni operación de 24 horas. El puerto físico observado es ether1;
no se afirmó que su cable llegue al NGA o al ISP.

## Alternativas descartadas

REST como sustituto del SNMP solicitado; inferir identidad desde ifIndex;
copiar credenciales a código o argumentos; atribuir un timeout a caída del equipo;
rellenar el OID ausente con cero; habilitar una aceptación de tasas menos estricta
sin una política explícita. SNMPv3 queda fuera de este adaptador v2c y requerirá
otra configuración de credenciales, sin cambios automáticos en el router.

## Fuentes

- [PySNMP 7.1](https://docs.lextudio.com/pysnmp/v7.1/docs/api-reference).
- [SNMP en RouterOS](https://manual.mikrotik.com/docs/diagnostics-monitoring-and-troubleshooting/snmp/).
- [IF-MIB, RFC 2863](https://www.rfc-editor.org/rfc/rfc2863.html).
- [hEX lite RB750r2: puertos 10/100](https://mikrotik.com/product/RB750r2).
