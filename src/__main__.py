from functions import data_ingestion
from dotenv import load_dotenv
import os
from time import sleep

if __name__ == '__main__':
    
    while True:
        load_dotenv()
        file_path = os.getenv("directory")
        data_ingestion(file_path)
        print(file_path)
        sleep(300)