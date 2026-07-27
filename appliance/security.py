"""Authentication, session handling, and abuse resistance.

The appliance ships with no default credentials.  The installer generates the
initial administrator password, prints it exactly once, and stores only a
salted key derivation of it.  Sessions are opaque random tokens held server
side; the cookie carries no claim the server has not itself issued.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .logging_setup import get_logger

__all__ = [
    "PasswordHasher",
    "Session",
    "SessionStore",
    "LoginThrottle",
    "CredentialStore",
    "AuthenticationError",
    "generate_password",
]

_LOG = get_logger("security")

_ALGORITHM = "pbkdf2_sha256"
_SALT_BYTES = 16
_TOKEN_BYTES = 32

# Ambiguous characters are excluded so that a generated password can be read
# off a console and typed correctly on the first attempt.
_PASSWORD_ALPHABET = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


class AuthenticationError(Exception):
    """Raised when a credential cannot be accepted."""


def generate_password(length: int = 20) -> str:
    """Generate a high entropy password from an unambiguous alphabet."""
    if length < 12:
        raise ValueError("a generated password must be at least twelve characters")
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


class PasswordHasher:
    """Salted key derivation with a configurable iteration count."""

    def __init__(self, iterations: int = 240000) -> None:
        if iterations < 100000:
            raise ValueError("the iteration count is too low to ship")
        self.iterations = iterations

    def hash(self, password: str, salt: bytes | None = None) -> str:
        if not password:
            raise ValueError("an empty password cannot be hashed")
        salt = salt if salt is not None else secrets.token_bytes(_SALT_BYTES)
        derived = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, self.iterations
        )
        return f"{_ALGORITHM}${self.iterations}${salt.hex()}${derived.hex()}"

    def verify(self, password: str, encoded: str) -> bool:
        """Verify a password in constant time with respect to the digest.

        A malformed stored value is treated as a failed verification rather
        than an exception, so that a corrupted credential file degrades to
        "nobody can log in" rather than to an unhandled crash on every attempt.
        """
        try:
            algorithm, iteration_text, salt_hex, digest_hex = encoded.split("$")
            if algorithm != _ALGORITHM:
                return False
            iterations = int(iteration_text)
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(digest_hex)
        except (AttributeError, ValueError):
            _LOG.warning("a stored credential is malformed and cannot be verified")
            return False

        candidate = hashlib.pbkdf2_hmac(
            "sha256", (password or "").encode("utf-8"), salt, iterations
        )
        return hmac.compare_digest(candidate, expected)

    def needs_rehash(self, encoded: str) -> bool:
        """Report whether a stored credential predates the current policy."""
        try:
            algorithm, iteration_text, _, _ = encoded.split("$")
        except (AttributeError, ValueError):
            return True
        return algorithm != _ALGORITHM or int(iteration_text) < self.iterations


@dataclass
class Session:
    """A server side session record.  The token itself is never stored here."""

    identifier: str
    username: str
    created_at: float
    last_seen_at: float
    source_address: str

    def age_seconds(self, now: float) -> int:
        return int(now - self.created_at)

    def idle_seconds(self, now: float) -> int:
        return int(now - self.last_seen_at)


class SessionStore:
    """Opaque token sessions with idle expiry and a hard population ceiling.

    Tokens are indexed by their digest rather than by their plaintext, so a
    disclosure of process memory or of a state dump does not hand an attacker a
    usable token.
    """

    def __init__(
        self,
        idle_seconds: int = 1800,
        maximum: int = 64,
        clock: Callable[[], float] | None = None,
        lifetime_seconds: int = 43200,
    ) -> None:
        if idle_seconds < 60:
            raise ValueError("the idle expiry must be at least sixty seconds")
        if maximum < 1:
            raise ValueError("at least one session must be permitted")
        if lifetime_seconds < idle_seconds:
            raise ValueError(
                "the absolute lifetime cannot be shorter than the idle expiry"
            )
        self.idle_seconds = idle_seconds
        #: How long a session may live, however busy it is.
        #:
        #: Idle expiry alone never ends a session that keeps being used. A
        #: console left open on a wall display, or a token an attacker holds
        #: and polls, renews itself every time it is touched and lives until
        #: the appliance restarts. Twelve hours is longer than a working day
        #: and shorter than a week of one, so an operator signs in each
        #: morning and a stolen token stops working by the next.
        self.lifetime_seconds = lifetime_seconds
        self.maximum = maximum
        self._clock = clock or time.monotonic
        self._sessions: dict[str, Session] = {}

    @staticmethod
    def _index(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def create(self, username: str, source_address: str = "unknown") -> str:
        now = self._clock()
        self.purge_expired()

        if len(self._sessions) >= self.maximum:
            # Evict the least recently used session rather than refusing the
            # login, so that a forgotten browser tab cannot lock out an
            # administrator during an incident.
            oldest = min(self._sessions.items(), key=lambda item: item[1].last_seen_at)
            del self._sessions[oldest[0]]
            _LOG.info("evicted the least recently used session to admit a new sign in")

        token = secrets.token_urlsafe(_TOKEN_BYTES)
        index = self._index(token)
        self._sessions[index] = Session(
            identifier=index[:16],
            username=username,
            created_at=now,
            last_seen_at=now,
            source_address=source_address,
        )
        return token

    def validate(self, token: str | None, touch: bool = True) -> Session | None:
        """Return the live session for a token, or nothing at all."""
        if not token:
            return None
        session = self._sessions.get(self._index(token))
        if session is None:
            return None

        now = self._clock()
        if now - session.last_seen_at > self.idle_seconds:
            self._sessions.pop(self._index(token), None)
            return None
        # And a ceiling that being busy cannot lift. Touching a session moves
        # its idle clock but not the moment it was created.
        if now - session.created_at > self.lifetime_seconds:
            self._sessions.pop(self._index(token), None)
            _LOG.info(
                "a session reached its absolute lifetime and was ended; "
                "the account named %s must sign in again", session.username,
            )
            return None
        if touch:
            session.last_seen_at = now
        return session

    def revoke(self, token: str | None) -> bool:
        if not token:
            return False
        return self._sessions.pop(self._index(token), None) is not None

    def revoke_all(self) -> int:
        count = len(self._sessions)
        self._sessions.clear()
        return count

    def purge_expired(self) -> int:
        now = self._clock()
        stale = [
            index
            for index, session in self._sessions.items()
            if now - session.last_seen_at > self.idle_seconds
            or now - session.created_at > self.lifetime_seconds
        ]
        for index in stale:
            del self._sessions[index]
        return len(stale)

    def active(self) -> Iterable[Session]:
        self.purge_expired()
        return tuple(self._sessions.values())

    def __len__(self) -> int:
        return len(self._sessions)


class LoginThrottle:
    """Per source address attempt limiting with a timed lockout."""

    def __init__(
        self,
        attempt_limit: int = 5,
        lockout_seconds: int = 300,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if attempt_limit < 1:
            raise ValueError("at least one attempt must be permitted")
        self.attempt_limit = attempt_limit
        self.lockout_seconds = lockout_seconds
        self._clock = clock or time.monotonic
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def is_locked(self, source_address: str) -> bool:
        until = self._locked_until.get(source_address)
        if until is None:
            return False
        if self._clock() >= until:
            self._locked_until.pop(source_address, None)
            self._failures.pop(source_address, None)
            return False
        return True

    def remaining_lockout_seconds(self, source_address: str) -> int:
        until = self._locked_until.get(source_address)
        if until is None:
            return 0
        return max(0, int(until - self._clock()))

    def record_failure(self, source_address: str) -> bool:
        """Record a failed attempt and report whether the source is now locked."""
        now = self._clock()
        window = self._failures.setdefault(source_address, [])
        window.append(now)
        # Only failures inside the lockout window count toward the limit.
        cutoff = now - self.lockout_seconds
        window[:] = [moment for moment in window if moment >= cutoff]

        if len(window) >= self.attempt_limit:
            self._locked_until[source_address] = now + self.lockout_seconds
            _LOG.warning(
                "the source address %s reached the failed sign in limit and is locked out",
                source_address,
            )
            return True
        return False

    def record_success(self, source_address: str) -> None:
        self._failures.pop(source_address, None)
        self._locked_until.pop(source_address, None)


class CredentialStore:
    """The administrator credential, persisted as a derivation only."""

    def __init__(self, path: str | Path, hasher: PasswordHasher | None = None) -> None:
        self.path = Path(path)
        self.hasher = hasher or PasswordHasher()

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> dict[str, str]:
        if not self.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            _LOG.error("the credential store could not be read: %s", error)
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, username: str, password: str) -> None:
        """Write the credential atomically with owner only permissions."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"username": username, "credential": self.hasher.hash(password)},
            indent=2,
        )
        temporary = self.path.with_name(self.path.name + ".partial")
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)

    def verify(self, username: str, password: str) -> bool:
        record = self.load()
        stored_user = record.get("username")
        stored_credential = record.get("credential")
        if not stored_user or not stored_credential:
            # No credential configured: reject, but still spend the time a
            # real verification would, so that the absence is not detectable
            # by timing.
            self.hasher.verify(password, self.hasher.hash("placeholder"))
            return False

        user_matches = hmac.compare_digest(stored_user, username or "")
        credential_matches = self.hasher.verify(password, stored_credential)
        return user_matches and credential_matches
