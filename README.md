# weather

MeshCore Station G3 weather application for the Raspberry Pi Zero 2W.

Broadcasts a compact 3-day forecast for Marion, OH (43302) to the
`#weather` hashtag channel once an hour, via an
[openHop Repeater](https://github.com/openhop-dev/openhop_repeater)
companion identity on the same Pi. Weather data comes from the National
Weather Service API (no API key required).

Example transmission (one LoRa packet, ~90 bytes):

```
WX Marion OH · Sun 84°/61° Sunny · Mon 79°/58° SctT-storms · Tue 74°/51° MCloudy
```

On first run the script provisions the `#weather` channel in a free slot
if the companion doesn't have it. Hashtag channel keys are derived from
the channel name, so the created channel automatically interoperates
with every other companion's `#weather`.

## Requirements

- openHop Repeater running with a companion identity on TCP port 5050
- Python 3 with `venv` (stock on Raspberry Pi OS Bookworm)
- git

## Install

Run on the Pi as your login user.

**1. Clone the repo to `/opt/weather`:**

```bash
sudo git clone https://github.com/pitbullcoder/weather.git /opt/weather
sudo chown -R $USER /opt/weather
```

**2. Create the venv and install the dependency** (Bookworm blocks
system-wide pip):

```bash
python3 -m venv /opt/weather/venv
/opt/weather/venv/bin/pip install meshcore
```

**3. Test without transmitting** (checks NWS fetch and formatting):

```bash
/opt/weather/venv/bin/python /opt/weather/weather_bot.py --dry-run
```

**4. One real send.** Creates the `#weather` channel slot on first run.
Disconnect any other client (meshcore-cli, phone app) from companion
port 5050 first:

```bash
/opt/weather/venv/bin/python /opt/weather/weather_bot.py
```

**5. Install the systemd units.** If your login user is not `pi`, edit
the `User=` line in `weather-bot.service` first:

```bash
sudo cp /opt/weather/weather-bot.service /opt/weather/weather-bot.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now weather-bot.timer
```

## Verify

```bash
systemctl list-timers weather-bot.timer
journalctl -u weather-bot.service -f
```

The timer fires hourly with up to 2 minutes of jitter.

## Update

```bash
cd /opt/weather
git pull
```

If the systemd units changed, re-copy them and reload:

```bash
sudo cp weather-bot.service weather-bot.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

## Configuration

Location, companion host/port, channel name, and message budget are
constants at the top of `weather_bot.py`. To adapt this for another
location, change `LATITUDE`, `LONGITUDE`, and `LOCATION_TAG` (NWS
covers US locations only).

## License

GPL-3.0 — see [LICENSE](LICENSE).
