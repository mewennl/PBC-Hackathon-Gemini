import asyncio
import json
import time
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from main import PredictionTrader

# ── persistence ────────────────────────────────────────────────────────────────
POSITIONS_FILE = "positions.json"

def load_positions() -> list:
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_positions(positions: list):
    with open(POSITIONS_FILE, "w") as f:
        json.dump(positions, f, indent=2)

# ── singleton trader instance ──────────────────────────────────────────────────
trader: PredictionTrader | None = None
ws_task: asyncio.Task | None = None
positions: list = load_positions()  # load from disk on startup

@asynccontextmanager
async def lifespan(app: FastAPI):
    global trader, ws_task
    trader = PredictionTrader()
    ws_task = asyncio.create_task(trader.connect_account())
    await asyncio.sleep(1.5)          # wait for ws handshake
    print("WebSocket connected and ready.")
    yield
    if ws_task:
        ws_task.cancel()
        try:
            await ws_task
        except asyncio.CancelledError:
            pass

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── request / response models ──────────────────────────────────────────────────

class ParseRequest(BaseModel):
    text: str

class ConfirmRequest(BaseModel):
    instrument_symbol: str
    event_title: str
    contract_label: str
    outcome: str
    ask_price: float
    contracts: int
    actual_cost: float
    potential_payout: float
    potential_profit: float
    implied_probability: float

# ── endpoints ──────────────────────────────────────────────────────────────────

@app.post("/api/parse")
async def parse(req: ParseRequest):
    """
    Step 1+2+3: parse intent, find market, get live price.
    Returns a trade confirmation object ready to show the user.
    """
    if trader is None:
        raise HTTPException(503, "Trader not initialized")

    try:
        intent = await trader.parse_intent(req.text)
        match = await trader.search_market(intent, req.text)
        ask = await trader.get_live_price(match["instrument_symbol"])
        details = trader.build_confirmation(match, ask, intent["dollar_amount"])
        return {"ok": True, "details": details}
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    except TimeoutError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"Unexpected error: {str(e)}"}


@app.post("/api/confirm")
async def confirm(req: ConfirmRequest):
    """
    Step 4+5: place the order and wait for fill.
    Returns success/failure.
    """
    if trader is None:
        raise HTTPException(503, "Trader not initialized")

    details = req.model_dump()

    try:
        order_id = await trader.place_order(details)
        await trader.await_fill(order_id, timeout=30.0)

        # record position
        positions.append({
            "order_id": order_id,
            "event_title": details["event_title"],
            "contract_label": details["contract_label"],
            "outcome": details["outcome"].upper(),
            "contracts": details["contracts"],
            "entry_price": details["ask_price"],
            "cost": details["actual_cost"],
            "potential_payout": details["potential_payout"],
            "potential_profit": details["potential_profit"],
            "implied_probability": details["implied_probability"],
            "status": "open",
            "filled_at": time.strftime("%H:%M:%S"),
        })
        save_positions(positions)

        return {"ok": True, "order_id": order_id, "message": "Order filled successfully!"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/portfolio")
async def portfolio():
    return {"ok": True, "positions": positions}


# ── serve frontend ─────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def root():
    return FileResponse("static/index.html")