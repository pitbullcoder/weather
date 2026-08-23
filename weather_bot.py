#!/usr/bin/env python3
"""weather_bot.py -- #weather channel broadcasts for the Station G3 node.

Two modes, each driven by its own systemd timer:

  forecast (default, every 4 hours)
    1. Fetch the NWS forecast for Marion, OH (43302).
    2. Compact it into a single <=140-byte channel message.
    3. Connect to the local openHop companion over TCP, ensure the
       #weather hashtag channel exists (creating it in a free slot if
       needed -- the key is auto-derived from the name, so it matches
       every other companion's #weather), send, disconnect, exit.

  alerts (--alerts, every 5 minutes)
    1. Fetch active NWS alerts for the same point.
    2. Keep only Severe/Extreme ones we have not already broadcast.
    3. Send each as its own SEVERE WX ALERT message on the same channel.

Alert IDs already sent are recorded in a state file so a warning that
stays active for hours is transmitted once, not once per timer tick.
NWS mints a new ID when it reissues or upgrades an alert, so genuine
updates do go out again.

Usage:
  weather_bot.py                      fetch + transmit forecast
  weather_bot.py --dry-run            print the forecast, no radio
  weather_bot.py --alerts             check + transmit new alerts
  weather_bot.py --alerts --dry-run   print pending alerts, no radio
  weather_bot.py --alerts --force     ignore the sent-state (testing)
"""

import asyncio
import json
import logging
import os
import sys
import urllib.request
from datetime import datetime, timezone

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
NWS_ALERTS_URL = (
    f"https://api.weather.gov/alerts/active?point={LATITUDE},{LONGITUDE}"
)
HTTP_TIMEOUT = 20              # seconds, per request
RUN_TIMEOUT = 90               # seconds, whole mesh phase
MAX_MSG_BYTES = 140            # single-LoRa-packet text budget

# --- Alerts ---
ALERT_PREFIX = "** SEVERE WX ALERT **"

# NWS severity values: Extreme, Severe, Moderate, Minor, Unknown.
# Widen this to include "Moderate" if you also want advisories and
# most watches -- expect several times the traffic.
ALERT_SEVERITIES = {"Extreme", "Severe"}

# Airtime guard: if a squall line lights up five products at once we
# still only key up this many times, worst first.
MAX_ALERTS_PER_RUN = 2
INTER_ALERT_DELAY = 8          # seconds between consecutive alert sends

ALERT_SEVERITY_RANK = {"Extreme": 0, "Severe": 1, "Moderate": 2,
                       "Minor": 3, "Unknown": 4}

# Trim the wordiest NWS product names to protect the byte budget.
ALERT_EVENT_ABBREVIATIONS = [
    ("Thunderstorm", "Tstm"),
    ("Special Marine", "Marine"),
    ("Extreme Cold", "Ext Cold"),
    ("Excessive Heat", "Exc Heat"),
    ("Winter Storm", "Wntr Storm"),
    ("Winter Weather", "Wntr Wx"),
    ("Warning", "Wrn"),
    ("Watch", "Wtch"),
    ("Advisory", "Advis"),
    ("Statement", "Stmt"),
]

# systemd sets STATE_DIRECTORY when the unit declares StateDirectory=.
STATE_DIR = os.environ.get("STATE_DIRECTORY") or "/var/lib/weather-bot"
SENT_ALERTS_PATH = os.path.join(STATE_DIR, "sent_alerts.json")

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


# --- Alerts ----------------------------------------------------------------

def fetch_active_alerts():
    """Return the raw active-alert feature list for our point."""
    return http_get_json(NWS_ALERTS_URL).get("features", [])


def is_broadcastable(alert):
    """True if this alert is a real, current, severe-enough product.

    'Actual' filters out the Test/Exercise/Draft products NWS pushes
    through the same feed.  'Cancel' and 'Ack' carry no new hazard.
    """
    props = alert.get("properties", {})
    if props.get("status") != "Actual":
        return False
    if props.get("messageType") in ("Cancel", "Ack"):
        return False
    return props.get("severity") in ALERT_SEVERITIES


def alert_sort_key(alert):
    props = alert.get("properties", {})
    return (
        ALERT_SEVERITY_RANK.get(props.get("severity"), 9),
        props.get("onset") or props.get("effective") or "",
    )


def parse_iso(value):
    """Parse an NWS timestamp, or None. NWS emits an explicit offset."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        log.warning("Unparseable timestamp: %s", value)
        return None


def format_until(alert):
    """Compact local end time, e.g. '3:45pm'. Empty if unknown."""
    props = alert.get("properties", {})
    ends = parse_iso(props.get("ends") or props.get("expires"))
    if ends is None:
        return ""
    # The offset in the NWS field is already the alert's local zone,
    # so no conversion is needed -- just drop the tzinfo and format.
    return ends.strftime("%-I:%M%p").lower()


def abbreviate_event(event):
    for full, brief in ALERT_EVENT_ABBREVIATIONS:
        event = event.replace(full, brief)
    return event


def build_alert_message(alert):
    props = alert.get("properties", {})
    event = abbreviate_event((props.get("event") or "Weather Alert").strip())
    lines = [ALERT_PREFIX, event, LOCATION_TAG]
    until = format_until(alert)
    if until:
        lines.append(f"til {until}")
    message = "\n".join(lines)
    while len(message.encode("utf-8")) > MAX_MSG_BYTES:
        message = message[:-1]
    return message


def load_sent_alerts():
    """Return {alert_id: expiry_iso} of alerts already broadcast."""
    try:
        with open(SENT_ALERTS_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # A corrupt state file must not wedge the bot.  Worst case we
        # re-send an alert once, which beats going silent.
        log.warning("Could not read %s; starting fresh", SENT_ALERTS_PATH)
        return {}


def prune_sent_alerts(sent):
    """Drop entries whose alert has already expired."""
    now = datetime.now(timezone.utc)
    kept = {}
    for alert_id, expiry in sent.items():
        parsed = parse_iso(expiry)
        if parsed is None or parsed > now:
            kept[alert_id] = expiry
    return kept


def save_sent_alerts(sent):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp_path = SENT_ALERTS_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(sent, handle)
        os.replace(tmp_path, SENT_ALERTS_PATH)
    except OSError:
        log.exception("Could not persist %s", SENT_ALERTS_PATH)


def select_new_alerts(alerts, sent, force=False):
    """Severe, unsent alerts, worst first, capped for airtime."""
    pending = [a for a in alerts if is_broadcastable(a)]
    if not force:
        pending = [
            a for a in pending
            if a.get("properties", {}).get("id") not in sent
        ]
    pending.sort(key=alert_sort_key)
    if len(pending) > MAX_ALERTS_PER_RUN:
        log.warning(
            "%d alerts pending; sending %d this run",
            len(pending), MAX_ALERTS_PER_RUN,
        )
    return pending[:MAX_ALERTS_PER_RUN]


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


async def send_to_mesh(messages):
    """Send one or more messages over a single companion connection.

    Returns the list of indices that went out.  A partial failure is
    reported rather than raised so an alert run can still record the
    alerts that did transmit.
    """
    if isinstance(messages, str):
        messages = [messages]
    sent = []
    meshcore = await MeshCore.create_tcp(COMPANION_HOST, COMPANION_PORT)
    try:
        channel_idx = await ensure_channel(meshcore)
        for position, message in enumerate(messages):
            if position:
                # Pace consecutive packets; back-to-back sends on a
                # shared channel step on each other's airtime.
                await asyncio.sleep(INTER_ALERT_DELAY)
            result = await meshcore.commands.send_chan_msg(channel_idx, message)
            if result.type == EventType.ERROR:
                log.error("send_chan_msg failed: %s", result.payload)
                continue
            sent.append(position)
            log.info("Sent to %s (slot %d): %s",
                     CHANNEL_NAME, channel_idx, message)
    finally:
        await meshcore.disconnect()
    return sent


# --- Entry point -----------------------------------------------------------

def preview(messages):
    for message in messages:
        print(message)
        print(f"({len(message.encode('utf-8'))} bytes)\n")


def run_forecast(dry_run):
    try:
        periods = fetch_forecast_periods()
        message = build_message(build_days(periods))
    except Exception:
        log.exception("Weather fetch/format failed; will retry next timer run")
        return 1

    if dry_run:
        preview([message])
        return 0

    try:
        asyncio.run(asyncio.wait_for(send_to_mesh([message]), RUN_TIMEOUT))
    except Exception:
        log.exception("Mesh send failed; will retry next timer run")
        return 1
    return 0


def run_alerts(dry_run, force):
    try:
        alerts = fetch_active_alerts()
    except Exception:
        log.exception("Alert fetch failed; will retry next timer run")
        return 1

    sent = prune_sent_alerts(load_sent_alerts())
    pending = select_new_alerts(alerts, sent, force=force)

    if not pending:
        log.info("No new severe alerts (%d active products)", len(alerts))
        if not dry_run:
            save_sent_alerts(sent)
        return 0

    messages = [build_alert_message(alert) for alert in pending]

    if dry_run:
        preview(messages)
        return 0

    # Timeout scales with the pacing delay between alert sends.
    timeout = RUN_TIMEOUT + INTER_ALERT_DELAY * (len(messages) - 1)
    try:
        delivered = asyncio.run(
            asyncio.wait_for(send_to_mesh(messages), timeout)
        )
    except Exception:
        log.exception("Alert send failed; will retry next timer run")
        return 1

    # Only record what actually went out, so a failed send is retried.
    for position in delivered:
        props = pending[position].get("properties", {})
        alert_id = props.get("id")
        if alert_id:
            sent[alert_id] = props.get("expires") or props.get("ends") or ""
    save_sent_alerts(sent)

    return 0 if len(delivered) == len(messages) else 1


def main(argv):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dry_run = "--dry-run" in argv

    if "--alerts" in argv:
        return run_alerts(dry_run, force="--force" in argv)
    return run_forecast(dry_run)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
