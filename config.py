import os
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("SECRET")

WSS_URL = 'wss://ws.gemini.com'