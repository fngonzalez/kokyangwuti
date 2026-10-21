import requests
from requests.auth import HTTPBasicAuth


user = 'reader'
password = 'readapi'
url = 'http://192.168.88.1'
interface = '/rest/interface'

url_interface = url+interface
session = requests.Session()
session.auth = HTTPBasicAuth(user,password)



response_recursos = session.get(url_interface, verify=False, timeout=5)


print(response_recursos)