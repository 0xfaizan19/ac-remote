"""Read-only check: log in to the AC and print its current settings (changes nothing).
Usage: .venv/bin/python check_ac.py [ip]"""
import asyncio
import json
import sys
import time

from ac_device import GreeAC


async def main():
    ac = GreeAC(ip=sys.argv[1] if len(sys.argv) > 1 else None)
    t = time.time()
    try:
        await ac.connect()
    except Exception as e:  # noqa: BLE001
        print(f"FAILED to log in to the AC: {type(e).__name__}: {e}")
        return
    print(f"Logged in to the AC in {time.time() - t:.1f}s "
          f"({type(ac.dev.device_cipher).__name__}) at {ac.info.get('ip')}")
    print(json.dumps(ac.snapshot(), indent=1))
    print("raw:", ac.dev.raw_properties)
    await ac.close()


asyncio.run(main())
