from pydantic import BaseModel
from typing import Dict, List, Optional


class BatchRequest(BaseModel):
    isins: List[str]
    currency: str = "EUR"


class BatchResponse(BaseModel):
    prices: Dict[str, float]
    errors: Dict[str, str] = {}


class HistoryPoint(BaseModel):
    date: str
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: float


class HistoryResponse(BaseModel):
    isin: str
    currency: str
    xid: str
    prices: List[HistoryPoint]
