#!/usr/bin/env python3
"""
Loaf R2 Patient Maker — main entry point.

Usage:
    export LOAF_API_KEY=...
    export LOAF_TARGET_TOKEN=eiffel
    python -m loaf_bot.main

Or after install:
    loaf-bot
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from typing import Optional

from .accounting import Accounting
from .config import BotConfig
from .order_manager import OrderManager
from .risk import RiskManager
from .strategy import PatientMaker

log = logging.getLogger("loaf_bot")


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def build_sdk_client(config: BotConfig):
    """Construct the official LoafClient. Fails clearly if SDK missing."""
    try:
        from loaf import LoafClient
    except ImportError as e:
        log.error(
            "Official Loaf SDK not installed.\n"
            "  pip install \"git+https://github.com/Loaf-Markets/loaf-python-api-bot-template.git\"\n"
            "  or: cd vendor && git clone ... && pip install -e .\n"
            "Original error: %s",
            e,
        )
        sys.exit(1)

    client = LoafClient(
        api_key=config.api_key,
        base_url=config.base_url,
        # timeout is handled by httpx inside the SDK; we still pass if supported
    )
    return client


def preflight(client, config: BotConfig) -> float:
    """Verify credentials and return starting cash."""
    try:
        from loaf.exceptions import LoafAuthError, CompetitionEligibilityError
    except ImportError:
        LoafAuthError = Exception  # type: ignore
        CompetitionEligibilityError = Exception  # type: ignore

    log.info("Preflight: portfolio.component() ...")
    try:
        pf = client.portfolio.component()
    except LoafAuthError as e:
        log.error("Auth failed — check LOAF_API_KEY: %s", e)
        sys.exit(1)
    except Exception as e:
        log.error("Preflight failed: %s", e)
        sys.exit(1)

    cash = float(getattr(pf, "cash", None) or pf.get("cash", 0) or 0)
    log.info("Cash available: %.2f USDL", cash)

    # Eligibility (non-fatal outside active round)
    try:
        q = client.competition.queue_position()
        log.info("Competition queue position: %s", q)
    except Exception as e:
        log.debug("queue_position not available (ok outside round): %s", e)

    return cash


class BotRuntime:
    def __init__(self, config: BotConfig):
        self.config = config
        self.client = build_sdk_client(config)
        self.starting_cash = preflight(self.client, config)
        self.accounting = Accounting(starting_cash=self.starting_cash)
        self.risk = RiskManager(
            config,
            self.accounting,
            flatten_callback=self._emergency_flatten,
        )
        self.token = config.tokens[0]
        self.om = OrderManager(
            self.client,
            self.token,
            self.accounting,
            self.risk,
            tick_size=config.tick_size,
            price_tolerance_ticks=config.price_move_threshold_ticks,
            request_timeout_s=config.request_timeout_s,
        )
        self.strategy = PatientMaker(
            config, self.om, self.accounting, self.risk, self.token
        )
        self._stop = threading.Event()
        self._ws = None

    def _emergency_flatten(self) -> None:
        log.error("[runtime] EMERGENCY FLATTEN")
        try:
            self.om.cancel_all()
        except Exception as e:
            log.exception("cancel_all failed: %s", e)

    def _on_orderbook(self, msg) -> None:
        try:
            bids = getattr(msg, "bids", None) or msg.get("bids") or []
            asks = getattr(msg, "asks", None) or msg.get("asks") or []
            bid = float(bids[0]["price"] if isinstance(bids[0], dict) else bids[0].price) if bids else None
            ask = float(asks[0]["price"] if isinstance(asks[0], dict) else asks[0].price) if asks else None
            self.strategy.on_book(bid, ask)
        except Exception as e:
            log.debug("orderbook parse: %s", e)

    def _on_mark(self, msg) -> None:
        try:
            px = float(getattr(msg, "price", None) or msg.get("price"))
            self.strategy.on_mark(px)
        except Exception as e:
            log.debug("mark parse: %s", e)

    def _on_fill(self, msg) -> None:
        try:
            t = getattr(msg, "trade", None) or msg
            side = getattr(t, "side", None) or t.get("side")
            price = float(getattr(t, "price", None) or t.get("price"))
            qty = float(getattr(t, "quantity", None) or t.get("quantity"))
            fee = float(getattr(t, "fee", 0) or (t.get("fee") or 0))
            oid = getattr(t, "orderId", None) or t.get("orderId")
            token = getattr(t, "tokenName", None) or t.get("tokenName") or self.token
            # Assume maker unless fee suggests otherwise (Loaf maker fee is 0)
            is_maker = fee <= 0
            self.accounting.on_fill(
                token, side, price, qty, order_id=oid, is_maker=is_maker, fee=fee
            )
            self.om.on_fill(side, oid)
            log.info(
                "[fill] %s %s %.4f @ %.2f fee=%.4f maker=%s",
                side, token, qty, price, fee, is_maker,
            )
        except Exception as e:
            log.warning("fill parse: %s", e)

    def _on_order_status(self, msg) -> None:
        try:
            oid = getattr(msg, "orderId", None) or msg.get("orderId")
            status = getattr(msg, "status", None) or msg.get("status")
            left = getattr(msg, "quantityLeft", None) or msg.get("quantityLeft") or 0
            if oid is not None:
                self.om.on_ws_order_update(int(oid), str(status), float(left or 0))
        except Exception as e:
            log.debug("order status parse: %s", e)

    def _on_balances(self, msg) -> None:
        try:
            cash = float(getattr(msg, "cash", None) or msg.get("cash") or 0)
            frozen = float(getattr(msg, "frozen", None) or msg.get("frozen") or 0)
            self.accounting.on_cash(cash, frozen)
        except Exception as e:
            log.debug("balance parse: %s", e)

    def start_ws(self) -> None:
        try:
            from loaf import LoafWebSocketClient
        except ImportError:
            log.warning("WS client not available — running REST-only (degraded)")
            return

        ws = self.client.websocket() if hasattr(self.client, "websocket") else None
        if ws is None:
            try:
                ws = LoafWebSocketClient(
                    api_key=self.config.api_key,
                    url=self.config.ws_url,
                )
            except Exception as e:
                log.warning("Could not construct WS client: %s", e)
                return

        self._ws = ws

        # Register handlers — SDK shapes vary slightly; be defensive
        if hasattr(ws, "on_orderbook"):
            ws.on_orderbook(self._on_orderbook)
        if hasattr(ws, "on_mark_price"):
            ws.on_mark_price(self._on_mark)
        if hasattr(ws, "on_trade"):  # private fills
            ws.on_trade(self._on_fill)
        if hasattr(ws, "on_order_status") or hasattr(ws, "on_order_update"):
            handler = getattr(ws, "on_order_status", None) or getattr(
                ws, "on_order_update", None
            )
            if handler:
                handler(self._on_order_status)
        if hasattr(ws, "on_balances"):
            ws.on_balances(self._on_balances)

        try:
            if hasattr(ws, "subscribe_orderbook"):
                ws.subscribe_orderbook(self.token)
            if hasattr(ws, "subscribe_markprice") or hasattr(ws, "subscribe_mark_price"):
                (getattr(ws, "subscribe_markprice", None) or ws.subscribe_mark_price)(
                    self.token
                )
            if self.config.user_id and hasattr(ws, "subscribe_portfolio"):
                ws.subscribe_portfolio(user_id=int(self.config.user_id))
        except Exception as e:
            log.warning("WS subscribe failed: %s", e)

        # Run WS in background
        def _run():
            try:
                if hasattr(ws, "run_forever"):
                    ws.run_forever()
                elif hasattr(ws, "start"):
                    ws.start()
                    while not self._stop.is_set():
                        time.sleep(1)
            except Exception as e:
                log.warning("WS thread exited: %s", e)

        t = threading.Thread(target=_run, name="loaf-ws", daemon=True)
        t.start()
        log.info("WS thread started")

    def seed_book(self) -> None:
        """One-shot REST seed so we have a book before first tick."""
        try:
            detail = self.client.market.property(self.token)
            ob = getattr(detail, "orderBook", None) or detail.get("orderBook") or {}
            bids = ob.get("bids") or []
            asks = ob.get("asks") or []
            bid = float(bids[0]["price"]) if bids else None
            ask = float(asks[0]["price"]) if asks else None
            self.strategy.on_book(bid, ask)
            mark = getattr(detail, "marketPrice", None) or detail.get("marketPrice")
            if mark:
                self.strategy.on_mark(float(mark))
            log.info("Seeded book bid=%s ask=%s mark=%s", bid, ask, mark)
        except Exception as e:
            log.warning("Seed book failed: %s", e)

    def run(self) -> None:
        log.info(
            "Starting PatientMaker token=%s offset=%d ticks size=%.1f "
            "refresh>=%.1fs max_dd=%.1f%%",
            self.token,
            self.config.inside_offset_ticks,
            self.config.base_size,
            self.config.min_refresh_interval_s,
            self.config.max_drawdown_pct,
        )
        self.seed_book()
        self.start_ws()

        def _sig(*_):
            log.info("Signal received — shutting down")
            self._stop.set()

        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)

        tick = 0
        while not self._stop.is_set():
            try:
                self.strategy.on_tick()
                tick += 1
                if tick % 6 == 0:  # ~ every 48s at 8s tick
                    self.accounting.log_status()
                    live = self.om.live_snapshot()
                    if live:
                        log.info("[om] live orders: %s", live)
            except Exception as e:
                log.exception("tick error: %s", e)
            self._stop.wait(self.config.tick_interval_s)

        log.info("Stopping — cancelling open orders")
        try:
            self.om.cancel_all()
        except Exception:
            pass
        self.accounting.log_status()
        log.info("Final snapshot: %s", self.accounting.snapshot())
        try:
            self.client.close()
        except Exception:
            pass


def main() -> None:
    setup_logging()
    config = BotConfig()
    try:
        config.validate()
    except ValueError as e:
        log.error("%s", e)
        sys.exit(1)

    runtime = BotRuntime(config)
    runtime.run()


if __name__ == "__main__":
    main()
