"""Backup and restore, performed entirely from the graphical interface.

A backup that requires a terminal is a backup that does not get taken. The
whole of the appliance's recoverable state — the source of truth document, the
secrets, the administrator credential, and the record of what was rendered — is
produced here as a single archive an operator downloads from the dashboard, and
accepted back the same way.

The restore path treats the uploaded archive as hostile. Only a fixed set of
member names is accepted, each is checked for a traversing path, and anything
that is not a regular file — a symbolic link, a device node, a hard link — is
refused outright. An archive that would write anywhere other than the state
directory is rejected before a single byte is extracted.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import time
from pathlib import Path
from typing import Any

from . import numerals
from .logging_setup import get_logger

__all__ = ["create", "restore", "RestoreRefused", "MEMBERS", "MANIFEST_NAME"]

_LOG = get_logger("backup")

MANIFEST_NAME = "manifest.json"

#: The complete set of members a backup carries.  Anything else in an uploaded
#: archive is refused rather than ignored, because an unexpected member means
#: the archive is not one of ours.
MEMBERS = (
    "appliance.json",
    "secrets.json",
    "credentials.json",
    "rendered-digests.json",
)

#: A backup is small.  A larger upload is refused before it is opened, so a
#: compressed archive cannot be used to exhaust memory or disk.
_MAXIMUM_ARCHIVE_BYTES = 8 * 1024 * 1024
_MAXIMUM_MEMBER_BYTES = 4 * 1024 * 1024

_FORMAT_VERSION = 1


class RestoreRefused(ValueError):
    """The uploaded archive was not acceptable and nothing was written."""


def _sources(context: Any) -> dict[str, Path]:
    """Map each member name onto where it lives on this appliance."""
    config = context.config
    return {
        "appliance.json": Path(config.configuration_document),
        "secrets.json": config.state_path / "secrets.json",
        "credentials.json": Path(config.credentials_path),
        "rendered-digests.json": Path(config.digest_path),
    }


def create(context: Any) -> tuple[bytes, str]:
    """Build a backup archive in memory and return it with a file name."""
    sources = _sources(context)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    included: list[str] = []

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, path in sources.items():
            if not path.is_file():
                continue
            payload = path.read_bytes()
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            info.mode = 0o600
            info.mtime = int(time.time())
            archive.addfile(info, io.BytesIO(payload))
            included.append(name)

        manifest = json.dumps(
            {
                "format_version": _FORMAT_VERSION,
                "product": "Legacy-to-Modern IPBX Appliance",
                "created_at": stamp,
                "members": included,
                "contains_secrets": "secrets.json" in included
                or "credentials.json" in included,
                "warning": (
                    "this archive carries the appliance's secrets and its "
                    "administrator credential; store it as you would store a "
                    "password"
                ),
            },
            indent=2,
        ).encode("utf-8")

        info = tarfile.TarInfo(name=MANIFEST_NAME)
        info.size = len(manifest)
        info.mode = 0o600
        info.mtime = int(time.time())
        archive.addfile(info, io.BytesIO(manifest))

    payload = buffer.getvalue()
    _LOG.info(
        "a backup was produced carrying %d file or files",
        len(included),
    )
    return payload, f"myipbx-backup-{stamp}.tar.gz"


def inspect(payload: bytes) -> dict[str, Any]:
    """Describe an archive without writing anything.

    Every refusal reason is raised here, so the restore path can validate
    completely before it touches the filesystem.
    """
    if not payload:
        raise RestoreRefused("the archive is empty")
    if len(payload) > _MAXIMUM_ARCHIVE_BYTES:
        raise RestoreRefused(
            "the archive is larger than a backup of this appliance can be"
        )

    permitted = set(MEMBERS) | {MANIFEST_NAME}
    found: dict[str, int] = {}
    manifest: dict[str, Any] = {}

    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            for member in archive.getmembers():
                name = member.name.lstrip("./")

                if not member.isfile():
                    raise RestoreRefused(
                        f"the archive carries {member.name}, which is not a plain "
                        "file; only plain files are accepted"
                    )
                if os.path.isabs(member.name) or ".." in Path(member.name).parts:
                    raise RestoreRefused(
                        "the archive carries a member whose path would write "
                        "outside the appliance state directory"
                    )
                if name not in permitted:
                    raise RestoreRefused(
                        f"the archive carries an unexpected member named {name}"
                    )
                if member.size > _MAXIMUM_MEMBER_BYTES:
                    raise RestoreRefused(
                        f"the member named {name} is larger than it could legitimately be"
                    )
                found[name] = member.size

            if MANIFEST_NAME in found:
                handle = archive.extractfile(MANIFEST_NAME)
                if handle is not None:
                    try:
                        manifest = json.loads(handle.read().decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        raise RestoreRefused(
                            "the archive's manifest could not be read"
                        ) from None
    except tarfile.TarError as error:
        raise RestoreRefused(f"the archive could not be opened: {error}") from error

    if "appliance.json" not in found:
        raise RestoreRefused(
            "the archive carries no configuration document, so it is not a "
            "backup of this appliance"
        )

    version = manifest.get("format_version", _FORMAT_VERSION)
    if not isinstance(version, int) or version > _FORMAT_VERSION:
        raise RestoreRefused(
            "the archive was produced by a later version of the appliance than "
            "this one, and cannot be read safely"
        )

    return {"members": found, "manifest": manifest}


#: Settings that describe how this appliance defends itself, rather than how
#: this site's telephony is arranged. They are read out of the same document a
#: backup carries, and they are the ones an archive must not be able to move.
#:
#: A backup taken before transport security was configured carries
#: ``tls_enabled: false``. Restoring it put the console back on plain transport
#: at the next start, silently, as a side effect of an operator recovering
#: their dial plan. Nobody chose that, and nothing said it had happened. The
#: same applies to the exclusion this product is built around: an archive must
#: not be able to switch off the check that keeps an address allocation service
#: from running here.
PROTECTED_SETTINGS = (
    "tls_enabled",
    "tls_certificate",
    "tls_private_key",
    "fail_on_address_allocation_server",
    "password_iterations",
    "session_idle_seconds",
    "maximum_sessions",
)


def _preserve_protected_settings(
    document: dict[str, Any], context: Any
) -> tuple[dict[str, Any], list[str]]:
    """Keep this appliance's own defences across a restore.

    The archive supplies the site: its trunks, its extensions, its routes. The
    running appliance keeps its posture. Anything held back is named, because a
    restore that quietly ignores part of what was handed to it is as
    surprising as one that quietly accepts all of it.
    """
    incoming = document.get("appliance")
    if not isinstance(incoming, dict):
        return document, []

    current_path = Path(context.config.configuration_document)
    current: dict[str, Any] = {}
    if current_path.is_file():
        try:
            loaded = json.loads(current_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, dict) and isinstance(loaded.get("appliance"), dict):
            current = loaded["appliance"]

    held: list[str] = []
    for name in PROTECTED_SETTINGS:
        if name not in incoming:
            continue
        running = current.get(name, getattr(context.config, name, None))
        if incoming[name] == running:
            continue
        held.append(name)
        if name in current:
            incoming[name] = current[name]
        else:
            del incoming[name]

    return document, held


def restore(
    context: Any, payload: bytes, *, replace_credentials: bool = False
) -> dict[str, Any]:
    """Validate an archive completely, then write it.

    Nothing is written until every member has passed inspection, so a bad
    archive cannot leave the appliance half restored.

    The administrator credential is held back unless it is asked for by name.
    An archive is a file an operator can be handed, and a restore that always
    replaced the credential would make anyone holding an old backup able to put
    the password of that day back onto a running appliance -- and would lock
    out the administrator who had changed it since. Recovering a dial plan and
    recovering a credential are two decisions, so they are asked as two.
    """
    description = inspect(payload)
    targets = _sources(context)
    written: list[str] = []
    held_back: list[str] = []

    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        # Read every member into memory first.  A backup is small, and this
        # removes any window in which a partial write could survive a failure.
        staged: dict[str, bytes] = {}
        for name in description["members"]:
            if name == MANIFEST_NAME:
                continue
            handle = archive.extractfile(name)
            if handle is None:
                raise RestoreRefused(f"the member named {name} could not be read")
            staged[name] = handle.read()

        # The configuration document must be readable as a mapping, or the
        # appliance would restore itself into an unusable state.
        try:
            document = json.loads(staged["appliance.json"].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RestoreRefused(
                f"the configuration document in the archive is not valid: {error}"
            ) from error
        if not isinstance(document, dict):
            raise RestoreRefused("the configuration document in the archive is not a mapping")

        document, held_settings = _preserve_protected_settings(document, context)
        if held_settings:
            staged["appliance.json"] = json.dumps(document, indent=2).encode("utf-8")
            held_back.extend(held_settings)

        if not replace_credentials and "credentials.json" in staged:
            del staged["credentials.json"]
            held_back.append("the administrator credential")

        for name, content in staged.items():
            destination = targets.get(name)
            if destination is None:
                continue
            _write_privately(destination, content)
            written.append(name)

    _LOG.warning(
        "a backup was restored, replacing %d file or files and holding %d value or "
        "values back; the appliance should now be restarted so that every "
        "component reads the restored state",
        len(written),
        len(held_back),
    )

    return {
        "restored": True,
        "written": written,
        "written_count": numerals.spell_integer(len(written)),
        "held_back": held_back,
        "held_back_count": numerals.spell_integer(len(held_back)),
        "created_at": description["manifest"].get("created_at", "an unrecorded time"),
        "next_step": (
            "restart the appliance so that every component reads the restored "
            "configuration, then render the engine configuration from it"
        ),
    }


def _write_privately(path: Path, content: bytes) -> None:
    """Write a restored file atomically and readable only by its owner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    os.replace(temporary, path)
    os.chmod(path, 0o600)
