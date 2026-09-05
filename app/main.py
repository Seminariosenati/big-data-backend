from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app.config.settings import get_settings
from app.routers import auth, profile, datasets, users, projects, admin
from app.routers import settings as settings_router

settings = get_settings()

limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="Datalume Backend", version="0.1.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(datasets.router)
app.include_router(settings_router.router)
app.include_router(users.router)
app.include_router(projects.router)
app.include_router(admin.router)


@app.get("/health")
def health():
    return {"status": "ok"}