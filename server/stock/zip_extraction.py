"""
ZIP parsing for admin-uploaded stock: extracts .session files and matches
each one to its JSON metadata file (<phone>.json) inside the same archive.

Expected zip structure:
  <phone>.session   — Telethon session file
  <phone>.json      — credentials: twoFA / password / app_id / app_hash

All other files inside the zip are silently ignored.
"""

import io
import json
import os
import zipfile

from server import LOGGER

_log = LOGGER(__name__)


def _is_safe_member(name: str) -> bool:
    """
    Reject zip entries that could escape the extraction context ("zip-slip"):
    absolute paths, drive letters, or any ".." path segment. We never extract
    to disk here (everything is read into memory via `zf.read`), but a
    malicious archive could still target `zf.read()` at arbitrary in-archive
    paths — this keeps every touched name confined to a relative, traversal-free
    path before we ever act on it.
    """
    if not name or name.startswith(("/", "\\")):
        return False
    normalized = name.replace("\\", "/")
    if ":" in normalized.split("/", 1)[0]:   # e.g. "C:" drive prefix
        return False
    return ".." not in normalized.split("/")


def read_json_credentials_from_zip(zf: zipfile.ZipFile, names: list[str]) -> dict[str, dict]:
    """Collect `<phone>.json` credential documents, keyed by digits-only phone."""
    json_map: dict[str, dict] = {}
    for name in names:
        if not _is_safe_member(name):
            _log.warning("Skipping unsafe zip entry (path traversal risk): %s", name)
            continue
        basename = os.path.basename(name)
        if not basename or not basename.lower().endswith(".json"):
            continue
        key = basename[:-5].lstrip("+")   # strip ".json" + normalize leading "+"
        try:
            raw_text = zf.read(name).decode("utf-8", errors="ignore")
            data = json.loads(raw_text)
        except Exception as exc:
            _log.warning("Failed to parse JSON %s: %s", name, exc)
            continue
        if isinstance(data, dict):
            json_map[key] = data
    return json_map


def match_session_credentials(basename: str, json_map: dict) -> dict:
    """
    Given one `<phone>.session` filename, resolve its phone number and
    any 2FA password / api_id / api_hash overrides from the matching JSON.

    JSON field priority for password: "twoFA" > "password".
    """
    raw_key = basename[:-8]         # strip ".session"
    key     = raw_key.lstrip("+")   # digits-only key for JSON lookup
    phone   = "+" + key

    json_data         = json_map.get(key)
    api_id_override   = None
    api_hash_override = None
    password          = ""

    if json_data is not None:
        # Scan all three known 2FA field names: "twoFA", "2FA", "password"
        # Priority: twoFA > 2FA > password  (matches real-world JSON diversity)
        two_fa    = str(json_data.get("twoFA") or json_data.get("2FA") or "").strip()
        pwd_field = str(json_data.get("password") or "").strip()
        password  = two_fa or pwd_field

        app_id_raw = json_data.get("app_id")
        if app_id_raw not in (None, ""):
            try:
                api_id_override = int(app_id_raw)
            except (TypeError, ValueError):
                api_id_override = None
        api_hash_override = (str(json_data.get("app_hash") or "").strip() or None)

    return {
        "phone":             phone,
        "password":          password,
        "has_json":          json_data is not None,
        "json_data":         json_data,
        "api_id_override":   api_id_override,
        "api_hash_override": api_hash_override,
    }


def extract_sessions_from_zip(zip_bytes: bytes) -> list[dict]:
    """
    Extracts all .session files from a zip archive.

    Only .session and .json files are read — everything else is ignored.
    Extension matching is case-insensitive; the phone-number portion of
    the filename is digits-only so case never matters there.

    Returns list of:
      {
        "filename":         "959660553440.session",
        "phone":            "+959660553440",
        "session_bytes":    b"...",
        "password":         "",          # 2FA password from JSON, or ""
        "has_json":         True,
        "json_data":        {...} | None,
        "api_id_override":  123456 | None,    # from JSON "app_id"
        "api_hash_override":"abc..." | None,  # from JSON "app_hash"
      }
    """
    results = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names    = zf.namelist()
        json_map = read_json_credentials_from_zip(zf, names)

        for name in names:
            if not _is_safe_member(name):
                _log.warning("Skipping unsafe zip entry (path traversal risk): %s", name)
                continue
            basename = os.path.basename(name)
            if not basename.lower().endswith(".session"):
                continue
            session_bytes = zf.read(name)
            match = match_session_credentials(basename, json_map)
            results.append({
                "filename":      basename,
                "session_bytes": session_bytes,
                **match,
            })

    return results
