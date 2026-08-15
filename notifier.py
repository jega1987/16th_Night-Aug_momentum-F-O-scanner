"""
Telegram alerts. Entirely optional - with no bot token configured the notifier
logs and returns, so nothing else has to care whether it's set up.
"""
import logging
from typing import Dict

import httpx

from config import cfg

logger = logging.getLogger(__name__)

ARROW = {"LONG": "\u25b2", "SHORT": "\u25bc"}


class Notifier:
    def __init__(self):
        self.enabled = bool(cfg.TELEGRAM_BOT_TOKEN and cfg.TELEGRAM_CHAT_ID)
        self.url = f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendMessage"
        if not self.enabled:
            logger.info("[Notifier] Telegram not configured - alerts stay in the log")

    async def send(self, text: str) -> bool:
        if not self.enabled:
            logger.info("[Notifier] %s", text.replace("\n", " | "))
            return False
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self.url, json={
                    "chat_id": cfg.TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                })
                resp.raise_for_status()
            return True
        except Exception as exc:
            logger.error("[Notifier] Telegram send failed: %s", exc)
            return False

    async def send_signal(self, s: Dict) -> bool:
        arrow = ARROW.get(s["direction"], "")
        text = (
            f"<b>{arrow} {s['symbol']} {s['direction']}</b>  ({s['timeframe']})\n"
            f"Entry <b>{s['entry']}</b>   SL <b>{s['sl']}</b>\n"
            f"T1 {s['tp1']}  T2 {s['tp2']}  T3 {s['tp3']}\n"
            f"Qty {s['qty']} ({s['lots']} lots)   ATR {s['atr14']}\n"
            f"{self._option_line(s)}"
            f"Score {s['composite_score']:.2f}\n"
            f"<i>Paper signal - no order was placed.</i>"
        )
        return await self.send(text)

    @staticmethod
    def _option_line(s: Dict) -> str:
        plan = s.get("option_plan")
        if not plan or not plan.get("strike"):
            return f"Option idea: {s.get('option_hint', '-')}\n"
        if plan.get("blocked"):
            return (f"Option leg blocked: {plan['block_reason']}\n"
                    f"<i>Futures-level signal only.</i>\n")
        bits = [f"Option: <b>{plan['label']}</b> @ {plan.get('ltp')}"]
        if plan.get("iv") is not None:
            rank = f", rank {plan['iv_rank']:.0f}" if plan.get("iv_rank") is not None else ", rank pending"
            bits.append(f"IV {plan['iv'] * 100:.1f}%{rank}")
        if plan.get("theta_drag_pct") is not None:
            bits.append(f"theta drag {plan['theta_drag_pct']:.0f}% of the move to T1")
        if plan.get("dte") is not None:
            bits.append(f"{plan['dte']} DTE")
        return "\n".join(bits) + "\n"

    async def send_exit(self, s: Dict) -> bool:
        pnl = s.get("pnl") or 0
        mark = "\u2705" if pnl > 0 else "\u274c"
        text = (
            f"{mark} <b>{s['symbol']} {s['status']}</b>\n"
            f"Entry {s['entry']} -> Exit {s.get('exit_price')}\n"
            f"P&L <b>{pnl:,.0f}</b>   R {s.get('r_multiple')}\n"
            f"{s.get('notes') or ''}"
        )
        return await self.send(text)

    async def send_error(self, where: str, detail: str) -> bool:
        return await self.send(f"\u26a0\ufe0f <b>Scanner problem</b>\n{where}\n<code>{detail[:400]}</code>")
