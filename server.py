"""AC Remote - one always-on server that both phones use to control the AC.

Run:  .venv/bin/python server.py            (real AC)
      .venv/bin/python server.py --simulate (fake AC, for testing)
Then open http://<this-mac>:8765 on a phone.
"""
import argparse
import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import secrets
import signal
import time
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web

from ac_device import GreeAC, SimulatedAC, validate_changes

ROOT = Path(__file__).parent
CONFIG = DATA = None  # set in main() from --data-dir
STATIC = ROOT / "static"
log = logging.getLogger("server")
# Outside temperature: Open-Meteo is free and needs no account or key. The AC itself doesn't report it.
WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

DEFAULT_SCENES = [
    # Cool down / Warm up use the unit's lowest / highest setpoint (16 / 30 °C) with Turbo.
    {"id": "cooldown", "name": "Cool down", "icon": "❄️",
     "settings": {"power": True, "mode": "cool", "target": 16, "fan": "high", "turbo": True}},
    {"id": "warmup", "name": "Warm up", "icon": "🔥",
     "settings": {"power": True, "mode": "heat", "target": 30, "fan": "high", "turbo": True}},
]
DEFAULT_RULES = {
    "guard": {"enabled": False, "max_room": 28, "min_room": 16, "cool_target": 25, "heat_target": 19},
    "max_runtime": {"enabled": False, "hours": 8},
}


def _load(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    tmp.replace(path)


class App:
    def __init__(self, simulate: bool):
        self.cfg = _load(CONFIG, {})
        self.cfg.setdefault("secret", secrets.token_hex(32))
        self.cfg.setdefault("siri_key", secrets.token_urlsafe(12))  # for the /siri/on and /siri/off links
        self.simulate = simulate
        _save(CONFIG, self.cfg)
        self.data = _load(DATA, {})
        self.data.setdefault("scenes", DEFAULT_SCENES)
        self.data.setdefault("schedules", [])
        self.data.setdefault("rules", DEFAULT_RULES)
        self.data.setdefault("log", [])
        self.data.setdefault("history", [])
        self.data.setdefault("timer", None)
        self.data.setdefault("on_since", None)
        self.ac = SimulatedAC() if simulate else GreeAC(
            ip=self.cfg.get("ip"), mac=self.cfg.get("mac"), subnet=self.cfg.get("subnet"))
        self.state = None
        self.error = None
        self.last_seen = None
        self.outside = None  # {"c": °C, "t": when fetched, "place": town}
        self.clients: set[asyncio.Queue] = set()
        self.login_fails: dict[str, list[float]] = {}

    # ---------- persistence / events ----------
    def save(self):
        _save(DATA, self.data)

    def add_log(self, who, text):
        self.data["log"].append({"t": time.time(), "who": who, "text": text})
        self.data["log"] = self.data["log"][-500:]
        self.save()

    def public_state(self):
        return {
            "ac": self.state, "connected": self.ac.connected and self.state is not None,
            "error": self.error, "last_seen": self.last_seen, "info": self.ac.info,
            "simulated": self.simulate, "timer": self.data["timer"],
            "scenes": self.data["scenes"], "schedules": self.data["schedules"],
            "rules": self.data["rules"], "log": self.data["log"][-60:][::-1], "stats": self.stats(),
            "outside": self.outside_now(),
        }

    def outside_now(self):
        """Outside temperature in the AC's units, or None if unknown or more than 2 hours old."""
        o = self.outside
        if not o or time.time() - o["t"] > 7200:
            return None
        f = (self.state or {}).get("units") == "F"
        return {"temp": round(o["c"] * 9 / 5 + 32) if f else round(o["c"]), "units": "F" if f else "C",
                "place": o["place"], "t": o["t"]}

    def broadcast(self):
        msg = json.dumps(self.public_state())
        for q in list(self.clients):
            if q.qsize() < 20:
                q.put_nowait(msg)

    def stats(self):
        now = time.time()
        today0 = dt.datetime.combine(dt.date.today(), dt.time()).timestamp()
        hist = self.data["history"]

        def runtime(since):
            # Each sample covers ~1 minute.
            return sum(1 for h in hist if h["t"] >= since and h.get("on")) / 60

        rooms = [h["room"] for h in hist if h["t"] >= now - 86400 and h.get("room") is not None]
        return {
            "on_hours_today": round(runtime(today0), 1),
            "on_hours_week": round(runtime(now - 7 * 86400), 1),
            "room_min_24h": min(rooms) if rooms else None,
            "room_max_24h": max(rooms) if rooms else None,
            "on_since": self.data.get("on_since"),
        }

    # ---------- AC actions ----------
    async def apply(self, changes, who, label=None):
        changes = validate_changes(changes)
        before = dict(self.state or {})
        self.state = await self.ac.apply(changes)
        self.last_seen = time.time()
        self.error = None
        self._track_power(before)
        if "power" in changes and not (who in ("Schedule", "Comfort guard", "Run-time limit") or who.startswith("Timer")):
            self.data["last_manual_power"] = time.time()
        desc = label or describe(changes, self.state)
        self.add_log(who, desc)
        self.broadcast()

    def _track_power(self, before):
        on = self.state and self.state.get("power")
        if on and not self.data.get("on_since"):
            self.data["on_since"] = time.time()
        if not on:
            self.data["on_since"] = None

    # ---------- background loops ----------
    async def connect_loop(self):
        delay = 5
        while True:
            if not self.ac.connected:
                try:
                    await self.ac.connect()
                    self.state = await self.ac.refresh()
                    self.last_seen = time.time()
                    self.error = None
                    log.info("Connected to AC %s", self.ac.info)
                    self.broadcast()
                    delay = 5
                except Exception as e:  # noqa: BLE001
                    self.error = f"Can't reach the AC: {e}"
                    log.warning(self.error)
                    self.broadcast()
                    delay = min(delay * 2, 120)
            await asyncio.sleep(delay if not self.ac.connected else 10)

    async def poll_loop(self):
        fails = 0
        while True:
            await asyncio.sleep(10)
            if not self.ac.connected:
                continue
            try:
                new = await self.ac.refresh()
                changed = new != self.state
                before = dict(self.state or {})
                self.state = new
                self.last_seen = time.time()
                self.error = None
                fails = 0
                if changed:
                    if before and before.get("power") != new.get("power"):
                        self.add_log("Remote / other app", "Turned " + ("on" if new["power"] else "off"))
                    self._track_power(before)
                    self.broadcast()
            except Exception as e:  # noqa: BLE001
                fails += 1
                self.error = f"Lost contact with the AC ({e})"
                if fails >= 3:
                    await self.ac.close()
                self.broadcast()

    async def history_loop(self):
        while True:
            if self.state:
                self.data["history"].append({
                    "t": round(time.time()), "room": self.state.get("room"),
                    "target": self.state.get("target"), "on": self.state.get("power"),
                    "mode": self.state.get("mode"), "out": (self.outside_now() or {}).get("temp")})
                self.data["history"] = [h for h in self.data["history"] if h["t"] > time.time() - 14 * 86400]
                self.save()
            await asyncio.sleep(60)

    async def weather_loop(self):
        """Every 10 minutes, fetch the outside temperature for the town saved by set_location.py.
        The town is re-read from config.json each minute, so setting it needs no restart."""
        last_loc, last_fetch = None, 0
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as http:
            while True:
                loc = _load(CONFIG, {}).get("location")
                if loc and (loc != last_loc or time.time() - last_fetch >= 600):
                    last_loc, last_fetch = loc, time.time()
                    try:
                        q = {"latitude": loc["lat"], "longitude": loc["lon"], "current": "temperature_2m"}
                        async with http.get(WEATHER_URL, params=q) as r:
                            r.raise_for_status()
                            c = float((await r.json())["current"]["temperature_2m"])
                        before = self.outside_now()
                        self.outside = {"c": c, "t": time.time(), "place": loc.get("name", "")}
                        if before is None or before["temp"] != self.outside_now()["temp"]:
                            self.broadcast()
                    except Exception as e:  # noqa: BLE001
                        log.warning("Couldn't get the outside temperature: %s", e)
                await asyncio.sleep(60)

    async def automation_loop(self):
        last_min = None
        while True:
            await asyncio.sleep(5)
            now = dt.datetime.now()
            try:
                await self._run_timer()
                minute = now.strftime("%Y-%m-%d %H:%M")
                if minute != last_min:
                    last_min = minute
                    await self._run_schedules(now)
                    await self._run_rules()
            except Exception as e:  # noqa: BLE001
                log.exception("automation error: %s", e)

    async def _run_timer(self):
        t = self.data["timer"]
        if t and time.time() >= t["at"] and self.ac.connected:
            self.data["timer"] = None
            self.save()
            await self.apply({"power": t["action"] == "on"}, f"Timer (set by {t['by']})",
                             f"Timer turned the AC {t['action']}")

    async def _run_schedules(self, now):
        hm = now.strftime("%H:%M")
        for s in self.data["schedules"]:
            if s.get("enabled") and s.get("time") == hm and now.weekday() in s.get("days", []):
                if not self.ac.connected:
                    self.add_log("Schedule", f"Skipped '{s.get('name')}' - AC offline")
                    continue
                ch = {"power": False} if s["action"] == "off" else {"power": True, **s.get("settings", {})}
                await self.apply(ch, "Schedule", f"Schedule '{s.get('name') or hm}' ran")

    async def _run_rules(self):
        if not (self.state and self.ac.connected):
            return
        r = self.data["rules"]
        g, room = r["guard"], self.state.get("room")
        recently_touched = time.time() - self.data.get("last_manual_power", 0) < 3600
        if g["enabled"] and room is not None and not self.state["power"] and not recently_touched:
            if room >= g["max_room"]:
                await self.apply({"power": True, "mode": "cool", "target": g["cool_target"]}, "Comfort guard",
                                 f"Room hit {room}° - comfort guard started cooling")
            elif room <= g["min_room"]:
                await self.apply({"power": True, "mode": "heat", "target": g["heat_target"]}, "Comfort guard",
                                 f"Room fell to {room}° - comfort guard started heating")
        m = r["max_runtime"]
        since = self.data.get("on_since")
        if m["enabled"] and self.state["power"] and since and time.time() - since > m["hours"] * 3600:
            await self.apply({"power": False}, "Run-time limit", f"Turned off after {m['hours']} h running")


def describe(ch, st):
    u = "°" + (st or {}).get("units", "C")
    names = {"power": "Power", "turbo": "Turbo", "quiet": "Quiet", "sleep": "Sleep mode", "light": "Display light",
             "xfan": "X-Fan (dry coil)", "anion": "Health / ionizer", "fresh_air": "Fresh air",
             "power_save": "Energy saver", "steady_heat": "8° heat (freeze guard)"}
    parts = []
    for k, v in ch.items():
        if k == "power":
            parts.append("Turned " + ("on" if v else "off"))
        elif k in names:
            parts.append(f"{names[k]} {'on' if v else 'off'}")
        elif k == "target":
            parts.append(f"Set to {v}{u}")
        elif k == "mode":
            parts.append(f"Mode: {v}")
        elif k == "fan":
            parts.append("Fan: " + v.replace("_", " "))
        elif k in ("vswing", "hswing"):
            parts.append(("Up/down" if k == "vswing" else "Left/right") + " vanes: " + v.replace("_", " "))
        elif k == "units":
            parts.append(f"Units: °{v}")
    return ", ".join(parts)


# ---------------- auth ----------------
def _hash(passcode, salt):
    return hashlib.pbkdf2_hmac("sha256", passcode.encode(), bytes.fromhex(salt), 200_000).hex()


def make_token(app: App, name):
    payload = base64.urlsafe_b64encode(json.dumps({"n": name, "i": time.time()}).encode()).decode()
    sig = hmac.new(app.cfg["secret"].encode(), payload.encode(), "sha256").hexdigest()
    return f"{payload}.{sig}"


def read_token(app: App, tok):
    try:
        payload, sig = tok.split(".")
        good = hmac.new(app.cfg["secret"].encode(), payload.encode(), "sha256").hexdigest()
        if hmac.compare_digest(sig, good):
            return json.loads(base64.urlsafe_b64decode(payload))["n"]
    except Exception:  # noqa: BLE001
        pass
    return None


def user_of(req):
    return read_token(req.app["core"], req.cookies.get("acr", ""))


@web.middleware
async def auth_mw(req, handler):
    open_paths = ("/api/session", "/api/login", "/api/setup")
    if req.path.startswith("/api/") and req.path not in open_paths and not user_of(req):
        return web.json_response({"error": "Please sign in"}, status=401)
    return await handler(req)


# ---------------- routes ----------------
routes = web.RouteTableDef()


@routes.get("/")
async def index(req):
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@routes.get("/api/session")
async def session(req):
    core = req.app["core"]
    return web.json_response({"name": user_of(req), "setup_needed": "pass_hash" not in core.cfg})


@routes.post("/api/setup")
async def setup(req):
    core = req.app["core"]
    if "pass_hash" in core.cfg:
        return web.json_response({"error": "Already set up"}, status=400)
    body = await req.json()
    pc, name = str(body.get("passcode", "")), str(body.get("name", "")).strip()[:30]
    if len(pc) < 4 or not name:
        return web.json_response({"error": "Passcode needs 4+ characters and a name"}, status=400)
    salt = secrets.token_hex(16)
    core.cfg.update(pass_salt=salt, pass_hash=_hash(pc, salt))
    _save(CONFIG, core.cfg)
    resp = web.json_response({"ok": True, "name": name})
    resp.set_cookie("acr", make_token(core, name), max_age=400 * 86400, httponly=True, samesite="Lax")
    core.add_log(name, "Created the household passcode")
    return resp


@routes.post("/api/login")
async def login(req):
    core = req.app["core"]
    ip = req.remote or "?"
    fails = [t for t in core.login_fails.get(ip, []) if t > time.time() - 600]
    if len(fails) >= 8:
        return web.json_response({"error": "Too many tries. Wait 10 minutes."}, status=429)
    body = await req.json()
    pc, name = str(body.get("passcode", "")), str(body.get("name", "")).strip()[:30]
    if "pass_hash" not in core.cfg or not hmac.compare_digest(_hash(pc, core.cfg["pass_salt"]), core.cfg["pass_hash"]):
        core.login_fails[ip] = fails + [time.time()]
        return web.json_response({"error": "Wrong passcode"}, status=403)
    if not name:
        return web.json_response({"error": "Enter your name"}, status=400)
    resp = web.json_response({"ok": True, "name": name})
    resp.set_cookie("acr", make_token(core, name), max_age=400 * 86400, httponly=True, samesite="Lax")
    return resp


@routes.post("/api/logout")
async def logout(req):
    resp = web.json_response({"ok": True})
    resp.del_cookie("acr")
    return resp


@routes.get("/api/state")
async def get_state(req):
    return web.json_response(req.app["core"].public_state())


@routes.get("/api/events")
async def events(req):
    core = req.app["core"]
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                       "X-Accel-Buffering": "no"})
    await resp.prepare(req)
    q = asyncio.Queue()
    core.clients.add(q)
    try:
        await resp.write(f"data: {json.dumps(core.public_state())}\n\n".encode())
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), 25)
                await resp.write(f"data: {msg}\n\n".encode())
            except asyncio.TimeoutError:
                await resp.write(b": ping\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        core.clients.discard(q)
    return resp


async def _guard(fn):
    try:
        await fn()
        return web.json_response({"ok": True})
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except Exception as e:  # noqa: BLE001
        log.exception("command failed")
        return web.json_response({"error": f"The AC didn't respond: {e}"}, status=502)


@routes.post("/api/set")
async def set_state(req):
    core, body = req.app["core"], await req.json()
    if not core.ac.connected:
        return web.json_response({"error": "The AC is offline right now"}, status=503)
    return await _guard(lambda: core.apply(body, user_of(req)))


@routes.post("/api/scene/{id}")
async def run_scene(req):
    core = req.app["core"]
    sc = next((s for s in core.data["scenes"] if s["id"] == req.match_info["id"]), None)
    if not sc:
        return web.json_response({"error": "No such scene"}, status=404)
    return await _guard(lambda: core.apply(sc["settings"], user_of(req), f"Ran scene '{sc['name']}'"))


@routes.post("/api/scenes")
async def save_scenes(req):
    core, body = req.app["core"], await req.json()
    scenes = []
    for s in body.get("scenes", [])[:20]:
        scenes.append({"id": s.get("id") or uuid.uuid4().hex[:8], "name": str(s["name"])[:40],
                       "icon": str(s.get("icon", "⭐"))[:4], "settings": validate_changes(s["settings"])})
    core.data["scenes"] = scenes
    core.add_log(user_of(req), "Updated scenes")
    core.broadcast()
    return web.json_response({"ok": True})


@routes.post("/api/schedules")
async def save_schedules(req):
    core, body = req.app["core"], await req.json()
    out = []
    for s in body.get("schedules", [])[:40]:
        hh, mm = str(s["time"]).split(":")
        assert 0 <= int(hh) < 24 and 0 <= int(mm) < 60
        out.append({"id": s.get("id") or uuid.uuid4().hex[:8], "name": str(s.get("name", ""))[:40],
                    "enabled": bool(s.get("enabled", True)), "time": f"{int(hh):02d}:{int(mm):02d}",
                    "days": sorted({int(d) for d in s.get("days", []) if 0 <= int(d) <= 6}),
                    "action": "off" if s.get("action") == "off" else "on",
                    "settings": validate_changes(s.get("settings", {}))})
    core.data["schedules"] = out
    core.add_log(user_of(req), "Updated schedules")
    core.broadcast()
    return web.json_response({"ok": True})


@routes.post("/api/rules")
async def save_rules(req):
    core, body = req.app["core"], await req.json()
    g, m = body["guard"], body["max_runtime"]
    core.data["rules"] = {
        "guard": {"enabled": bool(g["enabled"]), "max_room": float(g["max_room"]), "min_room": float(g["min_room"]),
                  "cool_target": int(g["cool_target"]), "heat_target": int(g["heat_target"])},
        "max_runtime": {"enabled": bool(m["enabled"]), "hours": max(0.5, float(m["hours"]))},
    }
    core.add_log(user_of(req), "Updated automations")
    core.broadcast()
    return web.json_response({"ok": True})


@routes.post("/api/timer")
async def set_timer(req):
    core, body = req.app["core"], await req.json()
    who = user_of(req)
    if body.get("cancel"):
        core.data["timer"] = None
        core.add_log(who, "Cancelled the timer")
    else:
        mins = max(1, min(24 * 60, int(body["minutes"])))
        action = "on" if body.get("action") == "on" else "off"
        core.data["timer"] = {"at": time.time() + mins * 60, "action": action, "by": who}
        core.add_log(who, f"Timer: turn {action} in {mins} min")
    core.broadcast()
    return web.json_response({"ok": True})


@routes.get("/api/history")
async def history(req):
    core = req.app["core"]
    hours = max(1, min(14 * 24, int(req.query.get("hours", 24))))
    since = time.time() - hours * 3600
    return web.json_response([h for h in core.data["history"] if h["t"] >= since])


# ---------------- Siri ----------------
# Each iPhone gets two Apple Shortcuts ("Turn on the AC" / "Turn off the AC"), each a single
# "Get Contents of URL" action that opens /siri/on or /siri/off with the household's Siri key, so
# "Hey Siri, turn on the AC" works. The key is separate from the passcode, and the More page shows
# the links to signed-in people. Like the app, it only works on the home Wi-Fi.
@routes.get("/api/siri")
async def siri_key(req):
    return web.json_response({"key": req.app["core"].cfg["siri_key"]})


@routes.get("/siri/{action}", allow_head=False)
async def siri(req):
    core = req.app["core"]
    if not hmac.compare_digest(req.query.get("key", ""), core.cfg["siri_key"]):
        return web.Response(text="This Siri link doesn't work. Copy it again from the AC Remote app.", status=403)
    action = req.match_info["action"]
    if action not in ("on", "off"):
        return web.Response(text="Unknown command.", status=404)
    if not core.ac.connected:
        return web.Response(text="The AC is offline right now.", status=503)
    by = req.query.get("by", "").strip()[:30]
    try:
        await core.apply({"power": action == "on"}, f"{by} via Siri" if by else "Siri")
    except Exception:  # noqa: BLE001
        log.exception("Siri command failed")
        return web.Response(text="The AC didn't respond. Try again.", status=502)
    return web.Response(text=f"OK, the AC is {action}.")


@routes.post("/api/reconnect")
async def reconnect(req):
    core = req.app["core"]
    await core.ac.close()
    core.add_log(user_of(req), "Asked to reconnect to the AC")
    return web.json_response({"ok": True})


async def on_startup(app):
    core = app["core"]
    app["tasks"] = [asyncio.create_task(f()) for f in
                    (core.connect_loop, core.poll_loop, core.history_loop, core.automation_loop,
                     core.weather_loop)]


async def on_cleanup(app):
    for t in app["tasks"]:
        t.cancel()
    await app["core"].ac.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--simulate", action="store_true", help="use a fake AC for testing")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--data-dir", default="data", help="folder for settings, schedules and history")
    a = p.parse_args()
    global CONFIG, DATA
    d = ROOT / a.data_dir
    CONFIG, DATA = d / "config.json", d / "data.json"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app = web.Application(middlewares=[auth_mw])
    app["core"] = App(a.simulate)
    app.add_routes(routes)
    app.router.add_static("/static", STATIC)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    # Stop at once on SIGTERM (how launchd stops and restarts the service). aiohttp's graceful
    # shutdown can hang while phones hold their live-update connections open. Saves are atomic
    # (write a temp file, then rename), so exiting immediately can't corrupt anything.
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    web.run_app(app, host=a.host, port=a.port, handle_signals=False)


if __name__ == "__main__":
    main()
