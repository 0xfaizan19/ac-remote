#!/bin/bash
# Installs AC Remote on this Mac and keeps it running (starts when the Mac starts, restarts if it crashes).
#
# Two background services (launchd daemons):
#   com.acremote.server  the app itself, running as you
#   com.acremote.relay   a tiny relay (relay.py) running as root that passes the app's packets to the AC
# Why the relay: macOS 15+ blocks Homebrew's unsigned Python from sending to devices on the home
# network, even with Python switched on under Local Network (Apple TN3179). Programs running as root
# are exempt, so only the relay runs as root (standard library only, on Apple's own python3).
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"
ME="$(id -un)"
SERVER=com.acremote.server
RELAY=com.acremote.relay
SUPPORT="/Library/Application Support/AC Remote"
SYSPY=/Library/Developer/CommandLineTools/usr/bin/python3
[ -x "$SYSPY" ] || SYSPY=/usr/bin/python3
command -v python3 >/dev/null || { echo "Python 3 is needed: install it from https://www.python.org/downloads/ and run this again."; exit 1; }
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
mkdir -p "$DIR/data"

# Remove the older per-user version, if present (it can't reach the AC).
launchctl bootout "gui/$(id -u)/$SERVER" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$SERVER.plist"

T_SERVER="$(mktemp)"
T_RELAY="$(mktemp)"
cat > "$T_SERVER" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
 <key>Label</key><string>$SERVER</string>
 <key>UserName</key><string>$ME</string>
 <key>ProgramArguments</key><array><string>$DIR/.venv/bin/python</string><string>$DIR/server.py</string></array>
 <key>EnvironmentVariables</key><dict><key>AC_RELAY</key><string>127.0.0.1:7001</string></dict>
 <key>WorkingDirectory</key><string>$DIR</string>
 <key>RunAtLoad</key><true/>
 <key>KeepAlive</key><true/>
 <key>StandardOutPath</key><string>$DIR/data/server.log</string>
 <key>StandardErrorPath</key><string>$DIR/data/server.log</string>
</dict></plist>
PL
cat > "$T_RELAY" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
 <key>Label</key><string>$RELAY</string>
 <key>ProgramArguments</key><array><string>$SYSPY</string><string>-I</string><string>$SUPPORT/relay.py</string><string>$DIR/data/ac_target</string></array>
 <key>RunAtLoad</key><true/>
 <key>KeepAlive</key><true/>
 <key>StandardOutPath</key><string>/Library/Logs/ac-remote-relay.log</string>
 <key>StandardErrorPath</key><string>/Library/Logs/ac-remote-relay.log</string>
</dict></plist>
PL
touch data/server.log
START=$(wc -c < data/server.log)
echo
echo "Installing the background services. Type your Mac password when asked (it won't show as you type)."
sudo mkdir -p "$SUPPORT"
sudo install -m 644 -o root -g wheel relay.py "$SUPPORT/relay.py"
sudo install -m 644 -o root -g wheel "$T_RELAY" "/Library/LaunchDaemons/$RELAY.plist"
sudo install -m 644 -o root -g wheel "$T_SERVER" "/Library/LaunchDaemons/$SERVER.plist"
rm -f "$T_SERVER" "$T_RELAY"
# (Re)start a service, waiting for the old copy to stop first: starting it again too soon fails
# with "Bootstrap failed: 5: Input/output error".
restart() {
  local L="$1"
  sudo launchctl bootout "system/$L" 2>/dev/null || true
  for i in $(seq 1 30); do launchctl print "system/$L" >/dev/null 2>&1 || break; sleep 0.5; done
  for i in 1 2 3 4; do
    sudo launchctl bootstrap system "/Library/LaunchDaemons/$L.plist" 2>/dev/null && return 0
    sleep 2
  done
  sudo launchctl bootstrap system "/Library/LaunchDaemons/$L.plist"
}
restart "$RELAY"
restart "$SERVER"

echo "Started. Checking it can reach the AC (up to a minute)..."
OK=
for i in $(seq 1 60); do
  sleep 1
  if tail -c +$((START + 1)) data/server.log | grep -q "Connected to AC"; then OK=1; break; fi
done
echo
if [ -n "$OK" ]; then
  echo "AC Remote is running and connected to your AC."
else
  echo "AC Remote is running, but it hasn't connected to the AC yet."
  echo "Watch the log with:  tail -f $DIR/data/server.log"
fi
echo "  On this Mac:          http://localhost:8765"
echo "  On phones at home:    http://$(scutil --get LocalHostName).local:8765"
echo "  Log file:             $DIR/data/server.log"
