"""Print a compact strategy scoreboard from the trade intelligence table."""

import argparse
import asyncio

from src.intelligence import TradeIntelligenceStore


async def main(db_path: str) -> None:
    store = TradeIntelligenceStore(db_path)
    rows = await store.strategy_summary()
    if not rows:
        print("No trade-intelligence records yet.")
        return

    print("STRATEGY SCOREBOARD")
    print("=" * 88)
    print(f"{'Strategy':30} {'Decisions':>9} {'Executed':>9} {'W/L':>9} {'PnL':>10} {'Conf':>8} {'Edge':>8}")
    for row in rows:
        wins = int(row['wins'] or 0)
        losses = int(row['losses'] or 0)
        conf = row['avg_confidence']
        edge = row['avg_edge']
        print(
            f"{row['strategy'][:30]:30} {int(row['decisions']):9d} {int(row['executed'] or 0):9d} "
            f"{wins}/{losses: <5} {float(row['total_pnl'] or 0):10.2f} "
            f"{('-' if conf is None else f'{conf:.2%}'):>8} "
            f"{('-' if edge is None else f'{edge:.2%}'):>8}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="trading_system.db")
    args = parser.parse_args()
    asyncio.run(main(args.db))
