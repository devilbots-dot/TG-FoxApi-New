"""
2FA password encryption + strong-password generation.

Shared by the stock pipeline (setting/rotating an account's 2FA password)
and the buyer-delivery flow (decrypting it to hand to the buyer).
"""

import os
import secrets as _secrets
import string

# ── Custom substitution cipher ────────────────────────────────────────────────
# A fully deterministic, key-free, reversible character-substitution cipher.
# Every supported character maps to a unique replacement; decryption applies
# the exact inverse mapping. Unsupported characters pass through unchanged.
#
# Mapping table (encrypt direction):
#   A-Z: A→Q B→M C→X D→L E→R F→A G→T H→P I→N J→Z K→W L→B M→Y
#        N→C O→F P→J Q→H R→K S→G T→E U→V V→S W→D X→I Y→O Z→U
#   a-z: same substitutions in lowercase
#   0-9: 0→7 1→4 2→9 3→1 4→8 5→0 6→3 7→6 8→2 9→5
#   special: !→@ @→# #→$ $→% %→^ ^→& &→* *→( (→) )→! _→- -→_ .→, ,→.

_SUBST_ENC: dict[str, str] = {
    # Uppercase
    "A": "Q", "B": "M", "C": "X", "D": "L", "E": "R", "F": "A", "G": "T",
    "H": "P", "I": "N", "J": "Z", "K": "W", "L": "B", "M": "Y", "N": "C",
    "O": "F", "P": "J", "Q": "H", "R": "K", "S": "G", "T": "E", "U": "V",
    "V": "S", "W": "D", "X": "I", "Y": "O", "Z": "U",
    # Lowercase
    "a": "q", "b": "m", "c": "x", "d": "l", "e": "r", "f": "a", "g": "t",
    "h": "p", "i": "n", "j": "z", "k": "w", "l": "b", "m": "y", "n": "c",
    "o": "f", "p": "j", "q": "h", "r": "k", "s": "g", "t": "e", "u": "v",
    "v": "s", "w": "d", "x": "i", "y": "o", "z": "u",
    # Digits
    "0": "7", "1": "4", "2": "9", "3": "1", "4": "8",
    "5": "0", "6": "3", "7": "6", "8": "2", "9": "5",
    # Special characters
    "!": "@", "@": "#", "#": "$", "$": "%", "%": "^", "^": "&",
    "&": "*", "*": "(", "(": ")", ")": "!", "_": "-", "-": "_",
    ".": ",", ",": ".",
}

# Reverse mapping — built automatically from _SUBST_ENC (guaranteed bijection)
_SUBST_DEC: dict[str, str] = {v: k for k, v in _SUBST_ENC.items()}


def subst_encrypt(text: str) -> str:
    """Encrypt text using the custom substitution cipher.

    Every character in _SUBST_ENC is substituted; others pass through unchanged.
    The operation is fully deterministic and requires no keys or external libs.
    """
    return "".join(_SUBST_ENC.get(ch, ch) for ch in text)


def subst_decrypt(text: str) -> str:
    """Decrypt text produced by subst_encrypt() using the reverse mapping.

    Guaranteed lossless round-trip: subst_decrypt(subst_encrypt(x)) == x
    for any string composed of supported characters.
    """
    return "".join(_SUBST_DEC.get(ch, ch) for ch in text)


def generate_strong_password(length: int = 6) -> str:
    """Generate a random password: exactly 2 letters + 4 digits (e.g. ab1234)."""
    letters = [_secrets.choice(string.ascii_lowercase) for _ in range(2)]
    digits  = [_secrets.choice(string.digits) for _ in range(4)]
    combined = letters + digits
    _secrets.SystemRandom().shuffle(combined)
    return "".join(combined)


def _get_fernet():
    """Return a Fernet instance keyed from SESSION_SECRET.

    SESSION_SECRET can be ANY non-empty string — we derive a valid 32-byte
    Fernet key from it using SHA-256 so the caller never has to worry about
    the raw value being exactly 44 url-safe base64 chars.

    Derivation: SHA-256(SESSION_SECRET) → 32 bytes → urlsafe_b64encode → Fernet key.
    This is deterministic: the same secret always produces the same key, so
    existing encrypted values continue to decrypt correctly after a restart.

    Raises RuntimeError only if SESSION_SECRET is completely absent.
    """
    import base64
    import hashlib
    from cryptography.fernet import Fernet

    raw = os.getenv("SESSION_SECRET", "").strip()
    if not raw:
        raise RuntimeError(
            "SESSION_SECRET is not set — cannot encrypt/decrypt 2FA passwords. "
            "Set this secret in Replit Secrets."
        )
    # Derive a stable 32-byte key from any passphrase string.
    derived_key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
    return Fernet(derived_key)


def encrypt_password(password: str) -> str:
    """Encrypt a 2FA password with Fernet (SESSION_SECRET).

    Raises RuntimeError if SESSION_SECRET is missing or not a valid Fernet key —
    callers should propagate or handle it; never silently drop the password.
    """
    if not password:
        return ""
    return _get_fernet().encrypt(password.encode()).decode()


def encrypt_bytes(data: bytes) -> bytes:
    """Encrypt arbitrary bytes (e.g. a .session file) with Fernet (SESSION_SECRET).

    Used by the pipeline state-store to checkpoint session bytes at rest in
    MongoDB. Raises RuntimeError if SESSION_SECRET is missing/invalid.
    """
    if not data:
        return b""
    return _get_fernet().encrypt(data)


def decrypt_bytes(token: bytes) -> bytes:
    """Decrypt Fernet-encrypted bytes produced by encrypt_bytes(). Fails
    loudly (InvalidToken propagates) — unlike decrypt_password there is no
    legacy-plaintext fallback, since this format was never plaintext."""
    if not token:
        return b""
    if isinstance(token, str):
        token = token.encode()
    return _get_fernet().decrypt(token)


def decrypt_password(token: str) -> str:
    """Decrypt a Fernet-encrypted 2FA password.

    Raises RuntimeError if SESSION_SECRET is missing or invalid (fail closed).
    Falls back to returning the token as-is only when decryption fails on a
    valid key — this covers legacy plaintext records stored before encryption
    was enabled; remove this fallback once those records are migrated.
    """
    if not token:
        return ""
    fernet = _get_fernet()  # raises loudly on key misconfiguration
    try:
        from cryptography.fernet import InvalidToken
        return fernet.decrypt(token.encode()).decode()
    except (InvalidToken, Exception):
        # Value is not a Fernet token — treat as legacy plaintext
        return token
