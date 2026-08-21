"""
scripts/backfill_evm_fees.py

Fill in gas fees for EVM transactions recorded before fees were collected.

Bitcoin fees were always stored, so this only concerns Ethereum and Base, where
the column was written as null. Each fee needs the transaction receipt, so the
work is one RPC call per *transaction* — not per row, since a swap produces two
rows that share one fee and therefore one receipt.

Safe to stop and re-run: it only touches rows that are still null, so a second
run picks up wherever the first left off.

Usage:
    venv/Scripts/python scripts/backfill_evm_fees.py --dry-run
    venv/Scripts/python scripts/backfill_evm_fees.py --limit 200
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402
from app.adapters import evm  # noqa: E402
from app.core.http import map_limit  # noqa: E402

# Matches the poller's own concurrency. The point of the backfill is to catch
# up quietly, not to outpace the thing it is catching up with.
CONCURRENCY = 8


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=500,
                    help="how many distinct transactions to process (default 500)")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and report, write nothing")
    args = ap.parse_args()

    pending = await db.fetch(
        """
        select distinct t.tx_hash, t.chain
        from transactions t
        where t.fee is null
          and t.chain in ('ethereum', 'base')
        order by t.chain, t.tx_hash
        limit $1
        """,
        args.limit,
    )

    if not pending:
        print("Nothing to backfill: no EVM transactions are missing a fee.")
        return

    rows = await db.fetchrow(
        """
        select count(*) as n
        from transactions
        where fee is null and chain in ('ethereum', 'base')
        """
    )
    print(f"{rows['n']} rows missing a fee, across {len(pending)} transactions this run")
    print("(one receipt per transaction, not per row)\n")

    async def fetch(item) -> tuple[str, str, str | None]:
        fee = await evm.get_transaction_fee(item["chain"], item["tx_hash"])
        return item["tx_hash"], item["chain"], fee

    results = await map_limit(list(pending), CONCURRENCY, fetch)

    found = [r for r in results if r[2] is not None]
    missing = len(results) - len(found)

    for tx_hash, chain, fee in found[:10]:
        print(f"  {chain:<9} {tx_hash[:20]}... {fee} ETH")
    if len(found) > 10:
        print(f"  ... and {len(found) - 10} more")

    if args.dry_run:
        print(f"\nDRY RUN: would update {len(found)} transactions "
              f"({missing} had no retrievable receipt). Nothing written.")
        return

    updated = 0
    for tx_hash, chain, fee in found:
        result = await db.execute(
            """
            update transactions set fee = $1
            where tx_hash = $2 and chain = $3 and fee is null
            """,
            fee, tx_hash, chain,
        )
        # "UPDATE n" — count the rows, since one transaction can back several.
        updated += int(result.rsplit(" ", 1)[-1] or 0)

    print(f"\nUpdated {updated} rows from {len(found)} transactions.")
    if missing:
        print(f"{missing} transactions returned no receipt and were left null — "
              f"re-running will try them again.")

    left = await db.fetchrow(
        """
        select count(*) as n
        from transactions
        where fee is null and chain in ('ethereum', 'base')
        """
    )
    print(f"{left['n']} rows still missing a fee.")

    pool = await db.get_pool()
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
