import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import init_db
from worker import start_worker, stop_worker
from routers import submit, internal, admin, status

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up Family Radio API")
    init_db()
    start_worker()
    yield
    logger.info("Shutting down Family Radio API")
    stop_worker()


app = FastAPI(title="Family Radio API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(submit.router)
app.include_router(internal.router)
app.include_router(admin.router)
app.include_router(status.router)


@app.get("/health")
def health():
    return {"ok": True}
