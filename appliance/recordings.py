"""Recorded calls: listing them, and serving one.

The engine writes the audio; this reads the directory it writes into. There is
deliberately no database beside it. Everything a listing needs -- when the call
was, who it was from, who it was to, and the engine's own identifier for it --
is in the file's name, so a row in a table and a file on disk cannot come to
disagree, which is exactly what a second record of the same thing eventually
does.

The listing is bounded and the serving is bounded, and the second of those is
the one that matters. This is the only route in the appliance that hands a file
off the disk to a browser, so it is the only route where a name arriving from
outside could be made to name a file somebody was never meant to have. Two
independent rules stop that, and both have to hold:

- a name is served only if it matches the pattern the appliance itself writes,
  which admits no separator and no dot beyond the extension; and
- the resolved path, after every symbolic link has been followed, must still
  sit inside the recordings directory.

The second alone would be enough on a well behaved filesystem. The first alone
would be enough against a naive traversal. Neither is trusted on its own.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from . import numerals
from .logging_setup import get_logger

__all__ = ["RecordingStore", "RECORDING_PATTERN"]

_LOG = get_logger("recordings")

#: What the appliance names a recording, and therefore the only shape it will
#: serve back. Anything else on that directory -- put there by hand, left by
#: another tool, or arriving in a request -- is not served.
RECORDING_PATTERN = re.compile(
    r"^(?P<stamp>\d{8}-\d{6})"
    r"_(?P<source>[0-9+#*]{0,24})"
    r"_(?P<destination>[0-9+#*]{0,24})"
    r"_(?P<identifier>[0-9]+\.[0-9]+)"
    r"\.(?P<extension>wav|WAV|gsm|ulaw|alaw)$"
)

#: What each extension is, for a browser that has to decide how to play it.
_MEDIA_TYPES = {
    "wav": "audio/wav",
    "gsm": "audio/x-gsm",
    "ulaw": "audio/basic",
    "alaw": "audio/x-alaw-basic",
}

#: The most recordings one listing will describe.
LISTING_LIMIT = 500

#: The largest single recording that will be served in one response. An hour of
#: uncompressed telephone audio is about eight and a half mebibytes, so this
#: covers a very long call and refuses a file that is not one.
MAXIMUM_BYTES = 64 * 1024 * 1024


class RecordingStore:
    """Lists recorded calls, and serves one by name."""

    def __init__(
        self,
        directory: str | Path = "/var/spool/asterisk/monitor",
        root: str | Path = "/",
    ) -> None:
        self.root = Path(root)
        self.directory = self.root / str(directory).lstrip("/")

    def available(self) -> bool:
        return self.directory.is_dir()

    # -- listing -----------------------------------------------------------

    def describe(self, name: str, size: int) -> dict[str, Any] | None:
        """One recording, from its name alone."""
        match = RECORDING_PATTERN.match(name)
        if match is None:
            return None
        try:
            moment = datetime.strptime(match.group("stamp"), "%Y%m%d-%H%M%S")
        except ValueError:
            return None
        return {
            "name": name,
            "at": moment.strftime("%Y-%m-%d %H:%M:%S"),
            "source": match.group("source"),
            "destination": match.group("destination"),
            "identifier": match.group("identifier"),
            "size": numerals.spell_integer(max(0, size // 1024)) + " kibibytes",
            "media_type": _MEDIA_TYPES.get(match.group("extension").lower(), "audio/wav"),
        }

    def list(self, since: str = "", until: str = "", search: str = "") -> dict[str, Any]:
        """Every recording inside a window, newest first."""
        if not self.available():
            return {
                "available": False,
                "records": [],
                "record_count": "zero",
                "explanation": (
                    "no call has been recorded on this machine yet; recording is "
                    "off on every extension until it is turned on, and in most "
                    "places a caller must be told before it is"
                ),
            }

        needle = (search or "").strip().lower()
        found: list[dict[str, Any]] = []
        skipped = 0

        try:
            entries = sorted(
                (entry for entry in self.directory.iterdir() if entry.is_file()),
                key=lambda entry: entry.name,
                reverse=True,
            )
        except OSError as error:
            _LOG.error("the recordings could not be listed: %s", error)
            return {
                "available": False,
                "records": [],
                "record_count": "zero",
                "explanation": f"the recordings could not be listed: {error}",
            }

        for entry in entries:
            try:
                described = self.describe(entry.name, entry.stat().st_size)
            except OSError:
                continue
            if described is None:
                # A file this appliance did not write. Counted, and reported,
                # rather than silently ignored: an operator who has copied
                # recordings in by hand should be told they are not listed.
                skipped += 1
                continue
            if since and described["at"][:10] < since:
                continue
            if until and described["at"][:10] > until:
                continue
            if needle and not any(
                needle in str(described[field]).lower()
                for field in ("source", "destination", "at")
            ):
                continue
            found.append(described)
            if len(found) >= LISTING_LIMIT:
                break

        return {
            "available": True,
            "records": found,
            "record_count": numerals.spell_integer(len(found)),
            "unrecognised_count": numerals.spell_integer(skipped),
            "explanation": (
                f"{numerals.spell_integer(skipped)} files in the recordings "
                "directory are not named the way this appliance names a "
                "recording, and are not listed or served"
            ) if skipped else "",
        }

    # -- serving -----------------------------------------------------------

    def resolve(self, name: str) -> Path | None:
        """The path for one recording, or nothing at all.

        Both rules have to hold. Returning ``None`` rather than raising is
        deliberate: the caller turns it into one refusal with one wording, so a
        name that failed the pattern and a name that escaped the directory
        cannot be told apart from outside.
        """
        candidate = (name or "").strip()
        if RECORDING_PATTERN.match(candidate) is None:
            return None

        try:
            path = (self.directory / candidate).resolve()
            directory = self.directory.resolve()
        except OSError:
            return None

        if not path.is_file():
            return None
        # Every symbolic link has been followed by now, so this is the real
        # location of the real file rather than the name that was asked for.
        if directory not in path.parents:
            _LOG.warning(
                "a recording resolved outside the recordings directory and was refused"
            )
            return None
        return path

    def read(self, name: str) -> tuple[bytes, str] | None:
        """One recording's bytes and its media type, or nothing."""
        path = self.resolve(name)
        if path is None:
            return None
        try:
            if path.stat().st_size > MAXIMUM_BYTES:
                _LOG.warning("a recording larger than the ceiling was refused: %s", name)
                return None
            payload = path.read_bytes()
        except OSError as error:
            _LOG.error("a recording could not be read: %s", error)
            return None

        described = self.describe(path.name, len(payload))
        media_type = described["media_type"] if described else "audio/wav"
        return payload, media_type

    # -- retention ---------------------------------------------------------

    def prune(self, keep_days: int, now: datetime | None = None) -> dict[str, Any]:
        """Delete recordings older than the retention the operator chose.

        Recording fills a disk faster than anything else this appliance does,
        and a telephone system that stops taking calls because its disk is full
        is a worse outcome than a recording nobody kept. A retention of zero
        keeps everything, and says so, rather than meaning "delete everything".
        """
        keep = max(0, int(keep_days or 0))
        if keep == 0 or not self.available():
            return {
                "removed": 0,
                "kept": 0,
                "explanation": "recordings are kept indefinitely" if keep == 0 else "",
            }

        moment = now or datetime.now()
        cutoff = moment.timestamp() - keep * 86400
        removed = kept = 0

        for entry in self.directory.iterdir():
            if not entry.is_file() or RECORDING_PATTERN.match(entry.name) is None:
                continue
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
                else:
                    kept += 1
            except OSError as error:
                _LOG.error("a recording could not be removed: %s", error)
        if removed:
            _LOG.info(
                "removed %s recordings older than %s days", removed, keep
            )
        return {"removed": removed, "kept": kept, "explanation": ""}
