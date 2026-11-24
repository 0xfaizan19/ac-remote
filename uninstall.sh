#!/bin/bash
# Stops AC Remote and removes it from startup. Your files and data are kept.
launchctl bootout "gui/$(id -u)/com.acremote.server" 2>/dev/null; rm -f "$HOME/Library/LaunchAgents/com.acremote.server.plist"
echo "Type your Mac password if asked."
for L in com.acremote.server com.acremote.relay; do
  sudo launchctl bootout "system/$L" 2>/dev/null; sudo rm -f "/Library/LaunchDaemons/$L.plist"
done
sudo rm -f "/Library/Application Support/AC Remote/relay.py"; sudo rmdir "/Library/Application Support/AC Remote" 2>/dev/null
echo "AC Remote stopped and removed from startup (your files and data were kept)."
