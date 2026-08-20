"""Patient Maker strategy — sizeable resting liquidity, low replace frequency."""

from __future__ import annotations

import logging
import math
import time
from typing import Optional, Tuple

from .accounting import Accounting
from .config import BotConfig
from .order_manager import OrderManager
from .risk import RiskManager

log = logging.getLogger(__name__)


class PatientMaker:
    """
    Competition-default market maker.

    - Posts sizeable orders at offset ticks from the touch
    - Replaces only when mid has moved meaningfully or inventory skew demands it
    - Strong inventory skew + time-stop
    - Never improves the touch solely for queue priority if it costs a cancel+place
    """

    name = "PatientMaker"

    def __init__(
        self,
        config: BotConfig,
        order_manager: OrderManager,
        accounting: Accounting,
        risk: RiskManager,
        token: str,
    ):
        self.config = config
        self.om = order_manager
        self.accounting = accounting
        self.risk = risk
        self.token = token

        self.best_bid: Optional[float] = None
        self.best_ask: Optional[float] = None
        self.mark: Optional[float] = None
        self._last_mid: Optional[float] = None
        self._last_quote_ts: float = 0.0
        self._last_desired: dict = {}

    # ------------------------------------------------------------------ #
    # Market data hooks (called from WS / main loop)
    # ------------------------------------------------------------------ #

    def on_book(self, bid: Optional[float], ask: Optional[float]) -> None:
        self.best_bid = bid
        self.best_ask = ask

    def on_mark(self, mark: float) -> None:
        self.mark = mark
        self.accounting.on_mark(self.token, mark)

    # ------------------------------------------------------------------ #
    # Main decision
    # ------------------------------------------------------------------ #

    def on_tick(self) -> None:
        if self.risk.is_halted() or self.risk.is_killed:
            return

        if self.best_bid is None or self.best_ask is None:
            return
        if self.best_bid >= self.best_ask:
            log.debug("[%s] inverted book — skip", self.token)
            return

        mid = (self.best_bid + self.best_ask) / 2.0
        natural_spread_bps = (self.best_ask - self.best_bid) / mid * 10000
        if natural_spread_bps > 80:
            log.info("[%s] natural spread %.1f bps too wide — skip", self.token, natural_spread_bps)
            return

        # Account-level risk
        if self.risk.check_account_drawdown():
            return

        inv = self.accounting.inventory(self.token)
        now = time.time()

        # Throttle
        if now - self._last_quote_ts < self.config.min_refresh_interval_s:
            # Still allow force-reduce path
            if not self.risk.should_force_reduce(self.token):
                return

        # Force reduce if inventory is old
        if self.risk.should_force_reduce(self.token) and abs(inv) > 0.1:
            self._force_reduce(inv, mid)
            self._last_quote_ts = now
            return

        # Compute desired quotes
        bid_px, ask_px, bid_sz, ask_sz = self._compute_quotes(mid, inv)

        # Only replace if price moved enough from last desired (or first time)
        last = self._last_desired
        move_thresh = self.config.tick_size * self.config.price_move_threshold_ticks

        def needs_update(side: str, px: float, sz: float) -> bool:
            prev = last.get(side)
            if prev is None:
                return True
            if abs(prev["price"] - px) > move_thresh:
                return True
            if abs(prev["size"] - sz) > 0.15:
                return True
            return False

        if bid_sz >= 0.1 and needs_update("BUY", bid_px, bid_sz):
            self.om.ensure("BUY", bid_px, bid_sz)
            self._last_desired["BUY"] = {"price": bid_px, "size": bid_sz}
        elif bid_sz < 0.1:
            self.om.cancel_side("BUY")
            self._last_desired.pop("BUY", None)

        if ask_sz >= 0.1 and needs_update("SELL", ask_px, ask_sz):
            self.om.ensure("SELL", ask_px, ask_sz)
            self._last_desired["SELL"] = {"price": ask_px, "size": ask_sz}
        elif ask_sz < 0.1:
            self.om.cancel_side("SELL")
            self._last_desired.pop("SELL", None)

        self._last_quote_ts = now
        self._last_mid = mid

    def _compute_quotes(
        self, mid: float, inv: float
    ) -> Tuple[float, float, float, float]:
        cfg = self.config
        offset = cfg.tick_size * cfg.inside_offset_ticks

        # Inventory skew: pull the increasing side away, tighten reducing side
        inv_ratio = inv / cfg.max_inventory if cfg.max_inventory > 0 else 0.0
        inv_ratio = max(-1.0, min(1.0, inv_ratio))
        skew_ticks = int(abs(inv_ratio) * 8)  # up to 8 ticks of skew

        if inv > 0:
            # Long: pull bid deeper, tighten ask
            buy_off = offset + skew_ticks * cfg.tick_size
            sell_off = max(cfg.tick_size, offset - skew_ticks * cfg.tick_size)
        elif inv < 0:
            buy_off = max(cfg.tick_size, offset - skew_ticks * cfg.tick_size)
            sell_off = offset + skew_ticks * cfg.tick_size
        else:
            buy_off = sell_off = offset

        # Improve from the touch by the offset (patient: we are not fighting for
        # every queue slot, we are providing size at a competitive distance)
        # For offset=3 we sit 3 ticks inside the current best.
        my_bid = round(self.best_bid + buy_off, 2) if self.best_bid else 0.0
        my_ask = round(self.best_ask - sell_off, 2) if self.best_ask else 0.0

        if my_bid >= my_ask:
            # Skew pushed us into a cross — fall back to touch
            my_bid = round(self.best_bid, 2)
            my_ask = round(self.best_ask, 2)

        # Size
        base = cfg.base_size
        inv_scale = 1.0 - 0.5 * abs(inv_ratio)
        size = max(0.1, round(base * inv_scale, 1))

        bid_sz = size
        ask_sz = size

        # Long-only platform rule: cannot sell more than we hold
        if inv <= 0:
            ask_sz = 0.0
        elif inv < ask_sz:
            ask_sz = round(max(0.1, inv), 1)

        # Urgency bands
        if abs(inv_ratio) >= 0.9:
            if inv > 0:
                bid_sz = 0.0
            else:
                ask_sz = 0.0
        elif abs(inv_ratio) >= 0.7:
            if inv > 0:
                bid_sz = 0.0
            else:
                ask_sz = 0.0

        return my_bid, my_ask, bid_sz, ask_sz

    def _force_reduce(self, inv: float, mid: float) -> None:
        if abs(inv) < 0.1:
            return
        if inv > 0:
            # Sell to reduce
            px = round((self.best_bid or mid) - self.config.tick_size, 2)
            sz = round(min(abs(inv), self.config.base_size * 2), 1)
            log.warning("[%s] FORCE REDUCE SELL %.1f @ %.2f", self.token, sz, px)
            self.om.ensure("SELL", px, sz)
            self.om.cancel_side("BUY")
        else:
            px = round((self.best_ask or mid) + self.config.tick_size, 2)
            sz = round(min(abs(inv), self.config.base_size * 2), 1)
            log.warning("[%s] FORCE REDUCE BUY %.1f @ %.2f", self.token, sz, px)
            self.om.ensure("BUY", px, sz)
            self.om.cancel_side("SELL")
