"""Check which chains the Bitnob key can reach."""

import asyncio

from app.adapters import bitnob


async def main() -> None:
    for chain in ["ethereum", "base", "bitcoin", "tron"]:
        try:
            if chain == "bitcoin":
                result = await bitnob.rpc(chain, "getblockchaininfo")
                detail = f"blocks: {result.get('blocks'):,}"
            else:
                result = await bitnob.rpc(chain, "eth_blockNumber")
                detail = f"block: {int(result, 16):,}"
            print(f"  {chain:10} OK    {detail}")
        except Exception as err:
            print(f"  {chain:10} FAIL  {err}")

    print("\nPrices:")
    try:
        prices = await bitnob.get_prices()
        print(f"  {prices or 'empty — response shape needs checking'}")
    except Exception as err:
        print(f"  FAIL  {err}")


if __name__ == "__main__":
    asyncio.run(main())