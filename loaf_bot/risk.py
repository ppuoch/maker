"""Hard risk limits and kill-switch."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from .accounting import Accounting
from .config import BotConfig

log = logging.getLogger(__name__)


class RiskManager:
    """Capital-first risk. Hard limits only — no soft suggestions."""

    def __init__(
        self,
        config: BotConfig,
        accounting: Accounting,
        flatten_callback: Optional[Callable[[], None]] = None,
    ):
        self.config = config
        self.accounting = accounting
        self.flatten_callback = flatten_callback
        self._lock = threading.Lock()
        self._halted_until: float = 0.0
        self._killed: bool = False
        self._last_inventory_check: dict = {}

    @property
    def is_killed(self) -> bool:
        return self._killed

    def is_halted(self) -> bool:
        return time.time() < self._halted_until or self._killed

    def halt(self, seconds: float, reason: str) -> None:
        with self._lock:
            target = time.time() + seconds
            if target > self._halted_until:
                self._halted_until = target
            log.warning("[risk] HALT %.0fs — %s", seconds, reason)

    def check_account_drawdown(self) -> bool:
        # Ignore the first 60s while marks/cash settle
        if time.time() - self.accounting._session_start < 60:
            return False
        dd = self.accounting.drawdown_pct()
        if dd >= self.config.max_drawdown_pct:
            log.error(
                "[risk] DRAWDOWN KILL %.2f%% >= %.2f%% — flattening",
                dd,
                self.config.max_drawdown_pct,
            )
            self._killed = True
            if self.flatten_callback:
                try:
                    self.flatten_callback()
                except Exception as e:
                    log.exception("[risk] flatten failed: %s", e)
            return True
        return False

    def check_position_notional(self, token: str, mark: float) -> bool:
        """Return True if position is within limits."""
        qty = abs(self.accounting.inventory(token))
        notional = qty * mark
        if notional > self.config.max_position_notional:
            log.warning(
                "[risk] position notional %.0f > max %.0f on %s",
                notional,
                self.config.max_position_notional,
                token,
            )
            return False
        return True

    def inventory_age(self, token: str) -> Optional[float]:
        snap = self.accounting.snapshot()
        pos = snap["positions"].get(token)
        if not pos:
            return None
        return pos.get("open_age_s")

    def should_force_reduce(self, token: str) -> bool:
        age = self.inventory_age(token)
        if age is not None and age > self.config.inventory_time_stop_s:
            log.warning(
                "[risk] inventory time-stop on %s (age %.0fs > %.0fs)",
                token,
                age,
                self.config.inventory_time_stop_s,
            )
            return True
        return False

    def allow_quote(self, token: str, side: str, size: float, price: float) -> bool:
        if self.is_halted() or self._killed:
            return False
        if size * price < self.config.min_notional and side == "BUY":
            return False
        if not self.check_position_notional(token, price):
            # Only allow reducing side
            inv = self.accounting.inventory(token)
            if side == "BUY" and inv >= 0:
                return False
            if side == "SELL" and inv <= 0:
                return False
        return True
