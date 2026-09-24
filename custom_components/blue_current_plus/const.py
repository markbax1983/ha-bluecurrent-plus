"""Constants for Blue Current Plus."""

from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

DOMAIN = "blue_current_plus"
LOGGER = logging.getLogger(__package__)

URL = "wss://motown.bluecurrent.nl/haserver"
BC_TZ = ZoneInfo("Europe/Amsterdam")

# Charge card id the backend uses for "start without a card" (app session).
NO_CARD_UID = "BCU-APP"

# Minimum current the IEC 61851 spec allows a car to charge with.
MIN_CURRENT = 6
DEFAULT_MAX_CURRENT = 32

# Reconnect backoff (seconds).
BACKOFF_START = 10
BACKOFF_MAX = 600

# Coalesce rapid override changes (e.g. a slider being dragged, or an automation
# firing every few seconds) into one request, to protect the daily request limit.
OVERRIDE_DEBOUNCE = 5.0

# Number of unknown / raw messages kept for diagnostics.
RAW_BUFFER_SIZE = 25

STORAGE_VERSION = 1

VEHICLE_STATUS = {
    "A": "standby",
    "B": "vehicle_detected",
    "C": "ready",
    "D": "ready",
    "E": "no_power",
    "F": "vehicle_error",
}
VEHICLE_STATUS_OPTIONS = sorted(set(VEHICLE_STATUS.values()))

ACTIVITY_OPTIONS = [
    "available",
    "charging",
    "delayed",
    "error",
    "offline",
    "unavailable",
]

DAY_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
