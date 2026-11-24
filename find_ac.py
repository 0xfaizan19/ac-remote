"""Run this on the Mac Mini to check it can see the AC: .venv/bin/python find_ac.py"""
import asyncio
from ac_device import GreeAC
async def main():
    found = await GreeAC().discover(wait=8)
    if not found:
        print("No AC found. Check: AC is set up in the Tosot Plus app on the SAME Wi-Fi as this Mac, "
              "and System Settings > Privacy & Security > Local Network allows Terminal/python.")
    for d in found:
        print(f"Found: name={d.name} ip={d.ip} mac={d.mac} brand={d.brand} model={d.model} firmware={d.version}")
asyncio.run(main())
