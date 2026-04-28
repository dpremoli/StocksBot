from pydantic import BaseModel
from typing import Dict, List


class BatchRequest(BaseModel):
    isins: List[str]
    currency: str = "EUR"


class BatchResponse(BaseModel):
    prices: Dict[str, float]
    errors: Dict[str, str] = {}
