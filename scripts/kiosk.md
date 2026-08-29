# Touchscreen kiosk mode (do this last)

Once a DSI touchscreen is attached to the Pi:

```bash
sudo apt-get install -y chromium-browser unclutter
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/mise-kiosk.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Mise Kiosk
Exec=chromium-browser --kiosk --noerrdialogs --disable-infobars --incognito \
  --check-for-update-interval=31536000 http://localhost:8000
X-GNOME-Autostart-enabled=true
EOF
```

Hide the cursor: `unclutter -idle 0.5 -root &` in the same autostart.

Stop the screen blanking:
```bash
sudo raspi-config nonint do_blanking 1
```

Notes:
- The Pi 5 uses **22-pin FPC** DSI connectors, the same style as your camera
  cable — not the older 15-pin. Check before ordering a panel.
- Run the kiosk against `localhost`, not `mise.local`, so it survives losing WiFi.
