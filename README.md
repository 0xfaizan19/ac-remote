# AC Remote

One app for both of your iPhones to control the Tosot mini-split, at home or away.

The Mac Mini runs a small server that talks to the AC over your home Wi-Fi (Tosot units are made by
Gree and speak Gree's local protocol). Both phones open the app in Safari, add it to the home screen,
and use it like a normal app. Neither phone connects to the AC directly, so they never kick each
other off.

```
Your iPhone ────┐
                ├──► Mac Mini (AC Remote server) ──Wi-Fi──► Tosot AC
Family iPhones ─┘    (at home: Wi-Fi · away: Tailscale)
```

## Features

| Area | What you get |
|---|---|
| Home | Power, temperature dial (+/−), modes Cool / Auto / Heat (Dry and Fan only on the AC's remote), 6 fan speeds, Turbo, Quiet, Sleep, Display light |
| Scenes | One-tap presets: Cool down (16 °C + Turbo) and Warm up (30 °C + Turbo), plus your own |
| Siri | "Hey Siri, turn on the AC" / "turn off the AC", through two Apple Shortcuts that open `/siri/on` or `/siri/off` with the household's Siri key (separate from the passcode). Setup steps and copy buttons are under More → Siri. Home Wi-Fi only |
| Timer | Turn on or off after 15 min to 8 h, visible on both phones |
| Schedules | Weekly on/off times with mode, temperature and fan |
| Automations | Comfort guard (auto cool/heat if the room gets too hot/cold while off); run-time limit |
| Outside temperature | Shown under the room temperature and as a line on the History chart. From Open-Meteo (free, no account) for your town; set the town with `.venv/bin/python set_location.py "Town, Province"` |
| History | Room vs. set temperature chart (6 h to 7 d), hours running today and this week, min/max |
| Activity log | Who changed what and when ("Sam · Set to 22°C", "Schedule · …") |
| More | Up/down and left/right vane positions, X-Fan, ionizer, fresh air, energy saver, 8° heat, °C/°F |
| Live sync | A change on one phone shows on the other within a second |
| Sign-in | A household passcode; each person enters their own name |

## Setting it up on the Mac Mini

The app finds the AC on your Wi-Fi by itself (if you have several, see the table at the end).

1. Open **Terminal** on the Mac Mini and download the app:
   ```bash
   git clone https://github.com/0xfaizan19/ac-remote.git ~/ac-remote
   ```
2. Install it:
   ```bash
   cd ~/ac-remote && ./install.sh
   ```
   It installs what it needs, asks for your Mac password once, and starts two background
   services that run whenever the Mac is on: the app itself (running as you) and a tiny relay
   (`relay.py`, running as root) that passes the app's messages to the AC. It then checks that it
   can reach the AC. No Local Network permission is needed. (macOS blocks Homebrew's unsigned
   Python from sending to the AC in the background with `Broken pipe`, even when Python is switched
   on under Local Network. Programs running as root are exempt, so only the small relay runs as root.)
3. Check that it can see the AC:
   ```bash
   .venv/bin/python find_ac.py
   ```
   It should print a line like `Found: name=a1b2c3d4e5f6 ip=192.168.1.x …`.
4. Check the server log:
   ```bash
   tail -f data/server.log
   ```
   Within about 20 seconds you should see `Connected to AC`. Press Ctrl + C to stop watching.
   The server keeps running.
5. Stop the Mac Mini from sleeping: **System Settings → Energy → Prevent automatic sleeping when the display is off** (turn it on).

If you move the `ac-remote` folder later, run `./install.sh` again from the new location.

## Setting it up on each iPhone

1. On home Wi-Fi, open Safari to the address `install.sh` printed, e.g. `http://Mac-mini.local:8765`.
2. The first person creates the household passcode. Everyone else signs in on their own phone with
   that passcode and their own name.
3. Tap **Share → Add to Home Screen**.
4. Optional: for Siri, open **More → Siri** in the app and follow the steps there (about 2 minutes).

## Using it away from home (Tailscale, free)

Tailscale creates a private, encrypted link between your devices. Nothing is opened to the public internet.

1. Install **Tailscale** on the Mac Mini (tailscale.com/download) and sign in.
2. Install **Tailscale** from the App Store on each iPhone and sign in, with the same account or one
   you invite from the Tailscale admin page.
3. In the Tailscale app, find the Mac Mini's name (e.g. `mac-mini`). On each phone, open
   `http://mac-mini:8765` in Safari and add *that* to the home screen. It works at home and away.

## If something's wrong

| Problem | Fix |
|---|---|
| Log shows `Broken pipe` or `No route to host` | The app isn't going through the relay. Run `./install.sh` again. The relay's own log is `/Library/Logs/ac-remote-relay.log`. |
| "AC offline" | Make sure the AC still works in the Tosot Plus app and that the Mac Mini is on the same Wi-Fi. Tap **More → Reconnect**. |
| Several ACs, or the wrong one | Put `{"ip": "192.168.x.x"}` in `data/config.json` (keep the other lines), then run `./install.sh` again. |
| Forgot the passcode | Delete the `pass_salt` and `pass_hash` lines from `data/config.json` and run `./install.sh`. The app will ask for a new one. |
| Want to check the AC without the app | `.venv/bin/python check_ac.py` prints its current settings and changes nothing |
| See what it's doing | `tail -f data/server.log` |
| Remove it | `./uninstall.sh` |

Test without the AC: `.venv/bin/python server.py --simulate --data-dir data-sim`
Automation tests: `.venv/bin/python tests/test_automation.py`
