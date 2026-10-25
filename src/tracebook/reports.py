"""Idempotent windows, local artifacts and opt-in delivery with durable retry."""

from __future__ import annotations

import hashlib
import os
import smtplib
import ssl
import urllib.request
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from tracebook.adapters.base import Adapter
from tracebook.config import Config
from tracebook.model import Json, digest, now, obj, utc
from tracebook.platform import atomic_write
from tracebook.store import Store, encode


def nightly_window(config: Config, at: datetime | None = None) -> tuple[str, str]:
    local = (at or datetime.now(UTC)).astimezone(ZoneInfo(config.timezone))
    end = local.replace(hour=config.report_hour, minute=0, second=0, microsecond=0)
    if local < end:
        end -= timedelta(days=1)
    start = end - timedelta(days=1)
    return utc(start.isoformat()), utc(end.isoformat())


def report(
    store: Store,
    config: Config,
    adapters: dict[str, Adapter],
    start: str,
    end: str,
    enqueue: bool = True,
) -> Json:
    start, end = utc(start), utc(end)
    if start >= end:
        raise ValueError("Report start must be before end")
    key = digest("report-v1", start, end)
    usage = store.overview(start, end)
    findings = store.rows(
        "SELECT * FROM findings WHERE ts>=? AND ts<? ORDER BY severity DESC,ts DESC", (start, end)
    )
    counts = store.rows(
        "SELECT COUNT(*) turns,SUM(status='error') errors,SUM(status='interrupted') interrupted,SUM(status='unknown') incomplete FROM turns WHERE started_at>=? AND started_at<? AND kind='prompt'",
        (start, end),
    )[0]
    coverage: list[Json] = [
        {
            "id": a.id,
            "name": a.display_name,
            "verification": a.verification,
            "capabilities": asdict(a.capabilities()),
        }
        for a in adapters.values()
    ]
    data: Json = {
        "id": key,
        "start": start,
        "end": end,
        "usage": usage,
        "turns": counts,
        "findings": findings,
        "coverage": coverage,
        "billing_mode": config.billing_mode,
    }
    value_note = (
        "API-equivalent value; not billed spend for subscription/plan usage."
        if config.billing_mode != "api"
        else "API-equivalent value; provider invoices remain authoritative."
    )

    def money(amount: object) -> str:
        return "unavailable" if amount is None else f"${float(str(amount)):.4f}"

    lines = [
        "# Tracebook nightly report",
        "",
        f"Window: {start} → {end} (end exclusive)",
        "",
        f"Prompts: {counts['turns']} · Model calls: {usage['calls']} · Unpriced calls: {usage['unpriced_calls'] or 0}",
        f"Input uncached: {usage['input_uncached']} · Cache read: {usage['input_cache_read']} · Output: {usage['output']} · Reasoning: {usage['reasoning']}",
        f"Reported value: {money(usage['cost_reported'])} · Estimated value: {money(usage['cost_estimated'])}",
        "",
        value_note,
        "Missing counters and prices are unknown, never assumed to be zero.",
        "",
        "## Findings",
        "",
    ]
    if not findings:
        lines.append("No rules triggered in this window. Missing capture data can limit detection.")
    for finding in findings:
        lines += [
            f"- **{finding['title']}** ({finding['severity']}): {finding['recommendation']}",
            f"  Evidence: `{finding['evidence']}`",
        ]
    lines += ["", "## Capture coverage", ""]
    for adapter in coverage:
        partial = [k for k, v in adapter["capabilities"].items() if v == "partial"]
        absent = [k for k, v in adapter["capabilities"].items() if v == "no"]
        lines += [
            f"- **{adapter['name']}** — {adapter['verification']}. Partial: {', '.join(partial) or 'none'}. Unavailable: {', '.join(absent) or 'none'}."
        ]
    lines += [
        "",
        "All analysis is local and rule-based. Tracebook adds zero model tokens by default.",
        "",
    ]
    body = "\n".join(lines)
    with store.transaction():
        store.db.execute(
            "INSERT INTO reports VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body,data=excluded.data",
            (key, start, end, now(), body, encode(data)),
        )
        if enqueue:
            for channel, settings in config.delivery.items():
                if obj(settings).get("enabled"):
                    store.db.execute(
                        "INSERT OR IGNORE INTO outbox(id,report_id,channel,next_attempt) VALUES(?,?,?,?)",
                        (digest(key, channel), key, channel, now()),
                    )
    # Files are reproducible from the DB after an interrupted write.
    atomic_write(config.root / "reports" / f"{key}.md", body)
    atomic_write(config.root / "reports" / f"{key}.json", encode(data))
    return {**data, "body": body}


def deliver(store: Store, config: Config, at: str | None = None) -> int:
    stamp = at or now()
    pending = store.rows(
        "SELECT o.*,r.body FROM outbox o JOIN reports r ON r.id=o.report_id WHERE o.status='pending' AND o.next_attempt<=? ORDER BY o.next_attempt LIMIT 20",
        (stamp,),
    )
    delivered = 0
    for item in pending:
        settings = obj(config.delivery.get(item["channel"]))
        if not settings.get("enabled"):
            continue
        try:
            if item["channel"] == "file":
                directory = Path(
                    str(settings.get("directory", config.root / "delivered"))
                ).expanduser()
                atomic_write(directory / f"{item['report_id']}.md", item["body"])
            elif item["channel"] == "webhook":
                url = str(settings.get("url", ""))
                if not url.startswith("https://"):
                    raise ValueError("Webhook delivery requires HTTPS")
                headers = {"Content-Type": "application/json", "Idempotency-Key": item["id"]}
                token = os.environ.get(str(settings.get("token_env", "TRACEBOOK_WEBHOOK_TOKEN")))
                if token:
                    headers["Authorization"] = "Bearer " + token
                request = urllib.request.Request(
                    url,
                    encode({"id": item["report_id"], "text": item["body"]}).encode(),
                    headers,
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=15) as response:
                    if response.status >= 300:
                        raise ValueError(f"Delivery HTTP status {response.status}")
            elif item["channel"] == "smtp":
                message = EmailMessage()
                message["From"] = str(settings["from"])
                message["To"] = str(settings["to"])
                message["Subject"] = "Tracebook nightly report"
                message["Message-ID"] = (
                    f"<{hashlib.sha256(item['id'].encode()).hexdigest()}@tracebook.local>"
                )
                message.set_content(item["body"])
                with smtplib.SMTP(
                    str(settings["host"]), int(settings.get("port", 587)), timeout=15
                ) as client:
                    client.starttls(context=ssl.create_default_context())
                    if settings.get("username"):
                        client.login(
                            str(settings["username"]),
                            os.environ[
                                str(settings.get("password_env", "TRACEBOOK_SMTP_PASSWORD"))
                            ],
                        )
                    client.send_message(message)
            else:
                raise ValueError("Unknown delivery channel")
        except (OSError, ValueError, KeyError, smtplib.SMTPException) as exc:
            attempts = item["attempts"] + 1
            retry = utc(
                (
                    datetime.now(UTC) + timedelta(seconds=min(86400, 30 * 2 ** min(attempts, 12)))
                ).isoformat()
            )
            # Errors never include URLs, credentials or response bodies.
            store.db.execute(
                "UPDATE outbox SET attempts=?,next_attempt=?,last_error=? WHERE id=?",
                (attempts, retry, type(exc).__name__, item["id"]),
            )
        else:
            store.db.execute(
                "UPDATE outbox SET status='sent',attempts=attempts+1,last_error=NULL WHERE id=?",
                (item["id"],),
            )
            delivered += 1
    return delivered
