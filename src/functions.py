import requests
import time


def data_ingestion(file_path):
    timestamp = time.time()

    response = requests.get('http://192.168.88.2/cgi-bin-igd/netcore_get.cgi')
    payload = (response.text)
    print(payload)
    # from datetime import datetime
    # ahora = datetime.now()


    # # Formato: 17-04-2026 15:30:00
    # fecha_formateada = ahora.strftime("%d-%m-%YT%H.%M.%S")


    # with open (f'{file_path}data_{fecha_formateada}.txt', 'w') as f:
    #     f.write(payload)

data_ingestion("")
