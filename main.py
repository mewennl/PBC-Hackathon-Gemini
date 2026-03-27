import asyncio
import websockets
import json
import config
import auth
import time
import math
from openai import AsyncOpenAi

#set up openai client
openai_client = AsyncOpenAi()

REST_BASE = "https://api.gemini.com"

class PredictionTrader:
    """
    Flow:
    1. parse_intent()       -GPT extracts the certain event, # dollars, outcome from text input
    2. search_market()      - REST finds associated market + contract
    3. connect_account()    - Websocket connection
    4. get_live_price()     - subscribe to book and listen for best ask
    5. confirm_with_user()  - show cost and payout, confirm with user before placing
    6. place_order()        - order.place over websocket
    7. await_fill()         - listen to the orders@account endpoint

    """

    def connect_account(self):
        pass

    def place_order(self):
        pass


