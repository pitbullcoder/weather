# weather
MeshCore Station G3 Weather Application for Pi Zero 2w

# weather-bot install (on the Pi, as your login user)

# 1. Create the app dir and venv (Bookworm blocks system-wide pip)
sudo mkdir -p /opt/weather-bot
sudo chown $USER /opt/weather-bot
python3 -m venv /opt/weather-bot/venv
/opt/weather-bot/venv/bin/pip install meshcore

# 2. Copy in the script
cp weather_bot.py /opt/weather-bot/

# 3. Test WITHOUT transmitting (checks NWS fetch + formatting)
/opt/weather-bot/venv/bin/python /opt/weather-bot/weather_bot.py --dry-run

# 4. One real send (creates #weather channel slot on first run).
#    Disconnect any other client from companion port 5050 first.
/opt/weather-bot/venv/bin/python /opt/weather-bot/weather_bot.py

# 5. Install units. If your login user is not 'pi', edit the User=
#    line in weather-bot.service first.
sudo cp weather-bot.service weather-bot.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now weather-bot.timer

# Verify
systemctl list-timers weather-bot.timer
journalctl -u weather-bot.service -f
