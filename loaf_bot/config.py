"""Configuration loaded from environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


@dataclass
class BotConfig:
    api_key: str = field(default_factory=lambda: os.environ.get("LOAF_API_KEY", ""))
    base_url: str = field(
        default_factory=lambda: os.environ.get(
            "LOAF_API_BASE_URL", "https://api.loafmarkets.com/api"
        )
    )
    ws_url: str = field(
        default_factory=lambda: os.environ.get(
            "LOAF_WS_URL", "wss://api.loafmarkets.com/ws"
        )
    )
    user_id: str = field(default_factory=lambda: os.environ.get("LOAF_USER_ID", ""))

    # Target token(s). Comma-separated for multi-asset later.
    target_token: str = field(
        default_factory=lambda: os.environ.get("LOAF_TARGET_TOKEN", "eiffel")
    )

    # Strategy timing
    tick_interval_s: float = field(
        default_factory=lambda: float(os.environ.get("LOAF_TICK_INTERVAL_S", "8.0"))
    )

    # Patient Maker defaults (conservative starting point — A/B later)
    inside_offset_ticks: int = 3
    tick_size: float = 0.01
    base_size: float = 4.0
    max_inventory: float = 40.0
    price_move_threshold_ticks: int = 3
    min_refresh_interval_s: float = 8.0

    # Risk
    max_drawdown_pct: float = field(
        default_factory=lambda: float(os.environ.get("LOAF_MAX_DRAWDOWN_PCT", "3.0"))
    )
    max_position_notional: float = field(
        default_factory=lambda: float(
            os.environ.get("LOAF_MAX_POSITION_NOTIONAL", "25000")
        )
    )
    inventory_time_stop_s: float = 1200.0  # 20 min
    min_notional: float = 12.0  # safety above platform 10 USDL

    # Network
    request_timeout_s: float = 10.0
    sustained_req_per_s: float = 1.2  # hard safety budget

    @property
    def tokens(self) -> List[str]:
        return [t.strip() for t in self.target_token.split(",") if t.strip()]

    def validate(self) -> None:
        if not self.api_key or len(self.api_key) < 32:
            raise ValueError(
                "LOAF_API_KEY is missing or too short. "
                "Create one at https://beta.loafmarkets.com/api"
            )
