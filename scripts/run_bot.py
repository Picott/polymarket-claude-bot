"""
Polymarket trading bot — scans markets and evaluates with Claude.

Usage:
  python scripts/run_bot.py --mode paper --once     # run once
  python scripts/run_bot.py --mode paper            # loop every 5 min
  python scripts/run_bot.py --mode paper --interval 60
"""

import argparse
import json
import math
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
MIN_EV        = float(os.getenv("MIN_EV", 0.05))       # min EV per dollar (5¢)
MIN_CONF      = float(os.getenv("MIN_CONFIDENCE", 0.65))
MAX_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", 10))
DAILY_LIMIT   = float(os.getenv("DAILY_LOSS_LIMIT_USDC", 50))
BANKROLL      = float(os.getenv("BANKROLL_USDC", 1000))
KELLY_FRAC    = float(os.getenv("KELLY_FRACTION", 0.25))  # quarter Kelly

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


# ── Trade resolution ─────────────────────────────────────────────

def resolve_pending_trades():
    """Check resolved markets and update result/pnl on executed trades."""
    if not TRADES_LOG.exists():
        return
    lines = TRADES_LOG.read_text().splitlines()
    new_lines = []
    updated = False

    for line in lines:
        if not line.strip():
            new_lines.append(line)
            continue
        try:
            trade = json.loads(line)
        except json.JSONDecodeError:
            new_lines.append(line)
            continue

        if trade.get("executed") and trade.get("result") is None:
            resolution = _check_market_resolution(trade)
            if resolution:
                trade["result"] = resolution["outcome"]
                trade["pnl_usdc"] = resolution["pnl"]
                trade["log_return"] = resolution.get("log_return", 0)
                updated = True
                print(f"  [✓] Resolved: {trade['question'][:55]} → {resolution['outcome']} (${resolution['pnl']:+.2f})")

        new_lines.append(json.dumps(trade))

    if updated:
        TRADES_LOG.write_text("\n".join(new_lines) + "\n")
        print()


def _check_market_resolution(trade: dict) -> dict | None:
    """Query Gamma API for a market. Returns {outcome, pnl} if resolved, else None."""
    market_id = trade.get("market_id")
    if not market_id:
        return None
    try:
        resp = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=10)
        if resp.status_code != 200:
            return None
        m = resp.json()

        if m.get("active", True) and not m.get("closed"):
            return None  # still open

        prices = m.get("outcomePrices", [])
        if isinstance(prices, str):
            prices = json.loads(prices)
        if not prices or len(prices) < 2:
            return None

        yes_price = float(prices[0])
        if yes_price >= 0.99:
            winner = "YES"
        elif yes_price <= 0.01:
            winner = "NO"
        else:
            return None  # not fully settled yet

        bet        = trade.get("bet")
        size       = trade.get("size_usdc", 0)
        entry_prob = trade.get("market_prob", 0.5)

        if bet == winner:
            entry_price = entry_prob if bet == "YES" else (1 - entry_prob)
            pnl = round(size * (1 / entry_price - 1), 2) if entry_price > 0 else 0
            # Log return: ln(final_value / initial_value) — correct for compounding
            log_ret = round(math.log(1 + pnl / size), 4) if size > 0 else 0
            return {"outcome": "WIN", "pnl": pnl, "log_return": log_ret}
        else:
            # Full loss: log return = ln(0) is -inf, so we use a floor of -4.6 (≈ 99% loss)
            log_ret = round(math.log(0.01), 4)  # -4.6052
            return {"outcome": "LOSS", "pnl": round(-size, 2), "log_return": log_ret}

    except Exception:
        return None


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
- confidence: your calibrated certainty in your estimate (use the full range):
    0.50 = total guess, 0.65 = educated guess, 0.75 = reasonably sure,
    0.85 = strong evidence, 0.95 = near certain
  Be honest — use high confidence only when you have strong factual basis.
- reasoning: one concise sentence justifying your call"""


def _parse_json_response(text: str) -> dict:
    """Strip markdown fences and parse JSON from Claude's response."""
    text = text.strip()
    # Remove ```json ... ``` or ``` ... ``` wrappers
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return json.loads(text.strip())


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
        raw = msg.content[0].text if msg.content else ""
        if not raw.strip():
            print(f"    [!] Claude empty response ({model}) | stop_reason={msg.stop_reason}")
            return None
        return _parse_json_response(raw)
    except json.JSONDecodeError as e:
        print(f"    [!] Claude JSON parse error ({model}): {e}")
        return None
    except Exception as e:
        print(f"    [!] Claude error ({model}): {e}")
        return None


# ── Math: EV + Kelly ─────────────────────────────────────────────

def calc_ev(true_prob: float, market_price: float) -> float:
    """Expected value per dollar risked.
    EV = p * (1 - price) - (1 - p) * price
    Positive = edge in our favour."""
    return true_prob * (1 - market_price) - (1 - true_prob) * market_price


def calc_kelly(true_prob: float, market_price: float) -> float:
    """Quarter-Kelly fraction of bankroll, capped to MAX_POS_USDC.
    b = payout ratio = (1 - price) / price
    f* = (p*b - q) / b  →  apply KELLY_FRAC for safety."""
    if market_price <= 0 or market_price >= 1:
        return 0.0
    b = (1 - market_price) / market_price
    q = 1 - true_prob
    f_full = (true_prob * b - q) / b
    f_safe = max(f_full, 0.0) * KELLY_FRAC
    return round(min(BANKROLL * f_safe, MAX_POS_USDC), 2)


# ── Core scan ────────────────────────────────────────────────────

def run_once(mode: str):
    ts_start = datetime.utcnow().strftime("%H:%M:%S")
    print(f"\n  [{ts_start}] Scanning markets…  mode={mode.upper()}")
    update_heartbeat()

    resolve_pending_trades()

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
            ep_skip = haiku_eval.get("estimated_prob", market_prob) if haiku_eval else market_prob
            log_trade({
                "timestamp": datetime.utcnow().isoformat(),
                "mode": mode, "executed": False,
                "question": question, "market_id": market_id,
                "bet": haiku_eval.get("action", "SKIP") if haiku_eval else "SKIP",
                "confidence": haiku_eval.get("confidence", 0) if haiku_eval else 0,
                "estimated_prob": ep_skip, "market_prob": market_prob,
                "ev_per_dollar": round(calc_ev(ep_skip, market_prob), 4),
                "kelly_fraction": 0,
                "size_usdc": 0,
                "reasoning": haiku_eval.get("reasoning", "") if haiku_eval else "",
                "model": HAIKU, "skip_reason": reason, "result": None,
            })
            print(f"    → SKIP ({reason})\n")
            continue

        est_prob  = haiku_eval.get("estimated_prob", market_prob)
        ev        = calc_ev(est_prob, market_prob)
        conf      = haiku_eval.get("confidence", 0)
        action    = haiku_eval.get("action")
        reasoning = haiku_eval.get("reasoning", "")

        print(f"    Haiku: {action} | est={est_prob:.1%} | EV={ev:+.2f} | conf={conf:.0%}")

        # ── Threshold checks ─────────────────────────────────────
        skip_reason = ""
        if ev < MIN_EV:
            skip_reason = f"Insufficient EV ({ev:.2f} < {MIN_EV:.2f})"
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
                "ev_per_dollar": round(ev, 4), "kelly_fraction": 0,
                "size_usdc": 0, "reasoning": reasoning,
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
                "ev_per_dollar": round(ev, 4), "kelly_fraction": 0,
                "size_usdc": 0,
                "reasoning": sonnet_eval.get("reasoning", reasoning),
                "model": SONNET, "skip_reason": "Sonnet downgrade: SKIP", "result": None,
            })
            print(f"    → SKIP: Sonnet downgraded\n")
            continue
        elif sonnet_eval:
            est_prob  = sonnet_eval.get("estimated_prob", est_prob)
            ev        = calc_ev(est_prob, market_prob)
            conf      = sonnet_eval.get("confidence", conf)
            action    = sonnet_eval.get("action", action)
            reasoning = sonnet_eval.get("reasoning", reasoning)
        else:
            final_model = HAIKU  # Sonnet failed, fall back to Haiku decision

        size         = calc_kelly(est_prob, market_prob)
        kelly_f      = round(size / BANKROLL, 4) if BANKROLL > 0 else 0
        print(f"    → EXECUTE {action} | size=${size} | EV={ev:+.2f} | Kelly={kelly_f:.1%} | conf={conf:.0%}")

        log_trade({
            "timestamp": datetime.utcnow().isoformat(),
            "mode": mode, "executed": True,
            "question": question, "market_id": market_id,
            "bet": action, "confidence": conf,
            "estimated_prob": est_prob, "market_prob": market_prob,
            "ev_per_dollar": round(ev, 4), "kelly_fraction": kelly_f,
            "size_usdc": size, "reasoning": reasoning,
            "model": final_model, "skip_reason": "", "result": None,
        })

        open_positions += 1
        daily_deployed += size
        update_heartbeat()
        time.sleep(1)  # polite rate limiting
        print()

    print(f"  Done. {open_positions} positions · ${daily_deployed:.2f} deployed today\n")
    _push_trades()


VERCEL_DEPLOY_HOOK = "https://api.vercel.com/v1/integrations/deploy/prj_b9wSsRHEMI91cYOtpAbhLeLRhfqd/rTBmFs8rLw"


def _push_trades():
    """Auto-commit and push trades.jsonl so Vercel dashboard updates."""
    import subprocess, urllib.request
    try:
        base = str(ROOT)
        subprocess.run(["git", "add", "logs/trades.jsonl"], cwd=base, check=True, capture_output=True)
        result = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=base, capture_output=True)
        if result.returncode == 0:
            _trigger_vercel_deploy()
            return  # nothing new to commit, but still trigger redeploy
        subprocess.run(
            ["git", "commit", "-m", f"auto: update trades {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}"],
            cwd=base, check=True, capture_output=True
        )
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=base, check=True, capture_output=True, text=True
        ).stdout.strip()
        subprocess.run(["git", "push", "origin", branch], cwd=base, check=True, capture_output=True)
        _trigger_vercel_deploy()
        print("  [✓] Dashboard updated on Vercel\n")
    except Exception as e:
        print(f"  [!] Auto-push failed (run manually): {e}\n")


def _trigger_vercel_deploy():
    """Call Vercel deploy hook to trigger a redeploy."""
    import urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(VERCEL_DEPLOY_HOOK, method="POST"), timeout=10)
        print("  [✓] Vercel redeploy triggered\n")
    except Exception as e:
        print(f"  [!] Vercel deploy hook failed: {e}\n")


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
