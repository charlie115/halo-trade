"""Wire contracts for the agent API. Shared by server and tests."""
from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

STOP_ATR_MENU = (1.5, 2.0, 3.0)
TAKE_PROFIT_ATR_MENU = (2.0, 3.0, 4.0)


class Outcome(str, Enum):
    FILLED = "filled"
    CLOSED = "closed"
    REJECTED = "rejected"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ErrorCode(str, Enum):
    UNAUTHORIZED = "UNAUTHORIZED"
    PAUSED = "PAUSED"
    GRANT_MISSING = "GRANT_MISSING"
    GRANTEE_LOW_GAS = "GRANTEE_LOW_GAS"
    MARKET_UNAVAILABLE = "MARKET_UNAVAILABLE"
    STALE_OBSERVATION = "STALE_OBSERVATION"
    STALE_DECISION = "STALE_DECISION"
    INVALID_INTENT = "INVALID_INTENT"
    POSITION_OPEN = "POSITION_OPEN"
    NO_POSITION = "NO_POSITION"
    WINDOW_USED = "WINDOW_USED"
    COOLDOWN = "COOLDOWN"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    DRAWDOWN_LIMIT = "DRAWDOWN_LIMIT"
    OVER_CAP = "OVER_CAP"
    TP_TOO_CLOSE = "TP_TOO_CLOSE"
    SIZE_TOO_SMALL = "SIZE_TOO_SMALL"
    BALANCE_TOO_LOW = "BALANCE_TOO_LOW"
    BOOK_TOO_THIN = "BOOK_TOO_THIN"
    SETUP_NOT_MET = "SETUP_NOT_MET"
    TICKET_EXPIRED = "TICKET_EXPIRED"
    TICKET_USED = "TICKET_USED"
    TICKET_NOT_FOUND = "TICKET_NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    CHAIN_REJECTED = "CHAIN_REJECTED"
    STOP_FAILED_FLATTENED = "STOP_FAILED_FLATTENED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    INTERNAL = "INTERNAL"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorOut(BaseModel):
    ok: Literal[False] = False
    outcome: Outcome
    code: ErrorCode
    limit: Optional[str] = None  # name=value of the binding limit
    retry_after_window: Optional[int] = None
    check_state_after_s: Optional[int] = None


class MarketOut(BaseModel):
    ticker: str
    mark_price: float
    oracle_price: float
    best_bid: float
    best_ask: float
    spread_bps: float
    funding_rate_bps: float


class FeaturesOut(BaseModel):
    atr_1h: float
    atr_1h_pct: float
    ema20_1h: float
    ema50_1h: float
    trend: Literal["up", "down", "flat"]
    ret_1h_pct: float
    ret_4h_pct: float
    ret_24h_pct: float
    volatility_ratio: float  # atr14 / atr50 on 1h candles
    round_trip_cost_bps: float
    # From closed 15-minute candles. None means they could not be read, never zero.
    ret_15m_pct: Optional[float] = None
    atr_15m_pct: Optional[float] = None
    trend_15m: Optional[Literal["up", "down", "flat"]] = None


class AccountOut(BaseModel):
    equity_usd: float
    available_usd: float
    day_pnl_pct: float
    drawdown_pct: float


class PositionOut(BaseModel):
    side: Literal["long", "short"]
    quantity: float
    entry_price: float
    unrealized_pnl_usd: float
    stop_price: Optional[float]
    take_profit_price: Optional[float]
    windows_held: int


class DecisionOut(BaseModel):
    window_id: int
    action: str
    side: Optional[str]
    result: str
    reason: str


class StateOut(BaseModel):
    ok: Literal[True] = True
    server_time_ms: int
    window_id: int
    next_window_ms: int
    observation_id: str
    market: MarketOut
    features: FeaturesOut
    account: AccountOut
    position: Optional[PositionOut]
    can_open: bool
    blocked_reasons: list[ErrorCode]
    last_decisions: list[DecisionOut] = Field(default_factory=list)


class PreviewIn(_Strict):
    observation_id: str = Field(min_length=8, max_length=128)
    side: Literal["long", "short"]
    stop_atr: float
    take_profit_atr: Optional[float] = None
    reason: str = Field(default="", max_length=280)

    @field_validator("stop_atr")
    @classmethod
    def _stop_menu(cls, v: float) -> float:
        if v not in STOP_ATR_MENU:
            raise ValueError("stop_atr not in menu")
        return v

    @field_validator("take_profit_atr")
    @classmethod
    def _tp_menu(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v not in TAKE_PROFIT_ATR_MENU:
            raise ValueError("take_profit_atr not in menu")
        return v


class PreviewOut(BaseModel):
    ok: Literal[True] = True
    ticket: str
    expires_ms: int
    side: Literal["long", "short"]
    quantity: float
    notional_usd: float
    mark_price: float
    stop_price: float
    take_profit_price: Optional[float]
    worst_loss_usd: float
    worst_loss_is_estimate: Literal[True] = True
    day_loss_remaining_usd: float


class SubmitIn(_Strict):
    ticket: str = Field(min_length=8, max_length=128)


class CloseIn(_Strict):
    reason: str = Field(default="", max_length=280)


class SubmitOut(BaseModel):
    ok: Literal[True] = True
    outcome: Outcome
    window_id: int
    side: Optional[str] = None
    quantity: Optional[float] = None
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    take_profit_price: Optional[float] = None
    stop_live: Optional[bool] = None
    tx_hashes: list[str] = Field(default_factory=list)
    code: Optional[ErrorCode] = None
    check_state_after_s: Optional[int] = None
