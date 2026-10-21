import requests
import json
import base64
from Crypto.PublicKey import RSA
from Crypto.Cipher import PKCS1_v1_5

# ==========================================
# CONFIGURACIÓN DEL ROUTER
# ==========================================
ROUTER_IP = "192.168.88.3"
USERNAME = "admin"  # Por defecto en la interfaz web suele ser admin
PASSWORD = "admin"

# Clave pública extraída del código JavaScript del router
PUBLIC_KEY_STR = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDppUIZsbdDZlC61T4HiHhGvISt
2UW8Sx2DE7v65X2RqEZa9Pybhj0XbjFcWz0DwTsaXtijKL8zTmeGw2VkWcNAEZra
PWmm3LeaWO/F0gGJMMRT0MW1m0Wf04/vb0AcQEDCkNW0Ou7wBV57O0RK1LAEmZpf
LUaG8bYgtcMzdcur+wIDAQAB
-----END PUBLIC KEY-----"""

def encrypt_password(password: str) -> str:
    """
    Cifra la contraseña usando RSA PKCS#1 v1.5 y la codifica en Base64.
    Equivale exactamente a la función `ja.encrypt(a)` de JSEncrypt.
    """
    rsa_key = RSA.import_key(PUBLIC_KEY_STR)
    cipher = PKCS1_v1_5.new(rsa_key)
    
    # Ciframos el texto plano y lo convertimos a base64
    encrypted_bytes = cipher.encrypt(password.encode('utf-8'))
    encrypted_b64 = base64.b64encode(encrypted_bytes).decode('utf-8')
    
    return encrypted_b64

def get_router_telemetry():
    url = f"http://{ROUTER_IP}/cgi-bin/cgi_vista.cgi"
    
    # requests.Session() se encarga automáticamente de guardar 
    # y enviar la cookie 'SESSION_ID' en cada petición subsiguiente.
    session = requests.Session()
    
    # 1. Armar el payload de Login (cgid: 4)
    login_payload = {
        "username": USERNAME,
        "password": encrypt_password(PASSWORD),
        "cgid": 4
    }

    print("[*] Iniciando sesión en el router...")
    try:
        login_resp = session.post(url, json=login_payload, timeout=5)
        login_data = login_resp.json()
        
        # El código 2000 significa éxito en esta API
        if login_data.get("restcode") == 2000:
            print("[+] Autenticación exitosa. Sesión establecida.")
            
            # 2. Solicitar la telemetría general (cgid: 92)
            # Si solo quieres el status básico, puedes cambiar a 93
            telemetry_payload = {"cgid": 77}
            
            print("[*] Solicitando datos de telemetría (cgid: 92)...")
            telemetry_resp = session.post(url, json=telemetry_payload, timeout=5)
            
            if telemetry_resp.status_code == 200:
                print("\n[+] Datos obtenidos:\n")
                # Formateamos el JSON para que sea legible en consola
                print(json.dumps(telemetry_resp.json(), indent=4))
                
                # Si en el futuro vas a parsearlo con Telegraf, puedes imprimir 
                # directamente un formato crudo o enviarlo por MQTT desde aquí.
            else:
                print(f"[-] Error en la respuesta HTTP: {telemetry_resp.status_code}")
        else:
            print("[-] Error de inicio de sesión. Revisa las credenciales.")
            print(f"Detalle: {login_data}")

    except requests.exceptions.RequestException as e:
        print(f"[-] Error de conexión con el router: {e}")

if __name__ == "__main__":
    get_router_telemetry()