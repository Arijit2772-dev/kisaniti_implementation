"""Password hashing (scrypt, standard library) and the password policy (L0_ACCESS §5)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

R = 8
P = 1
DKLEN = 32
MAX_VERIFY_N = 2**18  # refuse absurd cost values read back from storage
_AMBIGUOUS_FREE = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"

# A deliberately short list of very common passwords of 10 or more characters. A real breached-password check
# needs an outside service and is future work (L0_ACCESS §5).
COMMON = frozenset(
    {
        "1234567890",
        "0123456789",
        "12345678910",
        "password123",
        "passw0rd123",
        "qwertyuiop",
        "qwerty12345",
        "1q2w3e4r5t",
        "welcome123",
        "letmein123",
        "changeme123",
        "administrator",
        "iloveyou123",
        "abcdefghij",
        "kisaniti123",
        "kisaniti@123",
        "punjab12345",
        "password1234",
        "test123456",
        "admin123456",
    }
)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _scrypt(password: str, salt: bytes, n: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=R, p=P, dklen=DKLEN, maxmem=512 * 1024 * 1024)


def hash_password(password: str, n: int) -> str:
    """`scrypt$N$r$p$salt$hash`; the parameters travel with the hash so they can be raised later."""
    salt = secrets.token_bytes(16)
    return f"scrypt${n}${R}${P}${_b64(salt)}${_b64(_scrypt(password, salt, n))}"


def verify_password(password: str, record: str) -> bool:
    try:
        scheme, n_s, r_s, p_s, salt_s, hash_s = record.split("$")
        n = int(n_s)
        if scheme != "scrypt" or int(r_s) != R or int(p_s) != P or n < 2 or n & (n - 1) or n > MAX_VERIFY_N:
            return False
        salt, expected = base64.b64decode(salt_s), base64.b64decode(hash_s)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(_scrypt(password, salt, n), expected)


_DUMMY: dict[int, str] = {}


def dummy_verify(password: str, n: int) -> None:
    """Do the same work as a real check, so an unknown username costs the same time as a wrong password."""
    if n not in _DUMMY:
        _DUMMY[n] = hash_password("not-a-real-password", n)
    verify_password(password, _DUMMY[n])


def generate_temporary_password() -> str:
    """16 characters from an alphabet without look-alike characters, in groups of four (shown once)."""
    chars = "".join(secrets.choice(_AMBIGUOUS_FREE) for _ in range(16))
    return "-".join(chars[i : i + 4] for i in range(0, 16, 4))


def password_problems(password: str, username: str) -> list[str]:
    """Reasons a new password is refused (empty list = acceptable). No composition rules; all Unicode accepted."""
    out: list[str] = []
    if len(password) < 10:
        out.append("TOO_SHORT")
    if len(password) > 256:
        out.append("TOO_LONG")
    low = password.lower()
    if username and username.lower() in low:
        out.append("CONTAINS_USERNAME")
    if password and len(set(password)) == 1:
        out.append("REPEATED_CHARACTER")
    if low in COMMON:
        out.append("COMMON_PASSWORD")
    return out
