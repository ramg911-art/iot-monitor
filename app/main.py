"""IoT Monitor - FastAPI application."""
import asyncio
import logging
from contextlib import asynccontextmanager

import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.config import get_settings
from app.database import init_db
from app.background.tasks import start_background_tasks
from app.routers import auth, devices, tapo, ewelink, cameras, admin
from app.websocket import ws_manager
from app.websocket.manager import heartbeat_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_bg_tasks = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    tasks = await start_background_tasks()
    _bg_tasks.extend(tasks)
    yield
    for t in _bg_tasks:
        t.cancel()
    logger.info("Shutdown complete")


app = FastAPI(
    title="IoT Monitor",
    description="Production-grade single-user IoT monitoring platform",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

app.include_router(auth.router)
app.include_router(devices.router)
app.include_router(tapo.router)
app.include_router(ewelink.router)
app.include_router(cameras.router)
app.include_router(admin.router)


@app.get("/")
async def root():
    """Serve frontend."""
    static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
    index = os.path.join(static_dir, "index.html")
    if os.path.exists(index):
        return FileResponse(index)
    return {"message": "IoT Monitor API", "docs": "/docs"}


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket with JWT auth. Pass token as query param: ?token=..."""
    token = websocket.query_params.get("token")
    user_id = await ws_manager.connect(websocket, token)
    if user_id is None:
        return
    try:
        heartbeat_task = None
        try:
            heartbeat_task = asyncio.create_task(heartbeat_loop(websocket, 30))
            while True:
                data = await websocket.receive_text()
                # Echo or handle client messages if needed
                if data == "ping":
                    await websocket.send_text('{"type":"pong","data":{}}')
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.warning("WebSocket error: %s", e)
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
    finally:
        await ws_manager.disconnect(websocket)


# Serve static files
static_dir = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
