"""
Detect a stock account's country from its phone number — used to pick the
right proxy for its Telethon connection and to categorize it once stored.
"""

import asyncio
import phonenumbers
from phonenumbers import geocoder, carrier

from server import LOGGER

_log = LOGGER(__name__)


async def detect_country(number: str) -> dict:
    """
    Detects country from a phone number.
    Accepts with or without '+'.
    Returns dict with iso_code, country_name, is_valid, e164_format.
    """
    raw = number.strip().replace(" ", "").replace("-", "")

    # Auto-add '+' if missing
    if not raw.startswith("+"):
        raw = "+" + raw

    try:
        parsed = phonenumbers.parse(raw, None)
    except phonenumbers.NumberParseException as e:
        _log.warning("detect_country: failed to parse %s — %s", number, e)
        return {
            "success": False,
            "error": str(e),
            "input": number,
            "iso_code": "XX",
            "country_name": "Unknown",
        }

    is_valid = phonenumbers.is_valid_number(parsed)
    is_possible = phonenumbers.is_possible_number(parsed)
    iso_code = phonenumbers.region_code_for_number(parsed) or "XX"
    country_name = geocoder.description_for_number(parsed, "en") or "Unknown"
    carrier_name = carrier.name_for_number(parsed, "en")
    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)

    return {
        "success": True,
        "input": number,
        "e164_format": e164,
        "iso_code": iso_code,
        "country_name": country_name,
        "carrier": carrier_name or None,
        "is_valid": is_valid,
        "is_possible": is_possible,
        "country_calling_code": parsed.country_code,
    }
