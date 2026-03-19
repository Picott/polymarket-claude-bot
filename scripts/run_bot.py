"""
Polymarket trading bot — scans markets and evaluates with Claude.

Usage:
  python scripts/run_bot.py --mode paper --once     # run once
  python scripts/run_bot.py --mode paper            # loop every 5 min
  python scripts/run_bot.py --mode paper --interval 60
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
load_dotenv(dotenv_path=ROOT / "config" / ".env")

# ── Config ───────────────────────────────────────────────────────
TRADES_LOG    = ROOT / "logs" / "trades.jsonl"
HEARTBEAT     = ROOT / "logs" / ".heartbeat"
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
TRADE_MODE    = os.getenv("TRADE_MODE", "paper")
MAX_POS_USDC  = float(os.getenv("MAX_POSITION_USDC", 10))
MIN_EDGE      = float(os.getenv("MIN_EDGE", 0.10))
MIN_CONF      = float(os.getenv("MIN_CONFIDENCE", 0.80))
MAX_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", 5))
DAILY_LIMIT   = float(os.getenv("DAILY_LOSS_LIMIT_USDC", 50))

GAMMA_API = "https://gamma-api.polymarket.com"
HAIKU     = "claude-haiku-4-5-20251001"
SONNET    = "claude-sonnet-4-6"

client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


# ── Helpers ──────────────────────────────────────────────────────

def log_trade(record: dict):
    TRADES_LOG.parent.mkdir(exist_ok=True)
    with open(TRADES_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")


def update_heartbeat():
    HEARTBEAT.write_text(datetime.utcnow().isoformat())


def load_today_trades() -> list:
    if not TRADES_LOG.exists():
        return []
    today = datetime.utcnow().strftime("%Y-%m-%d")
    trades = []
    with open(TRADES_LOG) as f:
        for line in f:
            try:
                t = json.loads(line)
                if t.get("timestamp", "").startswith(today):
                    trades.append(t)
            except json.JSONDecodeError:
                continue
    return trades


# ── Market fetching ──────────────────────────────────────────────

def fetch_markets(limit: int = 30) -> list:
    try:
        resp = requests.get(
            f"{GAMMA_API}/markets",
            params={"limit": limit, "active": "true", "closed": "false"},
            timeout=15,
        )
        resp.raise_for_status()
        binary = []
        for m in resp.json():
            prices   = m.get("outcomePrices")
            outcomes = m.get("outcomes")
            if not prices or not outcomes:
                continue
            try:
                prices   = json.loads(prices)   if isinstance(prices, str)   else prices
                outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
            except Exception:
                continue
            if len(prices) == 2 and len(outcomes) == 2:
                binary.append(m)
        return binary
    except Exception as e:
        print(f"  [!] Error fetching markets: {e}")
        return []


# ── Claude evaluation ────────────────────────────────────────────

SYSTEM_PROMPT = """You are a sharp prediction market analyst.
Evaluate whether a Polymarket binary market is mispriced.
Reply ONLY with valid JSON — no markdown, no extra text.

Required format:
{
  "action": "YES" | "NO" | "SKIP",
  "estimated_prob": <float 0-1>,
  "confidence": <float 0-1>,
  "reasoning": "<one sentence>"
}

- action: which outcome to bet on, or SKIP if no clear edge
- estimated_prob: your best estimate of the YES probability
- confidence: how confident you are in that estimate
- reasoning: one concise sentence justifying your call"""


def evaluate_market(question: str, market_prob: float, model: str) -> dict | None:
    prompt = (
        f"Market: {question}\n"
        f"Current market probability (YES): {market_prob:.1%}\n\n"
        "Is this market mispriced? Should I bet YES, NO, or SKIP?"
    )
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=256,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return json.loads(msg.content[0].text)
    except Exception as e:
        print(f"    [!] Claude error ({model}): {e}")
        return None


# ── Sizing ───────────────────────────────────────────────────────

def calc_size(edge: float, confidence: float) -> float:
    """Fractional Kelly sizing capped at MAX_POS_USDC."""
    size = MAX_POS_USDC * edge * confidence
    return round(min(size, MAX_POS_USDC), 2)


# ── Core scan ────────────────────────────────────────────────────

def run_once(mode: str):
    ts_start = datetime.utcnow().strftime("%H:%M:%S")
    print(f"\n  [{ts_start}] Scanning markets…  mode={mode.upper()}")
    update_heartbeat()

    today_trades    = load_today_trades()
    today_executed  = [t for t in today_trades if t.get("executed")]
    daily_deployed  = sum(t.get("size_usdc", 0) for t in today_executed)
    open_positions  = len(today_executed)

    print(f"  Today so far: {open_positions} trades · ${daily_deployed:.2f} deployed\n")

    markets = fetch_markets(limit=30)
    print(f"  Fetched {len(markets)} binary markets\n")

    for m in markets:
        question  = m.get("question", "")
        market_id = m.get("conditionId") or m.get("id", "")
        prices    = m.get("outcomePrices", [])

        try:
            prices      = json.loads(prices) if isinstance(prices, str) else prices
            market_prob = float(prices[0])   # YES price
        except Exception:
            continue

        print(f"  ▸ {question[:70]}")
        print(f"    Market YES: {market_prob:.1%}")

        # ── Quick screen with Haiku ──────────────────────────────
        haiku_eval = evaluate_market(question, market_prob, HAIKU)

        if not haiku_eval or haiku_eval.get("action") == "SKIP":
            reason = "Claude SKIP" if haiku_eval else "API error"
            ep     = haiku_eval.get("estimated_prob", market_prob) if haiku_eval else market_prob
            log_trade({
                "timestamp": datetime.utcnow().isoformat(),
                "mode": mode, "executed": False,
                "question": question, "market_id": market_id,
                "bet": haiku_eval.get("action", "SKIP") if haiku_eval else "SKIP",
                "confidence": haiku_eval.get("confidence", 0) if haiku_eval else 0,
                "estimated_prob": ep, "market_prob": market_prob,
                "edge": abs(ep - market_prob),
                "size_usdc": 0,
                "reasoning": haiku_eval.get("reasoning", "") if haiku_eval else "",
                "model": HAIKU, "skip_reason": reason, "result": None,
            })
            print(f"    → SKIP ({reason})\n")
            continue

        est_prob = haiku_eval.get("estimated_prob", market_prob)
        edge     = abs(est_prob - market_prob)
        conf     = haiku_eval.get("confidence", 0)
        action   = haiku_eval.get("action")
        reasoning = haiku_eval.get("reasoning", "")

        print(f"    Haiku: {action} | est={est_prob:.1%} | edge={edge:.1%} | conf={conf:.0%}")

        # ── Threshold checks ─────────────────────────────────────
        skip_reason = ""
        if edge < MIN_EDGE:
            skip_reason = f"Insufficient edge ({edge:.1%} < {MIN_EDGE:.0%})"
        elif conf < MIN_CONF:
            skip_reason = f"Low confidence ({conf:.0%} < {MIN_CONF:.0%})"
        elif open_positions >= MAX_POSITIONS:
            skip_reason = f"Max open positions reached ({MAX_POSITIONS})"
        elif daily_deployed >= DAILY_LIMIT:
            skip_reason = f"Daily loss limit reached (${daily_deployed:.2f})"

        if skip_reason:
            log_trade({
                "timestamp": datetime.utcnow().isoformat(),
                "mode": mode, "executed": False,
                "question": question, "market_id": market_id,
                "bet": action, "confidence": conf,
                "estimated_prob": est_prob, "market_prob": market_prob,
                "edge": edge, "size_usdc": 0, "reasoning": reasoning,
                "model": HAIKU, "skip_reason": skip_reason, "result": None,
            })
            print(f"    → SKIP: {skip_reason}\n")
            continue

        # ── Confirm with Sonnet ───────────────────────────────────
        print(f"    Escalating to Sonnet…")
        sonnet_eval = evaluate_market(question, market_prob, SONNET)

        final_model = SONNET
        if sonnet_eval and sonnet_eval.get("action") == "SKIP":
            log_trade({
                "timestamp": datetime.utcnow().isoformat(),
                "mode": mode, "executed": False,
                "question": question, "market_id": market_id,
                "bet": action, "confidence": conf,
                "estimated_prob": est_prob, "market_prob": market_prob,
                "edge": edge, "size_usdc": 0,
                "reasoning": sonnet_eval.get("reasoning", reasoning),
                "model": SONNET, "skip_reason": "Sonnet downgrade: SKIP", "result": None,
            })
            print(f"    → SKIP: Sonnet downgraded\n")
            continue
        elif sonnet_eval:
            est_prob  = sonnet_eval.get("estimated_prob", est_prob)
            edge      = abs(est_prob - market_prob)
            conf      = sonnet_eval.get("confidence", conf)
            action    = sonnet_eval.get("action", action)
            reasoning = sonnet_eval.get("reasoning", reasoning)
        else:
            final_model = HAIKU  # Sonnet failed, fall back to Haiku decision

        size = calc_size(edge, conf)
        print(f"    → EXECUTE {action} | size=${size} | edge={edge:.1%} | conf={conf:.0%}")

        log_trade({
            "timestamp": datetime.utcnow().isoformat(),
            "mode": mode, "executed": True,
            "question": question, "market_id": market_id,
            "bet": action, "confidence": conf,
            "estimated_prob": est_prob, "market_prob": market_prob,
            "edge": edge, "size_usdc": size, "reasoning": reasoning,
            "model": final_model, "skip_reason": "", "result": None,
        })

        open_positions += 1
        daily_deployed += size
        update_heartbeat()
        time.sleep(1)  # polite rate limiting
        print()

    print(f"  Done. {open_positions} positions · ${daily_deployed:.2f} deployed today\n")


# ── Entry point ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Polymarket Claude Bot")
    parser.add_argument("--mode",     choices=["paper", "live"], default=TRADE_MODE)
    parser.add_argument("--once",     action="store_true", help="Run once and exit")
    parser.add_argument("--interval", type=int, default=300,
                        help="Seconds between scans in loop mode (default: 300)")
    args = parser.parse_args()

    print(f"\n  polymarket-claude-bot  |  mode={args.mode.upper()}")
    if args.mode == "live":
        print("  ⚠  LIVE mode — real funds will be used!")
        confirm = input("  Type 'yes' to confirm: ").strip().lower()
        if confirm != "yes":
            print("  Aborted.")
            sys.exit(0)

    if args.once:
        run_once(args.mode)
    else:
        while True:
            run_once(args.mode)
            print(f"  Sleeping {args.interval}s…")
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
