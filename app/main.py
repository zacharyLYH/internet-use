from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()


class Health(BaseModel):
    status: str


@app.get("/health")
def health() -> Health:
    return Health(status="ok")
