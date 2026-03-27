import asyncio

import httpx
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
    def __init__(self):
        self.ws = None              #websocket
        self.pending_orders = {}    #order_id fill tracking
        self.price_futures = {}     #symbol to asyncio.future for bookticker price

    async def parse_intent(self, user_text): #str -> dict
        """
        Takes in text input from user,
        Returns:
        {
            "event_description": "Kansas wins ncaa tonight",
            "outcome": "yes" always yes or no,
            "dollar_amount": 100.0,
            "category": "sports"
        }
        """
        #AI generated system prompt to prompt an AI
        system_prompt = """
                            You are a prediction market order parser. Extract trading intent from natural language.
                             
                            Return ONLY valid JSON with these fields:
                            {
                              "event_description": "<concise search-friendly description of the event>",
                              "outcome": "<yes or no>",
                              "dollar_amount": <number>,
                              "category": "<sports | crypto | politics | economics | other>"
                            }
                             
                            Rules:
                            - "outcome" is always "yes" or "no". If the user says "I think X will win/happen", outcome = "yes".
                              If they say "X won't happen" or "I think X will lose", outcome = "no".
                            - "dollar_amount" is the USD amount the user wants to spend, as a number.
                            - "event_description" should be short and search-friendly (e.g. "Kansas NCAA win" not a full sentence).
                            - If dollar_amount is missing or ambiguous, return null for that field.
                            - Return ONLY the JSON object. No explanation, no markdown, no backticks.
                        """
        response = await openai_client.chat.completions.create(
            model = "gpt-4o",
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            temperature=0
        )

        raw = response.choices[0].message.content.strip()
        intent = json.loads(raw)

        if intent.get("dollar_amount") is None:
            raise ValueError("Could not determine dollar amount...")
        if intent.get("event_description") not in ("yes", "no"):
            raise ValueError("Could not determine your yes/no description...")

        return intent

    async def search_market(self, intent): #dict to dict
        """
        search prediction market events by rest and uses gpt to pick best matching contract
        """
        params = {
            "status": "active",
            "limit": 20,
        }
        if intent.get("category") and intent["category"] != "other":
            params["category"] = intent["category"]
        if intent.get("event_description"):
            params["search"] = intent["event_description"]

        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{REST_BASE}/v1/prediction-markets/events",
                params=params
            )
            resp.raise_for_status()
            data = resp.json()

        events = data.get("data", [])
        if not events:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{REST_BASE}/v1/prediction-markets/events",
                    params={"status": "active", "search": intent["event_description"], "limit": 20}
                )
                resp.raise_for_status()
                data = resp.json()
            events = data.get("data", [])

        if not events:
            raise ValueError(f"no prediction markets found for {intent['event_description']}")

        events_summary = []
        for ev in events:
            for contract in ev.get("contracts", []):
                if contract.get("status") != "active":
                    continue
                ask = contract.get("prices", {}).get("bestAsk")
                if not ask:
                    continue
                events_summary.append({
                    "event_title": ev["title"],
                    "contract_label": contract["label"],
                    "instrument_symbol": contract["instrumentSymbol"],
                    "best_ask": float(ask),
                    "expiry": ev.get("expiryDate", "unknown"),
                    "category": ev.get("category", "")
                })

        if not events_summary:
            raise ValueError("Found events but no active contracts with pricing available.")

        match_prompt = match_prompt = f"""
            You are matching a user's prediction market intent to the best available contract.
             
            User intent: "{intent['event_description']}"
            User outcome preference: {intent['outcome']} (yes = betting it happens, no = betting it doesn't)
             
            Available contracts (JSON array):
            {json.dumps(events_summary, indent=2)}
             
            Pick the single best matching contract and return ONLY this JSON:
            {{
              "instrument_symbol": "<exact instrumentSymbol from the list>",
              "event_title": "<event title>",
              "contract_label": "<contract label>",
              "best_ask": <number>,
              "reasoning": "<one sentence why this is the best match>"
            }}
             
            Rules:
            - Match on event topic first, then on the outcome direction.
            - If the user says outcome=yes, pick the contract that represents the thing happening.
            - If the user says outcome=no, pick the contract that represents the opposite (or the NO side).
            - Return ONLY the JSON. No markdown, no backticks.
            """
        response = await openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": match_prompt}],
            temperature=0
        )

        raw = response.choices[0].message.content.strip()
        match = json.loads(raw)
        match["outcome"] = intent["outcome"]
        return match


    async def connect_account(self):
        headers = auth.get_headers(config.API_KEY, config.API_SECRET)

        async with websockets.connect(
                config.WSS_URL,
                additional_headers=headers
        ) as ws:
            self.ws = ws
            print("connected")

        await ws.send(json.dumps({
            "id": "sub-orders",
            "method": "subscribe",
            "params": ["orders@account"]
        }))
        print("orders@account subscribed")

        async for raw in ws:
            await self._handle_message(json.loads(raw))

    async def _handle_message(self, data):
        if data.get("status") and data["status"] != 200:
            err = data.get("error", {})
            print(f"websocket error {data['status']} — {err.get('msg', 'unknown error')}")
            return

        result = data.get("result", {})

        if "a" in data and "s" in data:
            symbol = data["s"]
            ask = float(data["a"])
            if symbol in self.price_futures and not self.price_futures[symbol].done():
                self.price_futures[symbol].set_result(ask)

        order_status = result.get("status") or data.get("X")
        if order_status in ("NEW", "OPEN", "FILLED", "PARTIALLY_FILLED", "CANCELLED"):
            order_id = str(result.get("orderId") or data.get("i", ""))
            print(f"websocket order update — id={order_id} status={order_status}")
            if order_status == "FILLED" and order_id in self.pending_orders:
                self.pending_orders[order_id].set()




