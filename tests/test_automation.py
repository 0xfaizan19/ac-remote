"""Checks schedules, timer, comfort guard and run-time limit against the simulated AC."""
import asyncio, datetime as dt, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server

async def main():
    d = Path(tempfile.mkdtemp())
    server.CONFIG, server.DATA = d / "c.json", d / "d.json"
    core = server.App(simulate=True)
    await core.ac.connect(); core.state = await core.ac.refresh()
    ok = lambda c, m: print(("PASS " if c else "FAIL ") + m) or c
    res = []
    now = dt.datetime.now()
    core.data["schedules"] = [{"name": "t", "enabled": True, "time": now.strftime("%H:%M"), "days": [now.weekday()],
                               "action": "on", "settings": {"mode": "heat", "target": 25, "fan": "high"}}]
    await core._run_schedules(now)
    res.append(ok(core.state["power"] and core.state["mode"] == "heat" and core.state["target"] == 25 and core.state["fan"] == "high", "schedule turned on heat 25 fan high"))
    core.data["schedules"][0]["days"] = [(now.weekday() + 1) % 7]
    await core.apply({"power": False}, "test")
    await core._run_schedules(now)
    res.append(ok(not core.state["power"], "schedule skipped on other days"))
    core.data["timer"] = {"at": time.time() - 1, "action": "on", "by": "test"}
    await core._run_timer()
    res.append(ok(core.state["power"] and core.data["timer"] is None, "timer turned AC on and cleared"))
    await core.apply({"power": False}, "test")
    core.data["rules"]["guard"].update(enabled=True, max_room=20, cool_target=23)
    core.state["room"] = 26
    await core._run_rules()
    res.append(ok(not core.state["power"], "comfort guard waits 1 h after someone turns it off by hand"))
    core.data["last_manual_power"] = 0
    await core._run_rules()
    res.append(ok(core.state["power"] and core.state["mode"] == "cool" and core.state["target"] == 23, "comfort guard started cooling when hot"))
    core.data["rules"]["guard"]["enabled"] = False
    core.data["rules"]["max_runtime"].update(enabled=True, hours=1)
    core.data["on_since"] = time.time() - 7200
    await core._run_rules()
    res.append(ok(not core.state["power"], "run-time limit turned it off after 1 h"))
    try:
        server.validate_changes({"turbo": True, "quiet": True}); v = server.validate_changes({"turbo": True})
        res.append(ok(v["quiet"] is False, "turbo forces quiet off"))
    except Exception as e:
        res.append(ok(False, f"validate {e}"))
    print("log:", [l["who"] + ": " + l["text"] for l in core.data["log"]][-4:])
    print("ALL PASS" if all(res) else "SOME FAILED")
asyncio.run(main())
