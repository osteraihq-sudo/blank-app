# models.py
from typing import Optional
from sqlmodel import SQLModel, Field

# Explicit table names avoid keyword collisions
class Note(SQLModel, table=True):
    __tablename__ = "notes"
    id: Optional[int] = Field(default=None, primary_key=True)
    content: str
    color: str = "#FFF176"

class List(SQLModel, table=True):
    __tablename__ = "lists"
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str

class ListItem(SQLModel, table=True):
    __tablename__ = "list_items"
    id: Optional[int] = Field(default=None, primary_key=True)
    list_id: int = Field(foreign_key="lists.id")
    text: str
    done: bool = False

class Document(SQLModel, table=True):
    __tablename__ = "documents"
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    content: str = ""
