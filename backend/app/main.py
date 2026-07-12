import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.auth import router as auth_router
from app.api.chat import router as chat_router
from app.api.garmin import router as garmin_router
from app.api.goals import router as goals_router
from app.api.health import router as health_router
from app.api.metrics import router as metrics_router
from app.api.plan import router as plan_router
from app.scheduler import register_jobs, scheduler
from app.telegram.bot import bot, dp, register_bot_commands

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await register_bot_commands()
    polling_task = asyncio.create_task(dp.start_polling(bot))
    register_jobs()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        polling_task.cancel()
        await bot.session.close()


app = FastAPI(title="Garmin Running Coach", lifespan=lifespan)
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(garmin_router)
app.include_router(metrics_router)
app.include_router(chat_router)
app.include_router(goals_router)
app.include_router(plan_router)

if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="static-assets")

    _static_root = STATIC_DIR.resolve()

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str) -> FileResponse:
        # Serves any real built file at its path (e.g. favicon), otherwise
        # falls back to index.html so React Router can handle client-side
        # routes like /goals, /plan, /chat on a fresh load or refresh.
        candidate = (STATIC_DIR / full_path).resolve()
        if candidate.is_relative_to(_static_root) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(STATIC_DIR / "index.html")
