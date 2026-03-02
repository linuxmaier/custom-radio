import logging
import os
from contextlib import asynccontextmanager

from database import init_db
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from metrics import start_metrics_poller, stop_metrics_poller
from routers import admin, auth, internal, push, status, submit
from worker import reset_stuck_jobs, start_worker, stop_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _station_name = os.getenv("STATION_NAME", "Family Radio")
    logger.info("Starting up %s API", _station_name)
    init_db()
    reset_stuck_jobs()
    start_worker()
    start_metrics_poller()
    yield
    logger.info("Shutting down %s API", _station_name)
    stop_worker()
    stop_metrics_poller()


app = FastAPI(title=os.getenv("STATION_NAME", "Family Radio") + " API", lifespan=lifespan)

_hostname = os.environ.get("SERVER_HOSTNAME", "")
_origins = [f"https://{_hostname}"] if _hostname else ["http://localhost", "http://localhost:8000"]

app.add_middleware(
    CORSMiddleware,  # ty: ignore[invalid-argument-type]
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "PATCH"],
    allow_headers=["Content-Type", "X-Admin-Token"],
)

app.include_router(auth.router)
app.include_router(submit.router)
app.include_router(internal.router)
app.include_router(admin.router)
app.include_router(status.router)
app.include_router(push.router)


@app.get("/health")
def health():
    return {"ok": True}
