#!/usr/bin/env python3
"""Paper-only Polymarket watcher.

This command uses public Gamma API data only. It does not read wallet
credentials, initialize a CLOB client, sign orders, or place trades.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_URL = "https://gamma-api.polymarket.com"
DATA_DIR = Path.home() / ".polymarket"
STATE_PATH = DATA_DIR / "paper_watch_state.json"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(errors="replace")

DEFAULT_CONFIG = {
    "paper_cash": 1000.0,
    "stake": 25.0,
    "max_open_positions": 18,
    "max_positions_per_slug": 1,
    "max_new_per_run": 1,
    "min_volume_24h": 100000.0,
    "min_liquidity": 5000.0,
    "max_spread": 0.015,
    "min_price": 0.18,
    "max_price": 0.82,
    "min_score": 0.82,
    "mature_after_hours": 24,
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def load_state() -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "created_at": iso(now()),
        "config": DEFAULT_CONFIG.copy(),
        "cash": DEFAULT_CONFIG["paper_cash"],
        "positions": [],
        "snapshots": [],
        "runs": [],
    }


def save_state(state: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def fetch(endpoint: str, params: dict[str, Any] | None = None) -> Any:
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    request = urllib.request.Request(
        f"{BASE_URL}{endpoint}{query}",
        headers={"User-Agent": "polymarket-paper-watch/1.0"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except Exception:
        return default


def parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def market_price(market: dict[str, Any]) -> float:
    prices = parse_json_list(market.get("outcomePrices"))
    if prices:
        return as_float(prices[0])
    return as_float(market.get("lastTradePrice") or market.get("price"))


def market_name(event: dict[str, Any], market: dict[str, Any]) -> str:
    return (
        market.get("groupItemTitle")
        or market.get("question")
        or event.get("title")
        or event.get("slug")
        or "unknown"
    )


def spread(market: dict[str, Any]) -> float | None:
    bid = as_float(market.get("bestBid"), -1)
    ask = as_float(market.get("bestAsk"), -1)
    if bid >= 0 and ask >= 0 and ask >= bid:
        return ask - bid
    return None


def score_candidate(event: dict[str, Any], market: dict[str, Any], config: dict[str, Any]) -> tuple[float, list[str]]:
    price = market_price(market)
    volume = as_float(event.get("volume24hr") or event.get("volume24h"))
    liquidity = as_float(market.get("liquidity") or event.get("liquidity"))
    spr = spread(market)
    one_day = as_float(market.get("oneDayPriceChange"))
    end = parse_dt(event.get("endDate") or market.get("endDate"))
    reasons: list[str] = []

    if price < config["min_price"] or price > config["max_price"]:
        return 0.0, ["price outside configured range"]
    if volume < config["min_volume_24h"]:
        return 0.0, ["low 24h volume"]
    if liquidity < config["min_liquidity"]:
        return 0.0, ["low liquidity"]
    if spr is not None and spr > config["max_spread"]:
        return 0.0, ["spread too wide"]
    if end and end < now():
        return 0.0, ["event already ended"]

    score = 0.0
    score += min(volume / 250000.0, 1.0) * 0.28
    score += min(liquidity / 100000.0, 1.0) * 0.24
    score += (max(0.0, 1.0 - (spr / config["max_spread"])) * 0.20) if spr is not None else 0.08
    score += max(0.0, 1.0 - abs(price - 0.5) / 0.32) * 0.18
    if one_day > 0:
        score += min(one_day / 0.08, 1.0) * 0.10

    reasons.append(f"volume24h=${volume:,.0f}")
    reasons.append(f"liquidity=${liquidity:,.0f}")
    reasons.append(f"price={price:.3f}")
    if spr is not None:
        reasons.append(f"spread={spr:.3f}")
    if one_day:
        reasons.append(f"one_day={one_day:+.3f}")

    return round(score, 4), reasons


def collect_candidates(config: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    events = fetch(
        "/events",
        {"closed": "false", "order": "volume24hr", "ascending": "false", "limit": 300},
    )
    candidates: list[dict[str, Any]] = []
    for event in events:
        for market in event.get("markets", []) or []:
            price = market_price(market)
            score, reasons = score_candidate(event, market, config)
            if score < config["min_score"]:
                continue
            candidates.append(
                {
                    "slug": event.get("slug"),
                    "title": event.get("title") or event.get("slug"),
                    "market": market_name(event, market),
                    "market_id": str(market.get("id") or market.get("conditionId") or market_name(event, market)),
                    "price": price,
                    "score": score,
                    "reason": ", ".join(reasons),
                    "end_date": event.get("endDate") or market.get("endDate"),
                }
            )
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:limit]


def current_price_for(position: dict[str, Any]) -> float | None:
    event_data = fetch("/events", {"slug": position["slug"]})
    if not event_data:
        return None
    event = event_data[0] if isinstance(event_data, list) else event_data
    for market in event.get("markets", []) or []:
        market_id = str(market.get("id") or market.get("conditionId") or market_name(event, market))
        if market_id == position.get("market_id") or market_name(event, market) == position.get("market"):
            return market_price(market)
    return None


def refresh_positions(state: dict[str, Any]) -> dict[str, int]:
    stats = {"updated": 0, "matured": 0, "wins": 0}
    mature_after = float(state["config"]["mature_after_hours"])
    for pos in state["positions"]:
        if pos.get("status") != "open":
            continue
        price = current_price_for(pos)
        if price is None:
            continue
        pos["current_price"] = price
        pos["last_checked_at"] = iso(now())
        pos["unrealized_pnl"] = round((price - pos["entry_price"]) * pos["shares"], 4)
        stats["updated"] += 1

        opened = parse_dt(pos.get("opened_at"))
        age_hours = (now() - opened).total_seconds() / 3600 if opened else 0
        if age_hours >= mature_after:
            pos["matured"] = True
            pos["hit"] = price > pos["entry_price"]
            stats["matured"] += 1
            if pos["hit"]:
                stats["wins"] += 1
    return stats


def open_new_positions(state: dict[str, Any], candidates: list[dict[str, Any]], dry_run: bool) -> list[dict[str, Any]]:
    config = state["config"]
    open_positions = [p for p in state["positions"] if p.get("status") == "open"]
    existing_keys = {(p.get("slug"), p.get("market_id")) for p in open_positions}
    slug_counts: dict[str, int] = {}
    for pos in open_positions:
        slug = pos.get("slug")
        if slug:
            slug_counts[slug] = slug_counts.get(slug, 0) + 1

    max_per_slug = int(config.get("max_positions_per_slug", 1))
    remaining_slots = max(0, int(config["max_open_positions"]) - len(open_positions))
    max_new = min(int(config["max_new_per_run"]), remaining_slots)
    opened: list[dict[str, Any]] = []

    for candidate in candidates:
        if len(opened) >= max_new:
            break
        key = (candidate["slug"], candidate["market_id"])
        if key in existing_keys:
            continue
        slug = candidate.get("slug")
        if slug and slug_counts.get(slug, 0) >= max_per_slug:
            continue
        stake = float(config["stake"])
        if state["cash"] < stake:
            break
        shares = stake / candidate["price"]
        position = {
            **candidate,
            "status": "open",
            "opened_at": iso(now()),
            "entry_price": candidate["price"],
            "current_price": candidate["price"],
            "stake": stake,
            "shares": shares,
            "matured": False,
            "hit": None,
        }
        opened.append(position)
        if not dry_run:
            state["positions"].append(position)
            state["cash"] = round(state["cash"] - stake, 2)
            existing_keys.add(key)
            if slug:
                slug_counts[slug] = slug_counts.get(slug, 0) + 1
    return opened


def summarize(state: dict[str, Any]) -> dict[str, Any]:
    positions = state["positions"]
    mature = [p for p in positions if p.get("matured")]
    wins = [p for p in mature if p.get("hit")]
    open_positions = [p for p in positions if p.get("status") == "open"]
    total_pnl = round(sum(as_float(p.get("unrealized_pnl")) for p in open_positions), 2)
    return {
        "open": len(open_positions),
        "mature": len(mature),
        "wins": len(wins),
        "hit_rate": round((len(wins) / len(mature)) * 100, 1) if mature else None,
        "cash": state["cash"],
        "open_unrealized_pnl": total_pnl,
    }


def cmd_run(args: argparse.Namespace) -> None:
    state = load_state()
    state["config"] = {**DEFAULT_CONFIG, **state.get("config", {})}
    refresh = refresh_positions(state)
    candidates = collect_candidates(state["config"], args.candidates)
    opened = open_new_positions(state, candidates, args.dry_run)
    summary = summarize(state)
    run = {
        "at": iso(now()),
        "dry_run": args.dry_run,
        "candidates": candidates[:10],
        "opened": opened,
        "refresh": refresh,
        "summary": summary,
    }
    if not args.dry_run:
        state["snapshots"].append({"at": run["at"], "summary": summary})
        state["runs"].append(run)
        save_state(state)

    print_scorecard("POLYMARKET PAPER WATCH", summary)
    print("")
    print("New paper calls:")
    if not opened:
        print("- none")
    for pos in opened:
        print(f"- {pos['title']} | {pos['market']} @ {pos['entry_price']:.3f} score={pos['score']:.3f}")
        print(f"  {pos['reason']}")


def print_scorecard(title: str, summary: dict[str, Any]) -> None:
    print(title)
    print(f"Open positions: {summary['open']}")
    print(f"Mature calls: {summary['mature']}")
    print(f"Hits: {summary['wins']}")
    print(f"Hit rate: {summary['hit_rate'] if summary['hit_rate'] is not None else 'n/a'}%")
    print(f"Paper cash: ${summary['cash']:,.2f}")
    print(f"Open unrealized PnL: ${summary['open_unrealized_pnl']:,.2f}")


def cmd_report(args: argparse.Namespace) -> None:
    state = load_state()
    state["config"] = {**DEFAULT_CONFIG, **state.get("config", {})}
    refresh_positions(state)
    if args.save:
        save_state(state)
    summary = summarize(state)
    print_scorecard("POLYMARKET PAPER WATCH REPORT", summary)
    print("")
    for pos in state["positions"][-10:]:
        pnl = as_float(pos.get("unrealized_pnl"))
        mature = "mature" if pos.get("matured") else "young"
        hit = "hit" if pos.get("hit") else "miss" if pos.get("hit") is False else "pending"
        print(
            f"- {pos['title']} | {pos['market']} {mature}/{hit} "
            f"entry={pos['entry_price']:.3f} now={pos.get('current_price', 0):.3f} pnl=${pnl:.2f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper-only Polymarket watcher")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Refresh positions and open new paper calls")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--candidates", type=int, default=30)
    run.set_defaults(func=cmd_run)

    report = sub.add_parser("report", help="Refresh and print current scorecard")
    report.add_argument("--save", action="store_true")
    report.set_defaults(func=cmd_report)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
