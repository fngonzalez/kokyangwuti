import asyncio
import time
from pysnmp.hlapi.v3arch.asyncio import *

# Función auxiliar para traer el valor numérico del contador
async def obtener_contador(engine, auth, target, context, oid):
    errorIndication, errorStatus, errorIndex, varBinds = await get_cmd(
        engine, auth, target, context, ObjectType(ObjectIdentity(oid))
    )
    if errorIndication or errorStatus:
        print(f"Error: {errorIndication or errorStatus.prettyPrint()}")
        return None
    
    # varBinds[0][1] contiene el valor de respuesta. Lo convertimos a entero.
    return int(varBinds[0][1])

async def monitorear_trafico():
    # --- 1. Configuración ---
    IP_DISPOSITIVO = '192.168.88.1'
    COMUNIDAD = 'public'
    
    # Vamos a medir la entrada (Download) del puerto 1 (ether1)
    OID_IN_OCTETS = '1.3.6.1.2.1.2.2.1.10.1' 
    TIEMPO_ESPERA = 5  # Segundos entre mediciones

    # Reutilizamos los parámetros de conexión
    engine = SnmpEngine()
    auth = CommunityData(COMUNIDAD, mpModel=1)
    target = await UdpTransportTarget.create((IP_DISPOSITIVO, 161))
    context = ContextData()

    print(f"Midiendo tráfico de descarga en la interfaz 1... (Espera {TIEMPO_ESPERA} segundos)")

    # --- 2. Primera Medición (T1) ---
    bytes_t1 = await obtener_contador(engine, auth, target, context, OID_IN_OCTETS)
    if bytes_t1 is None:
        return

    # --- 3. Pausa ---
    await asyncio.sleep(TIEMPO_ESPERA)

    # --- 4. Segunda Medición (T2) ---
    bytes_t2 = await obtener_contador(engine, auth, target, context, OID_IN_OCTETS)

    # --- 5. Cálculo Matemático ---
    diferencia_bytes = bytes_t2 - bytes_t1
    
    # Multiplicamos por 8 para pasar de Bytes a bits
    bits_totales = diferencia_bytes * 8
    
    # Dividimos por el tiempo de espera para obtener bits por segundo (bps)
    bps = bits_totales / TIEMPO_ESPERA
    
    # Dividimos por un millón para pasarlo a Megabits por segundo (Mbps)
    mbps = bps / 1_000_000

    print(f"Tráfico actual de descarga: {mbps:.2f} Mbps")

# --- 6. Inicializar el script ---
if __name__ == '__main__':
    asyncio.run(monitorear_trafico())