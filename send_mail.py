#!/usr/bin/env python3
"""
Email the battery/PAKOVANJE price review CSV to the weekly price job.

WHY THIS EXISTS
---------------
The scheduled price job cannot fetch the mirror's raw GitHub URLs any more:
its web_fetch refuses any URL that did not arrive in a human's message, and a
scheduled run has no human message by definition. Browser fallback works but
needs Peter present. So the mirror PUSHES the data to the job instead of the
job pulling it. Gmail is the transport because the job already has a Gmail
connector wired up.

The CSV goes INLINE IN THE BODY, not as an attachment: the job's Gmail
connector exposes attachment *metadata* only, with no way to download
attachment bytes. Inline is ~15 KB, well within limits.

SENDS ON EVERY RUN, including runs where nothing changed. The job treats a
fresh message as its liveness signal, so silence must always mean "the mirror
is broken", never "there was nothing to say". Do not add an "only if changed"
condition here -- the commit step is allowed to skip, this step is not.

Secrets (GitHub repo -> Settings -> Secrets and variables -> Actions):
  MAIL_USER  the bot Gmail address that sends
  MAIL_PASS  that account's 16-char app password (NOT the login password)
  MAIL_TO    where the report goes (defaults to Peter)
"""

import os
import smtplib
import ssl
import sys
from email.message import EmailMessage

CSV_PATH = "baterije-pregled.csv"
RUN_PATH = "last-run.txt"

BEGIN = "-----BEGIN CSV-----"
END = "-----END CSV-----"


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def main():
    user = os.environ.get("MAIL_USER", "").strip()
    password = os.environ.get("MAIL_PASS", "").strip()
    to_addr = os.environ.get("MAIL_TO", "").strip() or "81sajo@gmail.com"

    if not user or not password:
        sys.exit("MAIL_USER / MAIL_PASS are not set (add them as repo secrets).")

    last_run = read(RUN_PATH)
    csv_text = read(CSV_PATH)

    if not csv_text.strip():
        sys.exit("baterije-pregled.csv is empty -- refusing to send.")
    if not csv_text.endswith("\n"):
        csv_text += "\n"

    # The timestamp goes in the subject so the job can check freshness without
    # parsing the body, and so a stale message is obvious in the inbox.
    stamp = ""
    for line in last_run.splitlines():
        if line.startswith("Last successful run:"):
            stamp = line.split(":", 1)[1].strip()
            break

    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = to_addr
    msg["Subject"] = f"[elementa-mirror] baterije-pregled {stamp}"
    msg.set_content(
        f"{last_run}\n"
        f"{BEGIN}\n"
        f"{csv_text}"
        f"{END}\n"
    )

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)

    print(f"mailed to {to_addr}: stamp={stamp} csv_bytes={len(csv_text)}")


if __name__ == "__main__":
    main()
