"""Talks to the Tosot (Gree-made) mini-split over the home Wi-Fi.

Two backends with the same interface:
  * GreeAC     - the real unit, via the local Gree protocol (UDP 7000)
  * SimulatedAC - a fake unit for testing the app without the AC
"""
import asyncio
import ipaddress
import logging
import os
import random
import socket
import time
from pathlib import Path

log = logging.getLogger("ac")

# When AC Remote runs as a background service, macOS blocks it from sending straight to the AC
# (see relay.py), so the installer sets AC_RELAY and the packets go through the small root relay.
# Run by hand from Terminal (which macOS allows), it talks to the AC directly.
RELAY = os.environ.get("AC_RELAY")  # e.g. "127.0.0.1:7001"
RELAY_TARGET = Path(__file__).resolve().parent / "data" / "ac_target"

MODES = ["auto", "cool", "dry", "fan", "heat"]
FANS = ["auto", "low", "medium_low", "medium", "medium_high", "high"]
VSWING = ["default", "full", "fixed_top", "fixed_upper_mid", "fixed_mid", "fixed_lower_mid",
          "fixed_bottom", "swing_top", "swing_upper_mid", "swing_mid", "swing_lower_mid", "swing_bottom"]
HSWING = ["default", "full", "left", "left_center", "center", "right_center", "right"]
TOGGLES = ["power", "turbo", "quiet", "sleep", "light", "xfan", "anion", "fresh_air",
           "power_save", "steady_heat"]
ALL_COLUMNS = ["Pow", "Mod", "SetTem", "TemUn", "TemRec", "TemSen", "WdSpd", "Air", "Blo",
               "Health", "SwhSlp", "SlpMod", "Lig", "SwingLfRig", "SwUpDn", "Quiet", "Tur",
               "StHt", "SvSt", "HeatCoolType"]
TEMP_MIN_C, TEMP_MAX_C = 16, 30


def validate_changes(ch: dict) -> dict:
    """Clean and range-check a change request coming from a phone."""
    out = {}
    for k, v in ch.items():
        if k in TOGGLES:
            out[k] = bool(v)
        elif k == "mode" and v in MODES:
            out[k] = v
        elif k == "fan" and v in FANS:
            out[k] = v
        elif k == "vswing" and v in VSWING:
            out[k] = v
        elif k == "hswing" and v in HSWING:
            out[k] = v
        elif k == "units" and v in ("C", "F"):
            out[k] = v
        elif k == "target":
            out[k] = int(round(float(v)))
        else:
            raise ValueError(f"Unknown or invalid setting: {k}={v!r}")
    # Turbo and quiet are mutually exclusive on Gree units.
    if out.get("turbo"):
        out["quiet"] = False
    if out.get("quiet"):
        out["turbo"] = False
    return out


class SimulatedAC:
    """Pretend AC so the app can be tested end to end without hardware."""

    name = "Simulated AC"

    def __init__(self):
        self.state = dict(power=False, mode="cool", target=22, units="C", room=26.0, fan="auto",
                          vswing="default", hswing="default", **{t: False for t in TOGGLES if t != "power"})
        self.state["light"] = True
        self.connected = False
        self.info = {"ip": "simulated", "mac": "00:00:00:00:00:00", "brand": "tosot", "model": "sim"}

    async def connect(self):
        await asyncio.sleep(0.2)
        self.connected = True

    async def refresh(self) -> dict:
        s = self.state
        # Room drifts toward the setpoint when running, toward 27C when off.
        goal = s["target"] if s["power"] and s["mode"] in ("cool", "heat", "auto", "dry") else 27.0
        s["room"] = round(s["room"] + (goal - s["room"]) * 0.08 + random.uniform(-0.05, 0.05), 1)
        return self.snapshot()

    def snapshot(self) -> dict:
        s = dict(self.state)
        if s["units"] == "F":
            s["room"] = round(s["room"] * 9 / 5 + 32)
            s["target"] = round(s["target"] * 9 / 5 + 32)
        else:
            s["room"] = round(s["room"])
        return s

    async def apply(self, ch: dict) -> dict:
        ch = validate_changes(ch)
        if "target" in ch:
            lo, hi = (TEMP_MIN_C, TEMP_MAX_C) if self.state["units"] == "C" else (61, 86)
            if self.state["units"] == "F":
                ch["target"] = round((ch["target"] - 32) * 5 / 9)
            ch["target"] = max(TEMP_MIN_C, min(TEMP_MAX_C, ch["target"]))
        self.state.update(ch)
        await asyncio.sleep(0.1)
        return self.snapshot()

    async def close(self):
        self.connected = False


class GreeAC:
    """The real unit over Wi-Fi, using the greeclimate library."""

    def __init__(self, ip: str | None = None, mac: str | None = None, subnet: str | None = None):
        self.ip, self.mac, self.subnet = ip, mac, subnet
        self.dev = None
        self.connected = False
        self.info = {}
        self.name = "Tosot AC"
        self._lock = asyncio.Lock()
        self._cipher_cls = None

    async def _fetch(self, timeout: float = 6):
        """Ask the AC for its settings and wait for the reply to arrive."""
        d = self.dev
        d._valid_state.clear()
        await d.update_state(columns=ALL_COLUMNS)
        try:
            await asyncio.wait_for(d._valid_state.wait(), timeout)
        except asyncio.TimeoutError:
            raise ConnectionError("The AC didn't answer the status request") from None

    async def discover(self, wait: int = 6):
        from greeclimate.discovery import Discovery
        disc = Discovery()
        targets = None
        if self.ip:
            targets = [ipaddress.IPv4Address(self.ip)]
        elif self.subnet:
            targets = list(ipaddress.IPv4Network(self.subnet, strict=False).hosts())
        found = await disc.scan(wait_for=wait, target_addresses=targets)
        if not found and targets is None:
            # Broadcast found nothing; try every address on our own /24.
            net = ipaddress.IPv4Network(_local_ip() + "/24", strict=False)
            found = await disc.scan(wait_for=wait, target_addresses=list(net.hosts()))
        disc.close()
        if self.mac:
            found = [d for d in found if d.mac.replace(":", "").lower() == self.mac.replace(":", "").lower()]
        return found

    async def connect(self):
        from greeclimate.air_conditioner import AirConditionerDevice
        found = await self.discover()
        if not found:
            raise ConnectionError("AC not found on the network (is it on Wi-Fi and on the same network?)")
        info = found[0]
        log.info("Found AC: %s", info)
        link = self._link(info)
        if self.dev:
            self.dev.close()
        from greeclimate.cipher import CipherV1, CipherV2
        self.dev = AirConditionerDevice(link)
        # Remember which login method worked so reconnects don't wait for the other one to time out.
        if self._cipher_cls:
            try:
                await self.dev.bind(cipher=self._cipher_cls())
            except Exception:  # noqa: BLE001
                self.dev.close()
                self.dev = AirConditionerDevice(link)
                self._cipher_cls = None
        if not self._cipher_cls:
            await self.dev.bind()
            self._cipher_cls = type(self.dev.device_cipher)
        await self._fetch()
        self.info = {"ip": info.ip, "mac": info.mac, "brand": info.brand, "model": info.model,
                     "version": info.version, "name": info.name}
        self.name = info.name or self.name
        self.connected = True

    @staticmethod
    def _link(info):
        """Where to send the AC's packets: straight to it, or through the root relay (see RELAY)."""
        if not RELAY:
            return info
        from greeclimate.deviceinfo import DeviceInfo
        host, port = RELAY.rsplit(":", 1)
        RELAY_TARGET.parent.mkdir(parents=True, exist_ok=True)
        RELAY_TARGET.write_text(info.ip)
        log.info("Talking to the AC through the relay at %s", RELAY)
        return DeviceInfo(host, int(port), info.mac, info.name, brand=info.brand, model=info.model,
                          version=info.version, mid=getattr(info, "mid", None))

    async def refresh(self) -> dict:
        async with self._lock:
            await self._fetch()
            return self.snapshot()

    def snapshot(self) -> dict:
        d = self.dev

        def idx(v, names):
            return names[v] if isinstance(v, int) and 0 <= v < len(names) else names[0]

        return {
            "power": bool(d.power), "mode": idx(d.mode, MODES), "target": d.target_temperature,
            "units": "F" if d.temperature_units == 1 else "C", "room": d.current_temperature,
            "fan": idx(d.fan_speed, FANS), "vswing": idx(d.vertical_swing, VSWING),
            "hswing": idx(d.horizontal_swing, HSWING), "turbo": bool(d.turbo), "quiet": bool(d.quiet),
            "sleep": bool(d.sleep), "light": bool(d.light), "xfan": bool(d.xfan), "anion": bool(d.anion),
            "fresh_air": bool(d.fresh_air), "power_save": bool(d.power_save),
            "steady_heat": bool(d.steady_heat),
        }

    async def apply(self, ch: dict) -> dict:
        ch = validate_changes(ch)
        async with self._lock:
            d = self.dev
            if "units" in ch:
                d.temperature_units = 1 if ch["units"] == "F" else 0
            for k, v in ch.items():
                if k == "mode":
                    d.mode = MODES.index(v)
                elif k == "fan":
                    d.fan_speed = FANS.index(v)
                elif k == "vswing":
                    d.vertical_swing = VSWING.index(v)
                elif k == "hswing":
                    d.horizontal_swing = HSWING.index(v)
                elif k == "target":
                    lo, hi = (TEMP_MIN_C, TEMP_MAX_C) if d.temperature_units != 1 else (61, 86)
                    d.target_temperature = max(lo, min(hi, v))
                elif k in TOGGLES:
                    setattr(d, k, v)
            await d.push_state_update()
            await asyncio.sleep(0.6)
            await self._fetch()
            return self.snapshot()

    async def close(self):
        if self.dev:
            self.dev.close()
        self.connected = False


def _local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "192.168.1.1"
    finally:
        s.close()
