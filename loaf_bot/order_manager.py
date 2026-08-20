"""OrderManager — single owner of all place / cancel network calls.

Strategy only emits DesiredQuote. This component decides whether a
network call is required, enforces timeouts, and owns the live order state.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional

log = logging.getLogger(__name__)

# We import the official SDK types at runtime so the package still
# documents cleanly if the SDK is not yet installed.
try:
    import loaf
    from loaf import LoafClient
    from loaf.exceptions import (
        CompetitionEligibilityError,
        LoafConnectionError,
        LoafRateLimitError,
        LoafServiceUnavailableError,
        LoafValidationError,
        TradingHaltedError,
    )
except ImportError:  # pragma: no cover
    loaf = None
    LoafClient = object  # type: ignore


@dataclass
class DesiredQuote:
    side: str          # BUY / SELL
    price: float
    size: float


@dataclass
class LiveOrder:
    order_id: int
    side: str
    price: float
    size: float
    placed_at: float
    token: str


class OrderManager:
    """
    Idempotent ensure() API.

    - Strategy calls ensure(side, price, size)
    - We cancel+replace only when the live order is meaningfully different
    - Every network call has an enforced timeout (via SDK client timeout)
    - On 503 we reconcile before any further action
    - Nonce lifecycle is owned by the official SDK (never exposed for reuse)
    """

    def __init__(
        self,
        client: "LoafClient",
        token: str,
        accounting,
        risk,
        tick_size: float = 0.01,
        price_tolerance_ticks: int = 2,
        request_timeout_s: float = 10.0,
    ):
        self.client = client
        self.token = token
        self.accounting = accounting
        self.risk = risk
        self.tick_size = tick_size
        self.price_tolerance = tick_size * price_tolerance_ticks
        self.request_timeout_s = request_timeout_s

        self._lock = threading.RLock()
        self._live: Dict[str, LiveOrder] = {}  # side -> LiveOrder
        self._last_reconcile = 0.0

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def ensure(self, side: str, price: float, size: float) -> None:
        """Make the live order for `side` match (price, size) with min network calls."""
        side = side.upper()
        price = round(price, 2)
        size = round(size, 1)

        if size < 0.1 or price <= 0:
            self.cancel_side(side)
            return

        if not self.risk.allow_quote(self.token, side, size, price):
            # Risk says no — if we have a live order on the increasing side, cancel it
            inv = self.accounting.inventory(self.token)
            increasing = (side == "BUY" and inv >= 0) or (side == "SELL" and inv <= 0)
            if increasing:
                self.cancel_side(side)
            return

        with self._lock:
            live = self._live.get(side)
            if live is not None:
                if (
                    abs(live.price - price) <= self.price_tolerance
                    and abs(live.size - size) <= 0.05
                ):
                    return  # already good — zero network cost

        # Need to move
        self.cancel_side(side)
        self._place(side, price, size)

    def cancel_side(self, side: str) -> None:
        side = side.upper()
        with self._lock:
            live = self._live.pop(side, None)
        if live is None:
            return
        self._cancel(live.order_id)

    def cancel_all(self) -> None:
        """Emergency path — bypass normal backoff."""
        with self._lock:
            lives = list(self._live.values())
            self._live.clear()
        for live in lives:
            try:
                self._cancel(live.order_id)
            except Exception as e:
                log.warning("[om] cancel_all failed for %s: %s", live.order_id, e)
        try:
            self.client.orders.cancel_all()
            self.accounting.record_request()
        except Exception as e:
            log.warning("[om] cancel_all (bulk) failed: %s", e)

    def on_ws_order_update(self, order_id: int, status: str, quantity_left: float = 0.0) -> None:
        """Drive local state from private portfolio WS order events."""
        status_u = (status or "").upper()
        with self._lock:
            for side, live in list(self._live.items()):
                if live.order_id == order_id:
                    if status_u in ("FILLED", "CANCELLED", "REJECTED", "EXPIRED"):
                        self._live.pop(side, None)
                        log.info("[om] %s order %s -> %s", side, order_id, status_u)
                    elif quantity_left is not None and quantity_left < 0.05:
                        self._live.pop(side, None)
                    break

    def on_fill(self, side: str, order_id: Optional[int] = None) -> None:
        """Optional: clear live order if fully filled (WS fill may arrive first)."""
        with self._lock:
            live = self._live.get(side.upper())
            if live and (order_id is None or live.order_id == order_id):
                # Leave it; on_ws_order_update will clean up on FILLED
                pass

    def live_snapshot(self) -> Dict[str, dict]:
        with self._lock:
            return {
                s: {
                    "order_id": o.order_id,
                    "price": o.price,
                    "size": o.size,
                    "age_s": round(time.time() - o.placed_at, 1),
                }
                for s, o in self._live.items()
            }

    # ------------------------------------------------------------------ #
    # Internal network
    # ------------------------------------------------------------------ #

    def _place(self, side: str, price: float, size: float) -> None:
        self.accounting.record_request()
        t0 = time.monotonic()
        try:
            # Official SDK handles nonce internally and never auto-retries place
            if side == "BUY":
                result = self.client.orders.limit_buy(
                    self.token, quantity=size, price=price
                )
            else:
                result = self.client.orders.limit_sell(
                    self.token, quantity=size, price=price
                )
            elapsed = (time.monotonic() - t0) * 1000
            order_id = getattr(result, "orderId", None) or getattr(
                result, "order_id", None
            )
            if order_id is None and isinstance(result, dict):
                order_id = result.get("orderId") or result.get("order_id")

            if order_id is not None:
                with self._lock:
                    self._live[side] = LiveOrder(
                        order_id=int(order_id),
                        side=side,
                        price=price,
                        size=size,
                        placed_at=time.time(),
                        token=self.token,
                    )
                log.info(
                    "[om] placed %s %s %.1f @ %.2f -> %s (%.0fms)",
                    side,
                    self.token,
                    size,
                    price,
                    order_id,
                    elapsed,
                )
            else:
                log.warning("[om] place returned no order_id: %s", result)

        except LoafRateLimitError as e:
            retry = getattr(e, "retry_after", 5.0) or 5.0
            log.warning("[om] 429 on place — pause %.1fs", retry)
            self.risk.halt(min(float(retry), 30.0), "rate limit on place")
        except LoafServiceUnavailableError:
            log.warning("[om] 503 on place — reconciling")
            self._reconcile()
        except (CompetitionEligibilityError, TradingHaltedError) as e:
            log.error("[om] cannot trade: %s", e)
            self.risk.halt(60.0, str(e))
        except LoafValidationError as e:
            log.error("[om] validation rejected: %s", e)
            if "insufficient" in str(e).lower():
                self.risk.halt(60.0, "insufficient balance")
        with self._lock:
            self._live.pop(side, None)
            # Do not retry invalid desired state
        except LoafConnectionError as e:
            log.warning("[om] connection error on place: %s", e)
            self.risk.halt(5.0, "connection error")
        except Exception as e:
            log.exception("[om] unexpected place error: %s", e)

    def _cancel(self, order_id: int) -> None:
        self.accounting.record_request()
        try:
            self.client.orders.cancel(order_id)
            log.info("[om] cancelled %s", order_id)
        except LoafRateLimitError as e:
            retry = getattr(e, "retry_after", 5.0) or 5.0
            self.risk.halt(min(float(retry), 15.0), "rate limit on cancel")
        except LoafServiceUnavailableError:
            log.warning("[om] 503 on cancel — treating as gone")
            self._reconcile()
        except Exception as e:
            # Cancel-on-filled race is common and benign
            msg = str(e).lower()
            if "filled" in msg or "no longer" in msg or "not found" in msg:
                log.info("[om] cancel race (already gone) %s", order_id)
            else:
                log.warning("[om] cancel failed %s: %s", order_id, e)

    def _reconcile(self) -> None:
        """On 503 / ambiguity: ask the exchange what is actually live."""
        now = time.time()
        if now - self._last_reconcile < 5.0:
            return
        self._last_reconcile = now
        self.accounting.record_request()
        try:
            active = self.client.history.active_orders()
            # active_orders may return a list or an object with .activeOrders
            if hasattr(active, "activeOrders"):
                orders = active.activeOrders or []
            elif isinstance(active, dict):
                orders = active.get("activeOrders") or active.get("orders") or []
            else:
                orders = list(active) if active else []

            live_ids = set()
            for o in orders:
                oid = getattr(o, "orderId", None) or (
                    o.get("orderId") if isinstance(o, dict) else None
                )
                tok = getattr(o, "tokenName", None) or (
                    o.get("tokenName") if isinstance(o, dict) else None
                )
                if oid is not None and (tok is None or tok == self.token):
                    live_ids.add(int(oid))

            with self._lock:
                for side, live in list(self._live.items()):
                    if live.order_id not in live_ids:
                        log.info(
                            "[om] reconcile: dropping stale local %s order %s",
                            side,
                            live.order_id,
                        )
                        self._live.pop(side, None)
            log.info("[om] reconcile done — %d live on exchange for token", len(live_ids))
        except Exception as e:
            log.warning("[om] reconcile failed: %s", e)
