from pydantic import BaseModel, Field


class Product(BaseModel):
    id: str
    available: int


class Reservation(BaseModel):
    quantity: int = Field(gt=0, le=10)
