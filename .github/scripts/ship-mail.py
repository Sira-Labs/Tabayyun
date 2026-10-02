#!/usr/bin/env python3
"""Email the owner what a deploy shipped, or that it failed (release.yml, promote.yml, ADR-0017).

The pull requests come from git: the first-parent commits between the commit that was live
before the deploy (PREV) and the one deployed (WANT). Merge commits name their PR as
"Merge pull request #N from ..."; squash merges end in "(#N)". A merge commit carries the PR
title only when the merger kept GitHub's default message, so titles come from the GitHub API
(GITHUB_TOKEN), falling back to the commit body, then to the subject.

Settings (environment):
  SMTP_USERNAME, SMTP_PASSWORD  sender account; a Google Workspace app password
  SHIP_MAIL_TO                  recipient(s), comma-separated
  SMTP_HOST, SMTP_PORT          default smtp.gmail.com:465 (implicit TLS); 587 uses STARTTLS
  TARGET                        staging | production
  STATUS                        shipped | failed
  PREV, WANT                    commits before and after (PREV may be empty or unknown)
  APP_URL, RUN_URL              links in the mail; GITHUB_SERVER_URL and GITHUB_REPOSITORY
                                build the PR links, GITHUB_API_URL and GITHUB_TOKEN read titles

Without SMTP_USERNAME, SMTP_PASSWORD or SHIP_MAIL_TO it prints a notice and exits 0, so a
repository without mail set up deploys as before. `--dry-run` prints the message instead.
Standard library only.
"""

# A CI script: it prints its result to the job log and runs a fixed `git` from PATH on the
# runner's checkout, and reads PR titles from the Actions API URL.
# ruff: noqa: T201, S310, S603, S607

from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import subprocess
import sys
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from urllib import error, request

MERGE_RE = re.compile(r"^Merge pull request #(\d+) from \S+")
SQUASH_RE = re.compile(r"^(.*) \(#(\d+)\)$")
SMTP_TIMEOUT_S = 30
MAX_LISTED = 30


@dataclass(frozen=True)
class Shipped:
    """One change that went out: a pull request, or a commit outside one."""

    title: str
    number: int | None


def git(*args: str) -> str:
    """Output of a git command in the checkout; raises on failure."""
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def is_commit(ref: str) -> bool:
    """Whether `ref` names a commit in this checkout."""
    if not ref:
        return False
    probe = subprocess.run(["git", "cat-file", "-e", f"{ref}^{{commit}}"], capture_output=True)
    return probe.returncode == 0


def pr_title(env: dict[str, str], number: int) -> str | None:
    """The PR's title from the GitHub API, or None when it cannot be read."""
    token, repo = env.get("GITHUB_TOKEN"), env.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return None
    api = env.get("GITHUB_API_URL", "https://api.github.com")
    req = request.Request(f"{api}/repos/{repo}/pulls/{number}")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    try:
        with request.urlopen(req, timeout=15) as resp:
            title = json.load(resp).get("title")
    except (error.URLError, OSError, ValueError) as exc:
        print(f"::notice::title of #{number} not read: {type(exc).__name__}")
        return None
    return title if isinstance(title, str) and title.strip() else None


def shipped(prev: str, want: str, env: dict[str, str] | None = None) -> list[Shipped]:
    """Pull requests (or commits) on the first-parent line after `prev` up to `want`, oldest
    first. Without a usable `prev`, only `want` itself."""
    if is_commit(prev) and prev != want:
        revs = f"{prev}..{want}"
        log = git("log", "--first-parent", "--reverse", "--format=%x1e%s%x1f%b", revs)
    else:
        log = git("log", "-1", "--format=%x1e%s%x1f%b", want)
    items: list[Shipped] = []
    for record in filter(None, log.split("\x1e")):
        subject, _, body = record.partition("\x1f")
        subject = subject.strip()
        if m := MERGE_RE.match(subject):
            number = int(m.group(1))
            from_body = next((line.strip() for line in body.splitlines() if line.strip()), None)
            title = pr_title(env or {}, number) or from_body or "(title not available)"
            items.append(Shipped(title, number))
        elif m := SQUASH_RE.match(subject):
            items.append(Shipped(m.group(1), int(m.group(2))))
        else:
            items.append(Shipped(subject, None))
    return items


def compose(env: dict[str, str], items: list[Shipped]) -> tuple[str, str]:
    """Subject and plain-text body of the mail."""
    target = env.get("TARGET", "staging")
    status = env.get("STATUS", "shipped")
    want = env["WANT"][:7]
    prev = env.get("PREV", "")[:7] if is_commit(env.get("PREV", "")) else ""
    repo = f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/{env.get('GITHUB_REPOSITORY', '')}"
    last = items[-1] if items else None
    first = want if last is None else (last.title if last.number is None else f"#{last.number} {last.title}")
    more = f" (+{len(items) - 1} more)" if len(items) > 1 else ""
    if status == "failed":
        subject = f"Tabayyun {target} deploy failed: {first}{more}"
        intro = (
            f"The deploy of {want} to {target} did not go live. The apps may run a mix of the "
            f"old and the new version until it is fixed; the run log says which."
        )
    else:
        subject = f"Shipped to Tabayyun {target}: {first}{more}"
        was = f" (was {prev})" if prev and prev != want else ""
        intro = f"Tabayyun {target} now runs {want}{was}."
    lines = [intro, "", "Changes:" if status == "shipped" else "In this deploy:"]
    for item in items[:MAX_LISTED]:
        if item.number is None:
            lines.append(f"- {item.title}")
        else:
            lines.append(f"- #{item.number} {item.title}")
            lines.append(f"  {repo}/pull/{item.number}")
    if len(items) > MAX_LISTED:
        lines.append(f"- and {len(items) - MAX_LISTED} more: {repo}/compare/{prev}...{want}")
    lines.append("")
    if env.get("APP_URL"):
        lines.append(f"Open: {env['APP_URL']}")
    if env.get("RUN_URL"):
        lines.append(f"Deploy run: {env['RUN_URL']}")
    if target == "staging" and status == "shipped":
        lines.append("Production follows only when you approve a promote run (ADR-0016).")
    return subject, "\n".join(lines) + "\n"


def send(env: dict[str, str], subject: str, body: str) -> None:
    """Send the mail over implicit TLS (465) or STARTTLS (any other port)."""
    sender = env["SMTP_USERNAME"]
    recipients = [r.strip() for r in env["SHIP_MAIL_TO"].split(",") if r.strip()]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"Tabayyun CI <{sender}>"
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or None)
    msg.set_content(body)
    host = env.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(env.get("SMTP_PORT") or 465)
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_S, context=context) as smtp:
            smtp.login(sender, env["SMTP_PASSWORD"])
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_S) as smtp:
            smtp.starttls(context=context)
            smtp.login(sender, env["SMTP_PASSWORD"])
            smtp.send_message(msg)


def main(argv: list[str]) -> int:
    """Compose the mail and send it (or print it with --dry-run)."""
    env = dict(os.environ)
    if not env.get("WANT"):
        print("::error::WANT (the deployed commit) is required")
        return 2
    items = shipped(env.get("PREV", ""), env["WANT"], env)
    subject, body = compose(env, items)
    if "--dry-run" in argv:
        print(f"Subject: {subject}\n\n{body}", end="")
        return 0
    missing = [k for k in ("SMTP_USERNAME", "SMTP_PASSWORD", "SHIP_MAIL_TO") if not env.get(k)]
    if missing:
        print(f"::notice::ship mail not set up ({', '.join(missing)} missing); nothing sent")
        return 0
    try:
        send(env, subject, body)
    except (smtplib.SMTPException, OSError) as exc:
        # The deploy itself is done; the step is continue-on-error, so this only warns.
        print(f"::warning::ship mail not sent: {type(exc).__name__}: {exc}")
        return 1
    print(f"ship mail sent: {subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
