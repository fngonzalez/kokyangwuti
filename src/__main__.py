from functions import data_ingestion
from dotenv import load_dotenv
import os


if __name__ == '__main__':
    
    load_dotenv()
    file_path = os.getenv("directory")
    data_ingestion(file_path)