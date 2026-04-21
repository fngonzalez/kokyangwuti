import requests
import datetime 
import time
from dotenv import load_dotenv
import os

load_dotenv()
file_path = os.getenv("directory")

timestamp = time.time()

response = requests.get('http://192.168.1.1/cgi-bin-igd/netcore_get.cgi')
payload = (response.text)

from datetime import datetime
ahora = datetime.now()


# Formato: 17-04-2026 15:30:00
fecha_formateada = ahora.strftime("%d-%m-%YT%H.%M.%S")


with open (f'{file_path}data_{fecha_formateada}.txt', 'w') as f:
    f.write(payload)