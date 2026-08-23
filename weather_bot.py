#!/usr/bin/env python3
"""weather_bot.py -- hourly 3-day forecast broadcast to the #weather channel.

Runs as a oneshot (systemd timer fires it hourly):
  1. Fetch the NWS forecast for Marion, OH (43302).
  2. Compact it into a single <=140-byte channel message.
  3. Connect to the local openHop companion over TCP, ensure the
     #weather hashtag channel exists (creating it in a free slot if
     needed -- the key is auto-derived from the name, so it matches
     every other companion's #weather), send, disconnect, exit.

Usage:
  weather_bot.py            fetch + transmit
  weather_bot.py --dry-run  fetch + print the message, no radio
"""

import asyncio
import json
import logging
import sys
import urllib.request
from datetime import datetime

from meshcore import EventType, MeshCore

# --- Configuration ---------------------------------------------------------

COMPANION_HOST = "127.0.0.1"
COMPANION_PORT = 5050
CHANNEL_NAME = "#weather"
NUM_CHANNEL_SLOTS = 8          # firmware channel slots 0-7
PUBLIC_CHANNEL_IDX = 0         # never provision over the public channel

# Marion, OH (ZIP 43302)
LATITUDE = 40.5887
LONGITUDE = -83.1264
LOCATION_TAG = "Marion OH"

NWS_POINTS_URL = f"https://api.weather.gov/points/{LATITUDE},{LONGITUDE}"
HTTP_TIMEOUT = 20              # seconds, per request
RUN_TIMEOUT = 90               # seconds, whole mesh phase
MAX_MSG_BYTES = 140            # single-LoRa-packet text budget

USER_AGENT = "stationg3-weather-bot (mesh node, Marion OH)"

# shortForecast -> compact form. Checked in order; first match wins.
CONDITION_ABBREVIATIONS = [
    ("Thunderstorms", "T-storms"),
    ("Thunderstorm", "T-storm"),
    ("Rain Showers", "Showers"),
    ("Snow Showers", "SnwShwrs"),
    ("Freezing Rain", "FrzRain"),
    ("Partly Cloudy", "PtCloudy"),
    ("Mostly Cloudy", "MCloudy"),
    ("Partly Sunny", "PtSunny"),
    ("Mostly Sunny", "MSunny"),
    ("Mostly Clear", "MClear"),
    ("Scattered", "Sct"),
    ("Isolated", "Iso"),
    ("Slight Chance", "Sl.Chc"),
    ("Chance", "Chc"),
    ("Likely", "Lkly"),
    ("Light", "Lt"),
    ("Heavy", "Hvy"),
]

log = logging.getLogger("weather_bot")


# --- Weather ---------------------------------------------------------------

def http_get_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
        return json.load(response)


def fetch_forecast_periods():
    """Resolve the NWS gridpoint, then return the forecast period list."""
    points = http_get_json(NWS_POINTS_URL)
    forecast_url = points["properties"]["forecast"]
    forecast = http_get_json(forecast_url)
    return forecast["properties"]["periods"]


def abbreviate_condition(short_forecast):
    """Compact an NWS shortForecast string ('Chance Rain Showers then ...')."""
    condition = short_forecast.split(" then ")[0].strip()
    for full, brief in CONDITION_ABBREVIATIONS:
        condition = condition.replace(full, brief)
    condition = condition.replace(" and ", "/").replace(" ", "")
    return condition[:12]


def build_days(periods):
    """Pair NWS day/night periods into [{label, high, low, cond}, ...].

    NWS alternates daytime and overnight periods.  A daytime period
    carries the high and the headline condition; the overnight period
    that follows carries the low.  A run that starts at night simply
    begins with the next full day.
    """
    days = []
    current = None
    for period in periods:
        if period["isDaytime"]:
            if current is not None:
                days.append(current)
            start = datetime.fromisoformat(period["startTime"])
            current = {
                "label": start.strftime("%a"),
                "high": period["temperature"],
                "low": None,
                "cond": abbreviate_condition(period["shortForecast"]),
            }
        elif current is not None and current["low"] is None:
            current["low"] = period["temperature"]
    if current is not None:
        days.append(current)
    return days[:3]


def format_day(day):
    low = "?" if day["low"] is None else day["low"]
    return f"{day['label']} {day['high']}\u00b0/{low}\u00b0 {day['cond']}"


def build_message(days):
    if not days:
        raise ValueError("no forecast days parsed from NWS response")
    message = f"WX {LOCATION_TAG}\n" + "\n".join(
        format_day(day) for day in days
    )
    # Stay inside one LoRa packet: degrade gracefully, never fail to send.
    if len(message.encode("utf-8")) > MAX_MSG_BYTES:
        message = message.replace("\u00b0", "")
    while len(message.encode("utf-8")) > MAX_MSG_BYTES:
        message = message[:-1]
    return message


# --- Mesh ------------------------------------------------------------------

async def ensure_channel(meshcore):
    """Return the slot index of CHANNEL_NAME, creating it if absent."""
    free_slot = None
    for idx in range(NUM_CHANNEL_SLOTS):
        result = await meshcore.commands.get_channel(idx)
        if result.type == EventType.ERROR:
            log.warning("get_channel(%d) failed: %s", idx, result.payload)
            continue
        name = (result.payload.get("channel_name") or "").strip("\x00")
        if name == CHANNEL_NAME:
            log.info("Found %s in slot %d", CHANNEL_NAME, idx)
            return idx
        if not name and free_slot is None and idx != PUBLIC_CHANNEL_IDX:
            free_slot = idx
    if free_slot is None:
        raise RuntimeError(
            f"{CHANNEL_NAME} not configured and no free channel slot available"
        )
    log.info("Creating %s in free slot %d", CHANNEL_NAME, free_slot)
    # Hashtag name => library derives the shared key (sha256(name)[:16]),
    # identical to every other companion's #weather channel.
    result = await meshcore.commands.set_channel(free_slot, CHANNEL_NAME)
    if result.type == EventType.ERROR:
        raise RuntimeError(f"set_channel failed: {result.payload}")
    return free_slot


async def send_to_mesh(message):
    meshcore = await MeshCore.create_tcp(COMPANION_HOST, COMPANION_PORT)
    try:
        channel_idx = await ensure_channel(meshcore)
        result = await meshcore.commands.send_chan_msg(channel_idx, message)
        if result.type == EventType.ERROR:
            raise RuntimeError(f"send_chan_msg failed: {result.payload}")
        log.info("Sent to %s (slot %d): %s", CHANNEL_NAME, channel_idx, message)
    finally:
        await meshcore.disconnect()


# --- Entry point -----------------------------------------------------------

def main(argv):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dry_run = "--dry-run" in argv

    try:
        periods = fetch_forecast_periods()
        message = build_message(build_days(periods))
    except Exception:
        log.exception("Weather fetch/format failed; will retry next timer run")
        return 1

    if dry_run:
        print(message)
        print(f"({len(message.encode('utf-8'))} bytes)")
        return 0

    try:
        asyncio.run(asyncio.wait_for(send_to_mesh(message), RUN_TIMEOUT))
    except Exception:
        log.exception("Mesh send failed; will retry next timer run")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
