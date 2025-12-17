from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from .database import engine
from .models import Base
from .routes import router

app = FastAPI(title="A4L Timesheet App")

# CHANGE THIS later (Render env var is better)
app.add_middleware(SessionMiddleware, secret_key="CHANGE_ME_TO_A_LONG_RANDOM_SECRET")

Base.metadata.create_all(bind=engine)

app.include_router(router)
