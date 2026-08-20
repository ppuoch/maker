"""Authoritative local ledger — realised + unrealised PnL, volume, maker %."""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class FillRecord:
    ts: float
    token: str
    side: str          # BUY / SELL
    price: float
    quantity: float
    order_id: Optional[int] = None
    is_maker: bool = True
    fee: float = 0.0


@dataclass
class PositionState:
    quantity: float = 0.0
    avg_entry: float = 0.0
    realised_pnl: float = 0.0
    volume: float = 0.0
    maker_volume: float = 0.0
    taker_volume: float = 0.0
    open_since: Optional[float] = None


class Accounting:
    """Thread-safe local ledger driven by WS fills + mark prices."""

    def __init__(self, starting_cash: float = 100_000.0):
        self._lock = threading.RLock()
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.peak_equity = starting_cash
        self._positions: Dict[str, PositionState] = defaultdict(PositionState)
        self._marks: Dict[str, float] = {}
        self._fills: Deque[FillRecord] = deque(maxlen=5000)
        self._session_start = time.time()
        self.total_requests = 0

    # ------------------------------------------------------------------ #
    # Updates
    # ------------------------------------------------------------------ #

    def on_fill(
        self,
        token: str,
        side: str,
        price: float,
        quantity: float,
        *,
        order_id: Optional[int] = None,
        is_maker: bool = True,
        fee: float = 0.0,
    ) -> None:
        with self._lock:
            pos = self._positions[token]
            notional = price * quantity
            self._fills.append(
                FillRecord(
                    ts=time.time(),
                    token=token,
                    side=side.upper(),
                    price=price,
                    quantity=quantity,
                    order_id=order_id,
                    is_maker=is_maker,
                    fee=fee,
                )
            )
            pos.volume += notional
            if is_maker:
                pos.maker_volume += notional
            else:
                pos.taker_volume += notional

            side_u = side.upper()
            if side_u == "BUY":
                # Increase long inventory
                new_qty = pos.quantity + quantity
                if pos.quantity >= 0:
                    # Adding to long or opening long
                    if new_qty > 0:
                        pos.avg_entry = (
                            (pos.avg_entry * pos.quantity + price * quantity) / new_qty
                            if new_qty > 1e-9
                            else price
                        )
                    pos.quantity = new_qty
                else:
                    # Covering short
                    cover = min(quantity, abs(pos.quantity))
                    pos.realised_pnl += (pos.avg_entry - price) * cover
                    pos.quantity += quantity
                    if pos.quantity > 0:
                        pos.avg_entry = price
            else:  # SELL
                if pos.quantity > 0:
                    sell_qty = min(quantity, pos.quantity)
                    pos.realised_pnl += (price - pos.avg_entry) * sell_qty
                    pos.quantity -= quantity
                    if pos.quantity < 0:
                        # Flipped to short
                        pos.avg_entry = price
                else:
                    # Adding to short or opening short
                    new_qty = pos.quantity - quantity
                    if pos.quantity <= 0 and new_qty < 0:
                        pos.avg_entry = (
                            (pos.avg_entry * abs(pos.quantity) + price * quantity)
                            / abs(new_qty)
                            if abs(new_qty) > 1e-9
                            else price
                        )
                    pos.quantity = new_qty

            if abs(pos.quantity) < 0.05:
                pos.quantity = 0.0
                pos.open_since = None
            elif pos.open_since is None:
                pos.open_since = time.time()

            self.cash -= fee  # approximate; exact cash comes from portfolio WS

    def on_mark(self, token: str, mark: float) -> None:
        with self._lock:
            self._marks[token] = mark
            self._update_peak()

    def on_cash(self, cash: float, frozen: float = 0.0) -> None:
        with self._lock:
            self.cash = cash
            self._update_peak()

    def record_request(self) -> None:
        with self._lock:
            self.total_requests += 1

    def _update_peak(self) -> None:
        eq = self.equity()
        if eq > self.peak_equity:
            self.peak_equity = eq

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #

    def inventory(self, token: str) -> float:
        with self._lock:
            return self._positions[token].quantity

    def avg_entry(self, token: str) -> float:
        with self._lock:
            return self._positions[token].avg_entry

    def unrealised(self, token: str) -> float:
        with self._lock:
            pos = self._positions[token]
            mark = self._marks.get(token)
            if mark is None or abs(pos.quantity) < 1e-9:
                return 0.0
            if pos.quantity > 0:
                return (mark - pos.avg_entry) * pos.quantity
            return (pos.avg_entry - mark) * abs(pos.quantity)

    def realised(self, token: Optional[str] = None) -> float:
        with self._lock:
            if token:
                return self._positions[token].realised_pnl
            return sum(p.realised_pnl for p in self._positions.values())

    def equity(self) -> float:
        with self._lock:
            u = sum(self.unrealised(t) for t in self._positions)
            # Approximate: cash + unrealised. Exact mark-to-market of holdings
            # is better once we have full portfolio component, but this is
            # directionally correct for risk.
            return self.cash + u

    def drawdown_pct(self) -> float:
        with self._lock:
            if self.peak_equity <= 0:
                return 0.0
            return (self.peak_equity - self.equity()) / self.peak_equity * 100.0

    def total_volume(self) -> float:
        with self._lock:
            return sum(p.volume for p in self._positions.values())

    def maker_ratio(self) -> float:
        with self._lock:
            m = sum(p.maker_volume for p in self._positions.values())
            t = sum(p.taker_volume for p in self._positions.values())
            tot = m + t
            return m / tot if tot > 0 else 0.0

    def requests_per_dollar(self) -> float:
        with self._lock:
            vol = self.total_volume()
            return self.total_requests / vol if vol > 0 else 0.0

    def snapshot(self) -> dict:
        with self._lock:
            positions = {}
            for tok, pos in self._positions.items():
                if abs(pos.quantity) < 1e-9 and abs(pos.realised_pnl) < 1e-6:
                    continue
                positions[tok] = {
                    "qty": round(pos.quantity, 4),
                    "avg_entry": round(pos.avg_entry, 4),
                    "unrealised": round(self.unrealised(tok), 2),
                    "realised": round(pos.realised_pnl, 2),
                    "volume": round(pos.volume, 2),
                    "open_age_s": (
                        round(time.time() - pos.open_since, 1)
                        if pos.open_since
                        else None
                    ),
                }
            return {
                "cash": round(self.cash, 2),
                "equity": round(self.equity(), 2),
                "peak_equity": round(self.peak_equity, 2),
                "drawdown_pct": round(self.drawdown_pct(), 3),
                "realised_pnl": round(self.realised(), 2),
                "total_volume": round(self.total_volume(), 2),
                "maker_ratio": round(self.maker_ratio(), 4),
                "requests": self.total_requests,
                "req_per_dollar": round(self.requests_per_dollar(), 6),
                "session_age_s": round(time.time() - self._session_start, 1),
                "positions": positions,
            }

    def log_status(self) -> None:
        s = self.snapshot()
        log.info(
            "[accounting] equity=%.2f dd=%.2f%% realised=%.2f vol=%.0f maker=%.1f%% "
            "req=%d req/$=%.6f",
            s["equity"],
            s["drawdown_pct"],
            s["realised_pnl"],
            s["total_volume"],
            s["maker_ratio"] * 100,
            s["requests"],
            s["req_per_dollar"],
        )
        for tok, p in s["positions"].items():
            log.info(
                "  %s qty=%.2f avg=%.2f u=%.2f r=%.2f age=%s",
                tok,
                p["qty"],
                p["avg_entry"],
                p["unrealised"],
                p["realised"],
                p["open_age_s"],
            )
