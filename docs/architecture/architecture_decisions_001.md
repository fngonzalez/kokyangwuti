# ADR
## Fecha: 2026/04/30
### Estado: accepted 

# Contexto
Resumen del problema y por qué requiere una decisión.
El objetivo de la propuesta es diseñar una arquitectura diseñada para poder hacer el almacenamiento, procesamiento y analítica de datos de networking. Está pensado para empezar a hacerse como un laboratorio hogareño y tiene en consideración las limitaciones de un sistema propio de hogar.
El problema a resolver es el monitoreo de repetidores del hogar, almacenando los datos para luego ser considerados con una finalidad de analítica, buscando acercarme a puntos más relacionados con inteligencia artificial y automatización avanzada de redes (NWDAF). 

Alternativas consideradas
Alternativa A — pros/cons:
Ingestar datos mediante python para la ingesta de logs del repetidor y almacenamiento en txt para el posterior procesamiento de datos. El mayor cuello de botella es la disponibilidad del sistema, ya que sin el manejo de errores pertinente, y la administración del código más adecuada, el sistema fallaría sin tener en consideración las requests y la disponibilidad de la red. Con una falla desde el repetidor o desde la computadora, o ambas, no se almacenaría como buffer los datos (como puede ser con mqtt o kafka)
El mayor activo de esta solución es la celeridad para probar la ingesta, las respuestas del modem, y tener en consideración si es que es viable, cómo están los datos, y los posibles inconvenientes que haya que tenerse en consideración para el sistema.

Alternativa B — pros/cons
La segunda alternativa es la automatización mediante python y pandas, teniendo en consideración las capas en medallones para poder hacer el tratamiento de datos de manera más ordenada y avanzada.

La expectativa es que es una arquitectura híbrida que ingeste primero los datos a json, y que después se hagan las ingestas de datos batch hacia una arquitectura en medallones. Los archivos json son los que van a tener la primera aproximación al repetidor con su correspondiente requests, y después de eso cada 24 horas se procesan los datos para volverlos a archivos delta y que habiliten la arquitectura delta lake.

En principio, la expectativa con esto es que se pueda llegar a tener una capa de landing que sean los datos en JSON, y despues en bronze, silver, y gold, tener los datos en delta para poder aprovechar o maximizar las funcionalidades del tipo de archivo. Esto es alineado a las arquitecturas modernas de datos para que se puedan implementar de manera exitosa las subsiguientes etapas de análisis y transformación esperadas para una solución con expectativas de volverse una plataforma orientada a la analítica.

En cuanto a la exportación de los datos desde el modem, las expectativas son de tener implementado al menos telegraf para poder tener un primer acercamiento a la centralización de tratamiento de datos de los repetidores, y ser capaz de poder tener en consideración el tratamiento de datos de manera centralizada, acercando la arquitectura a una más profesional o empresarial, y habilitando la posterior implementación de Kafka. 

La expectativa es tener los sistemas corriendo sobre docker para que sea transferible si fuera necesario, como tambien haciendo a profundidad las principales validaciones de datos y testing general.

la arquitectura o el flujo de datos aproximado:
telegraf - python - delta


Alternativa C — pros/cons
La última alternativa es íntegramente cloud y aprovechando las capacidades empresariales necesarias para una implementación empresarial. Aprovechando herramientas de cicd propias de databricks asset bundles, como tambien de gobernanza a través de unity catalog. El workspace de databricks también habilita tener la posibilidad de aprovechar las capacidades necesarias de gobernanza, cicd, apis de datos para poder exportar la información mediante apis o delta sharing. 
Por otro lado, se tiene la facultad de poseer tanto la capa OLTP como OLAP a través de un solo servicio, siendo capaces de diseñar el punta a punta del ETL, el procesamiento de datos, la gobernanza, y la disponibilización.

El sistema aprovecha las facultades necesarias de sistemas distribuidos de alta complejidad con la integración entre mqtt, telegraf y kafka, dejando a Databricks y Prometheus como principales herramientas de consumo y habilitadoras para la visualización y monitoreo de datos. Se pueden aprovechar las capacidades de procesamiento para integración de datos y testing de codigo y datos. 
Los datos dentro de adls gen2 o s3 se pueden aplicar políticas de datos y ser capaces de servir otras aplicaciones si fueran necesarias para tener en consideración de una manera poco gobernada y muy barata. 

Por último, el uso de PBI para el uso de presentación de datos, es nativa para databricks, de manera que vuelve fácil y liviana la integración entre los sistemas.

mqtt - telegraf - kafka - databricks - adls gen2 o s3 - pbi
           |_____ prometheus - grafana

pros: mayor escalabilidad, separacion de capacidades y responsabilidades, mayor capacidad de gobernanza e integración mas gobernada entre los sistemas. Esto habilita una capacidad mayor de desarrollo en materia de inteligencia artificial y llms. 
cons: mayor complejidad, implementaciones posiblemente terraformadas para un mayor control, y el desarrollo se hace netamente sobre nube, teniendo que cubrir costos si es que no son cuentas con créditos gratis disponibles.

Decisión
Qué se eligió y por qué.
Se va a ir escalando la solución entre las 3 opciones para tener en consideración todos los aspectos de la implementacion, y poder escalar a un buen nivel el sistema. 