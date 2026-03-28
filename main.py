import asyncio
import httpx
import websockets
import json
import config
import auth
import time
import math
import os
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()
#print("KEY LOADED:", os.getenv("OPENAI_API_KEY"))
#set up openai client
openai_client = AsyncOpenAI()

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
                            - Preserve specific names exactly as given (e.g. "Claude", "GPT-4", "Gemini") in the event_description.
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
        if intent.get("outcome") not in ("yes", "no"):
            raise ValueError("Could not determine your yes/no description...")

        return intent

    async def search_market(self, intent): #dict to dict
        """
        search prediction market events by rest and uses gpt to pick best matching contract
        """
        params = {
            "status": "active",
            "limit": 50,

        }

        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{REST_BASE}/v1/prediction-markets/events", params=params)
            resp.raise_for_status()
            data = resp.json()

        events = data.get("data", [])

        # fetch page 2
        async with httpx.AsyncClient() as client:
            resp2 = await client.get(
                f"{REST_BASE}/v1/prediction-markets/events",
                params={"status": "active", "limit": 50, "offset": 50}
            )
            events += resp2.json().get("data", [])

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

        keyword = intent["event_description"].lower()
        events_summary.sort(
            key=lambda x: sum(w in x["event_title"].lower() for w in keyword.split()),
            reverse=True
        )
        events_summary_trimmed = events_summary[:10]
        match_prompt = match_prompt = f"""
            You are matching a user's prediction market intent to the best available contract.
             
            User intent: "{intent['event_description']}"
            User outcome preference: {intent['outcome']} (yes = betting it happens, no = betting it doesn't)
             
            Available contracts (JSON array):
            {json.dumps(events_summary_trimmed, indent=2)}
             
            Pick the single best matching contract and return ONLY this JSON:
            {{
              "instrument_symbol": "<exact instrumentSymbol from the list>",
              "event_title": "<event title>",
              "contract_label": "<contract label>",
              "best_ask": <number>,
              "reasoning": "<one sentence why this is the best match>",
              "confidence": "<high | low>"
            }}
            
            IMPORTANT: If none of the contracts genuinely match the user's intent,
            return confidence="low". Do NOT force a match.
             
            Rules:
            - Match on event topic first, then on the outcome direction.
            - If the user says outcome=yes, pick the contract that represents the thing happening.
            - If the user says outcome=no, pick the contract that represents the opposite (or the NO side).
            - Return ONLY the JSON. No markdown, no backticks.
            """
        response = await openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": match_prompt}],
            temperature=0
        )

        raw = response.choices[0].message.content.strip()
        match = json.loads(raw)

        if match.get("confidence") == "low":
            raise ValueError(
                f"No relevant market found for: {intent['event_description']}. Try topics like sports, crypto prices, politics, or economics.")

        match["outcome"] = intent["outcome"]
        return match

    async def connect_account(self):
        while True:
            try:
                headers = auth.get_headers(config.API_KEY, config.API_SECRET)
                async with websockets.connect(config.WSS_URL, additional_headers=headers) as ws:
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

            except Exception as e:
                print(f"ws disconnected: {e}, reconnecting")
                self.ws = None
                await asyncio.sleep(2)

    async def _handle_message(self, data):
        if data.get("status") and data["status"] != 200:
            err = data.get("error", {})
            print(f"websocket error {data['status']} — {err.get('msg', 'unknown error')}")
            return

        result = data.get("result", {})

        if "a" in data and "s" in data:
            symbol = data["s"].upper()  #add .upper()
            ask = float(data["a"])
            if symbol in self.price_futures and not self.price_futures[symbol].done():
                self.price_futures[symbol].set_result(ask)

        order_status = data.get("X") or result.get("status")
        exchange_id = str(result.get("orderId") or data.get("i", ""))
        client_id = str(result.get("clientOrderId") or data.get("c", ""))

        if order_status in ("NEW", "OPEN"):
            if exchange_id and exchange_id not in self.pending_orders and self.pending_orders:
                # map exchange_id to whichever local pending order exists
                local_id = next(iter(self.pending_orders))
                self.pending_orders[exchange_id] = self.pending_orders[local_id]

        if order_status == "FILLED":
            if exchange_id in self.pending_orders:
                self.pending_orders[exchange_id].set()
            elif client_id in self.pending_orders:
                self.pending_orders[client_id].set()

    async def get_live_price(self, instrument_symbol, timeout: float = 5.0) -> float:
        if self.ws is None:
            raise RuntimeError("websocket not initialized")

        future = asyncio.get_event_loop().create_future()
        self.price_futures[instrument_symbol] = future

        await self.ws.send(json.dumps({
            "id": f"price-{instrument_symbol}",
            "method": "subscribe",
            "params": [f"{instrument_symbol}@bookTicker"]
        }))

        try:
            ask = await asyncio.wait_for(future, timeout=timeout)
            return ask
        except asyncio.TimeoutError:
            raise TimeoutError(f"no price received for {instrument_symbol} within {timeout} seconds")
        finally:
            try:
                await self.ws.send(json.dumps({
                    "id": f"unsub-{instrument_symbol}",
                    "method": "unsubscribe",
                    "params": [f"{instrument_symbol}@bookTicker"]
                }))
            except Exception:
                pass
            self.price_futures.pop(instrument_symbol, None)

    def build_confirmation(self, match: dict, ask:float, dollar_amount: float) -> dict:
        """
        Calc contracts and builds confirmation to show to the user
        returns a dict with stuff to display
        """
        contracts = math.floor(dollar_amount / ask)
        actual_cost = round(contracts * ask, 2)
        potential_payout = contracts  # $1 per contract if correct
        potential_profit = round(potential_payout - actual_cost, 2)
        implied_probability = round(ask * 100, 1)
        return {
            "instrument_symbol": match["instrument_symbol"],
            "event_title": match["event_title"],
            "contract_label": match["contract_label"],
            "outcome": match["outcome"],
            "ask_price": ask,
            "contracts": contracts,
            "actual_cost": actual_cost,
            "potential_payout": potential_payout,
            "potential_profit": potential_profit,
            "implied_probability": implied_probability,
        }

    def print_confirmation(self, details: dict):
        print("\n" + "═" * 50)
        print("  TRADE CONFIRMATION")
        print("═" * 50)
        print(f"  Event   : {details['event_title']}")
        print(f"  Bet     : {details['contract_label'].upper()} ({details['outcome'].upper()})")
        print(f"  Odds    : ${details['ask_price']} per contract ({details['implied_probability']}% implied)")
        print(f"  Contracts: {details['contracts']}")
        print(f"  Cost    : ${details['actual_cost']}")
        print(f"  Payout  : ${details['potential_payout']} if correct (+${details['potential_profit']} profit)")
        print("═" * 50)

    async def place_order(self, details):
        """
        place limit order via ws
        price is set at best ask
        returns order id
        """
        if self.ws is None:
            raise RuntimeError("websocket not initialized")

        order_id = f"nlp-{int(time.time())}"

        order_msg = {
            "id": order_id,
            "method": "order.place",
            "params": {
                "symbol": details["instrument_symbol"],
                "side": "BUY",
                "type": "LIMIT",
                "timeInForce": "GTC",
                "price": str(details["ask_price"]),
                "quantity": str(details["contracts"]),
                "eventOutcome": details["outcome"].upper()   #ALWAYS Yes or no
            }
        }

        fill_event = asyncio.Event()
        self.pending_orders[order_id] = fill_event

        await self.ws.send(json.dumps(order_msg))
        print(f"\nOrder sent — id={order_id}")
        return order_id

    async def await_fill(self, order_id, timeout: float = 30.0):
        event = self.pending_orders.get(order_id)
        if not event:
            return
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            print(f"Order {order_id} was filled")
        except asyncio.TimeoutError:
            print(f"Order {order_id} was not filled within {timeout} seconds")
        finally:
            self.pending_orders.pop(order_id, None)

    async def execute_trade_from_text(self, user_text):
        print(f"\n Input: \"{user_text}\"")

        # Step 1: Parse intent
        print("\n[1/5] Parsing intent...")
        intent = await self.parse_intent(user_text)
        print(f"      → Event: {intent['event_description']}")
        print(f"      → Outcome: {intent['outcome'].upper()}")
        print(f"      → Amount: ${intent['dollar_amount']}")

        # Step 2: Find market
        print("\n[2/5] Searching markets...")
        match = await self.search_market(intent)
        print(f"      → Matched: {match['event_title']} — {match['contract_label']}")
        print(f"      → Symbol: {match['instrument_symbol']}")

        # Step 3: Get live price
        print("\n[3/5] Fetching live price...")
        ask = await self.get_live_price(match["instrument_symbol"])
        print(f"      → Best ask: ${ask}")

        # Step 4: Confirm
        print("\n[4/5] Building confirmation...")
        details = self.build_confirmation(match, ask, intent["dollar_amount"])
        self.print_confirmation(details)

        confirm = input("\n  Confirm trade? (y/n): ").strip().lower()
        if confirm != "y":
            print("  Trade cancelled.")
            return

        # Step 5: Place order
        print("\n[5/5] Placing order...")
        order_id = await self.place_order(details)
        await self.await_fill(order_id)

    async def run(self, user_text):
        ws_task = asyncio.create_task(self.connect_account())
        await asyncio.sleep(1.5)

        try:
            await self.execute_trade_from_text(user_text)
        finally:
            ws_task.cancel()
            try:
                await ws_task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
    else:
        text = input("What do you want to bet on? → ")

    trader = PredictionTrader()
    asyncio.run(trader.run(text))
