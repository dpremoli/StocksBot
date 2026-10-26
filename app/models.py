from pydantic import BaseModel
from typing import Dict, List, Optional


class BatchRequest(BaseModel):
    isins: List[str]
    currency: str = "EUR"


class BatchResponse(BaseModel):
    prices: Dict[str, float]
    errors: Dict[str, str] = {}


class AllocationItem(BaseModel):
    name: str
    weight: float  # percentage, e.g. 63.5


class FundInfo(BaseModel):
    isin: str
    name: Optional[str] = None
    asset_type: Optional[str] = None   # e.g. "Equity", "Fixed Income", "Mixed"
    fund_type: Optional[str] = None    # e.g. "ETF", "OEIC", "Fund"
    regions: List[AllocationItem] = []
    sectors: List[AllocationItem] = []
    assets: List[AllocationItem] = []  # asset-class breakdown (equity/bond/cash etc.)


class BatchInfoRequest(BaseModel):
    isins: List[str]
    currency: str = "EUR"


class BatchInfoResponse(BaseModel):
    info: Dict[str, FundInfo]
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
    symbol: str
    prices: List[HistoryPoint]
