import os
from dotenv import load_dotenv

load_dotenv()

WC_KEY = os.environ.get("WC_KEY")
WC_SECRET = os.environ.get("WC_SECRET")
USUARIO = os.environ.get("USUARIO")
PASSWORD = os.environ.get("PASSWORD")