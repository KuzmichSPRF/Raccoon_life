from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import logging
from database import init_db, upsert_user, update_stats, get_leaderboard

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = FastAPI(title="Raccoon Life API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class SyncData(BaseModel):
    type: str
    source: Optional[str] = None
    clown_games: int = 0
    clown_wins: int = 0
    vladeos_games: int = 0
    vladeos_wins: int = 0
    tower_max_level: int = 0
    tower_total_levels: int = 0
    quests: List[str] = []

class UserStats(BaseModel):
    user_id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    clown_games: int = 0
    clown_wins: int = 0
    vladeos_games: int = 0
    vladeos_wins: int = 0
    tower_max_level: int = 0
    tower_total_levels: int = 0
    quests: List[str] = []

@app.on_event("startup")
async def startup_event():
    init_db()
    logger.info("Backend started!")

@app.get("/")
async def root():
    return {"message": "Raccoon Life API works!", "status": "ok"}

@app.post("/api/sync")
async def sync_stats(data: SyncData, user_id: Optional[int] = None):
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    stats = data.dict(exclude={"type", "source"})
    if update_stats(user_id, stats):
        return {"status": "ok", "message": "Data saved"}
    raise HTTPException(status_code=500, detail="Error")

@app.post("/api/user")
async def register_user(user: UserStats):
    upsert_user(user.user_id, user.username, user.first_name, user.last_name)
    stats = user.dict(exclude={"user_id", "username", "first_name", "last_name"})
    update_stats(user.user_id, stats)
    return {"status": "ok", "message": "User registered"}

@app.get("/api/leaderboard")
async def leaderboard(limit: int = 10):
    leaders = get_leaderboard(limit)
    return {"status": "ok", "leaders": leaders}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
