"""Public oracle showcase — hindsight-optimal trades from live candles.

Reads the local candle store (public market data only) and renders, for
each tracked coin: the best achievable buy->sell sequence over the last
24h and 7d ("oracle"), buy&hold for context, and a price chart with the
oracle entry/exit marked. Outputs README.md + charts/*.png.

This is a research visualization: oracle results use hindsight and are
NOT investable. No positions, keys or private model internals appear
here.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRAN = "ONE_HOUR"
COINS = ["BTC", "ETH", "XRP", "SOL", "ZEC", "ADA", "XLM", "LTC",
         "LINK", "NEAR", "DOGE", "ONDO", "SUI"]
WINDOWS = {"24h": 24, "7d": 168}


def load_closes(db: str, coin: str) -> list[tuple]:
    conn = sqlite3.connect(db)
    since = int((datetime.now(timezone.utc)
                 - timedelta(hours=WINDOWS["7d"] + 2)).timestamp())
    rows = conn.execute(
        "SELECT start_ts, low, high, close FROM candles "
        "WHERE pair=? AND granularity=? AND start_ts>=? "
        "ORDER BY start_ts", (f"{coin}-USDC", GRAN, since)).fetchall()
    conn.close()
    return rows


def oracle(rows: list[tuple], bars: int) -> dict:
    """Best hindsight buy->sell within the last `bars` hours."""
    w = rows[-bars:]
    if len(w) < 8:
        return {}
    best = None
    for i, (ts_i, lo, _h, _c) in enumerate(w[:-1]):
        for ts_j, _lo, hi, _c in w[i + 1:]:
            r = hi / lo - 1 if lo > 0 else 0
            if best is None or r > best["ret"]:
                best = {"ret": r,
                        "buy": datetime.fromtimestamp(ts_i, timezone.utc),
                        "sell": datetime.fromtimestamp(ts_j, timezone.utc),
                        "buy_px": lo, "sell_px": hi}
    bh = w[-1][3] / w[0][3] - 1 if w[0][3] > 0 else 0
    return {"ret": best["ret"], "buy": best["buy"], "sell": best["sell"],
            "buy_px": best["buy_px"], "sell_px": best["sell_px"],
            "bh": bh} if best else {}


def fmt_pct(x: float) -> str:
    return f"{x * 100:+.1f}%"


def chart(rows: list[tuple], coin: str, o7: dict, path: str) -> None:
    w = rows[-WINDOWS["7d"]:]
    ts = [datetime.fromtimestamp(r[0], timezone.utc) for r in w]
    close = [r[3] for r in w]
    fig, ax = plt.subplots(figsize=(10, 3.4), dpi=110)
    ax.plot(ts, close, lw=1.1, color="#2a6f97")
    ax.fill_between(ts, close, min(close), alpha=0.08, color="#2a6f97")
    if o7:
        ax.scatter([o7["buy"]], [o7["buy_px"]], marker="^", s=64,
                   color="#2f9e63", zorder=5, label="oracle buy")
        ax.scatter([o7["sell"]], [o7["sell_px"]], marker="v", s=64,
                   color="#c94f4f", zorder=5, label="oracle sell")
        ax.legend(loc="upper left", fontsize=8, frameon=False)
    ax.set_title(f"{coin}-USDC — last 7 days (hourly)", fontsize=10,
                 loc="left")
    ax.grid(alpha=0.25, lw=0.4)
    ax.tick_params(labelsize=8)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def main() -> None:
    db = sys.argv[1] if len(sys.argv) > 1 else "data/athena_merged.db"
    charts = os.path.join(ROOT, "charts")
    os.makedirs(charts, exist_ok=True)
    table = ["| Pair | 24h oracle | 7d oracle | 7d buy&hold | oracle window |",
             "|---|---|---|---|---|"]
    rendered = 0
    for coin in COINS:
        rows = load_closes(db, coin)
        if len(rows) < 8:
            continue
        o24 = oracle(rows, WINDOWS["24h"])
        o7 = oracle(rows, WINDOWS["7d"])
        if not o7:
            continue
        table.append(
            f"| {coin}-USDC | {fmt_pct(o24['ret'])} | {fmt_pct(o7['ret'])} "
            f"| {fmt_pct(o7['bh'])} "
            f"| {o7['buy']:%b %d %H:%M} → {o7['sell']:%b %d %H:%M} UTC |")
        chart(rows, coin, o7, os.path.join(charts, f"{coin}.png"))
        rendered += 1
    now = datetime.now(timezone.utc).strftime("%b %d %Y %H:%M UTC")
    readme = f"""# ATLAS Oracle Tracker

Live hindsight-**oracle** analytics for {len(COINS)} liquid USDC perpetuals,
refreshed automatically by GitHub Actions from public exchange candle
data.

The **oracle** for a window is the best trade that existed in hindsight:
buy at the lowest hourly low, sell at the highest hourly high after it.
Comparing today's oracle against buy&hold shows how much opportunity the
week actually contained, per coin — a benchmark for any systematic
strategy.

> These numbers are computed with hindsight and are **not investable**.
> Nothing here is financial advice.

**Last update:** {now} — {rendered}/{len(COINS)} coins rendered

{table[0]}
{table[1]}
""" + "\n".join(table[2:]) + "\n"

    readme += "\n## Charts (7-day window, oracle entry/exit marked)\n\n"
    for coin in COINS:
        if os.path.exists(os.path.join(charts, f"{coin}.png")):
            readme += f"![{coin}](charts/{coin}.png)\n\n"
    with open(os.path.join(ROOT, "README.md"), "w") as fh:
        fh.write(readme)
    print(f"rendered {rendered} coins -> README.md + charts/")


if __name__ == "__main__":
    main()
