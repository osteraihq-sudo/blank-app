# db.py
from sqlmodel import SQLModel, create_engine, Session

DB_URL = "sqlite:///hive.db"
# Streamlit uses threads; this flag keeps SQLite happy
engine = create_engine(DB_URL, echo=False, connect_args={"check_same_thread": False})

def init_db():
    SQLModel.metadata.create_all(engine)

def get_session() -> Session:
    return Session(engine)
