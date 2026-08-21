"""
app/main.py

The FastAPI application. Routers are attached here so this file stays a wiring
diagram rather than somewhere logic accumulates.

The monitor loop starts with the app. It runs in-process because Render keeps
one service alive continuously, and because the loop itself is what stops a free
service from idling to sleep.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import auth, balances, collectibles, transactions, wallets
from app.core.errors import RequestError, to_error_response
from app.services import monitor

logging.basicConfig(level=logging.INFO)

# httpx logs every outbound request at INFO. A poll cycle makes hundreds, which
# buries the lines that actually say something happened.
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(monitor.run_forever())
    yield
    task.cancel()


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
app.include_router(balances.router)
app.include_router(collectibles.router)
app.include_router(transactions.router)
