"""Sending one report by electronic mail, using the standard library alone.

This appliance accepts no mail and runs no mail server. It reaches out to a
server somebody named, hands over one message, and disconnects. That is the
whole of it, and it is written here in one place so that the one part of this
product which talks to a machine outside the site is a page somebody can read
in full before deciding to turn it on.

Two things it will not do:

**It will not send a password in the clear without being told to.** A
destination whose connection is unprotected has to say so explicitly, and the
word appears in the console beside an explanation of what it means.

**It will not fail a scheduled task because a mail server was unreachable.**
The report has already been drawn and kept on the appliance by the time this is
reached; a server that is down should cost the site a delivery, not the report.
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Callable, Mapping

from .logging_setup import get_logger

__all__ = ["Destination", "MailRefused", "build_message", "send", "destination_from"]

_LOG = get_logger("mailer")

#: How long to wait on a server that is not answering. Short, because this runs
#: inside a scheduled task and a task that hangs stops every task behind it.
TIMEOUT_SECONDS = 20.0


class MailRefused(RuntimeError):
    """A message that could not be sent, with the reason a person can act on."""


@dataclass(frozen=True)
class Destination:
    """Where a report goes, and how this appliance gets it there."""

    name: str
    recipient: str
    sender: str
    host: str
    port: int = 587
    security: str = "upgraded"
    username: str = ""
    secret: str = ""


def destination_from(record: Mapping[str, Any], secret: str = "") -> Destination:
    return Destination(
        name=str(record.get("name", "")).strip(),
        recipient=str(record.get("recipient", "")).strip(),
        sender=str(record.get("sender", "")).strip(),
        host=str(record.get("host", "")).strip(),
        port=int(record.get("port") or 587),
        security=str(record.get("security", "upgraded")).strip().lower(),
        username=str(record.get("username", "")).strip(),
        secret=secret,
    )


def build_message(
    destination: Destination,
    subject: str,
    body: str,
    attachment: tuple[str, str] | None = None,
) -> EmailMessage:
    """The message itself, with the report attached as a file.

    Attached rather than pasted into the body, because the file carries digits
    and the body carries words, and a report somebody has to retype out of an
    electronic mail is a report nobody uses.
    """
    message = EmailMessage()
    message["From"] = destination.sender
    message["To"] = destination.recipient
    message["Subject"] = subject
    message.set_content(body)

    if attachment is not None:
        name, payload = attachment
        message.add_attachment(
            payload.encode("utf-8"),
            maintype="text",
            subtype="csv",
            filename=name,
        )
    return message


def send(
    destination: Destination,
    message: EmailMessage,
    transport: Callable[..., Any] | None = None,
) -> None:
    """Hand one message to the server, or raise with a reason.

    ``transport`` exists so the suite can exercise every branch of this without
    a mail server: it is called exactly as the standard library's own client
    would be. Nothing else passes it.
    """
    if not destination.host:
        raise MailRefused("the destination names no mail server")
    if not destination.recipient or not destination.sender:
        raise MailRefused(
            "the destination is missing an address to send to or to send from"
        )

    secured = destination.security == "secured"
    factory = transport or (smtplib.SMTP_SSL if secured else smtplib.SMTP)

    try:
        with factory(destination.host, destination.port, timeout=TIMEOUT_SECONDS) as client:
            if destination.security == "upgraded":
                # The context carries the system's own trust store, which is
                # what a site's own certificate authority is installed into.
                client.starttls(context=ssl.create_default_context())
            if destination.username:
                if destination.security == "none":
                    # Said out loud rather than silently done. An operator who
                    # chose an unprotected connection may not have realised the
                    # password goes across the network with it.
                    _LOG.warning(
                        "the mail destination named %s sends its password over an "
                        "unprotected connection, because that is how it is configured",
                        destination.name,
                    )
                client.login(destination.username, destination.secret)
            client.send_message(message)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as error:
        raise MailRefused(
            f"the report could not be sent to {destination.recipient} through "
            f"{destination.host}: {error}"
        ) from error

    _LOG.info(
        "a scheduled report was sent to the destination named %s", destination.name
    )
