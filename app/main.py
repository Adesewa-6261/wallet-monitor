"""
app/main.py

The FastAPI application. Routers are attached here so this file stays a wiring
diagram rather than somewhere logic accumulates.

Two background loops start with the app: the monitor, which polls chains and
sends alerts, and the Telegram bot, which answers commands. Both run in-process
because Render keeps one service alive continuously, and because the loops
themselves are what stop a free service from idling to sleep.

The bot does not deliver alerts — the monitor does that directly. The bot is
the command surface, and it simply does not start if no token is configured.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import auth, transactions, wallets
from app.core.errors import RequestError, to_error_response
from app.services import monitor
from bot import main as bot

logging.basicConfig(level=logging.INFO)

# httpx logs every request URL at INFO, and the Telegram bot token lives inside
# the URL (api.telegram.org/bot<TOKEN>/sendMessage). At INFO that token lands in
# the Render log stream in plaintext, where anyone with dashboard access can
# read it — and a leaked bot token lets someone send messages as us.
#
# Raised to WARNING rather than disabled: real transport failures still surface.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(_: FastAPI):
    tasks = [
        asyncio.create_task(monitor.run_forever()),
        asyncio.create_task(bot.run_forever()),
    ]
    yield
    for task in tasks:
        task.cancel()
    # Let the cancellations settle so neither loop logs a spurious failure on
    # the way out.
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(
    title="WalletNest API",
    description="Wallet monitoring and transaction alerts.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.exception_handler(RequestError)
async def handle_request_error(_: Request, err: RequestError) -> JSONResponse:
    status, body = to_error_response(err)
    return JSONResponse(status_code=status, content=body)


@app.exception_handler(Exception)
async def handle_unexpected_error(_: Request, err: Exception) -> JSONResponse:
    status, body = to_error_response(err)
    return JSONResponse(status_code=status, content=body)


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/api/monitor/run")
async def run_monitor_once() -> dict:
    """
    Trigger one poll cycle immediately instead of waiting for the timer.
    Useful while testing, and for an external pinger to keep the service warm.
    """
    return await monitor.run_cycle()


app.include_router(auth.router)
app.include_router(wallets.router)
app.include_router(transactions.router)
