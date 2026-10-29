"""Idempotent windows, local artifacts and opt-in delivery with durable retry."""

from __future__ import annotations

import asyncio
import hashlib
import os
import smtplib
import ssl
import urllib.request
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from tracebook.adapters.base import Adapter
from tracebook.config import Config
from tracebook.model import Json, digest, now, obj, utc
from tracebook.platform import atomic_write
from tracebook.store import Store, encode


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        # A configured channel must not forward private reports/credentials elsewhere.
        return None


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
    datetime.fromisoformat(start.replace("Z", "+00:00"))
    datetime.fromisoformat(end.replace("Z", "+00:00"))
    start, end = utc(start), utc(end)
    if start >= end:
        raise ValueError("Report start must be before end")
    key = digest("report-v1", start, end)
    usage = store.overview(start, end)
    models = store.rows(
        "SELECT agent,model,COUNT(*) calls,"
        "SUM(input_uncached) input_uncached,SUM(input_cache_read) input_cache_read,"
        "SUM(input_cache_write) input_cache_write,SUM(output) output,SUM(reasoning) reasoning,"
        "SUM(cost_reported) cost_reported,"
        "SUM(CASE WHEN cost_reported IS NULL THEN cost_estimated END) cost_estimated,"
        "SUM(cost_reported IS NULL AND cost_estimated IS NULL) unpriced_calls "
        "FROM accounted_model_calls WHERE ts>=? AND ts<? GROUP BY agent,model ORDER BY calls DESC",
        (start, end),
    )
    priced = config.pricing_enabled
    if not priced:
        for row in (usage, *models):
            for field in ("cost_reported", "cost_estimated", "unpriced_calls"):
                row.pop(field, None)
        usage["agents"] = [{**a, "value": None} for a in usage["agents"]]
    pricing = (
        []
        if not priced
        else store.rows(
            "SELECT DISTINCT json_extract(data,'$.pricing_as_of') as_of,"
            "json_extract(data,'$.pricing_basis') basis,json_extract(data,'$.pricing_source') source "
            "FROM accounted_model_calls WHERE ts>=? AND ts<? AND cost_estimated IS NOT NULL",
            (start, end),
        )
    )
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
        "models": models,
        "pricing": pricing,
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
        "# Tracebook usage report",
        "",
        f"Window: {start} → {end} (end exclusive)",
        "",
        f"Prompts: {counts['turns']} · Model calls: {usage['calls']}"
        + (f" · Unpriced calls: {usage['unpriced_calls'] or 0}" if priced else ""),
        f"Input uncached: {usage['input_uncached']} · Cache read: {usage['input_cache_read']} · Cache write: {usage['input_cache_write']} · Output: {usage['output']} · Reasoning: {usage['reasoning']}",
        *(
            [
                f"Reported value: {money(usage['cost_reported'])} · Estimated value: {money(usage['cost_estimated'])}",
                "",
                value_note,
            ]
            if priced
            else []
        ),
        "Missing counters"
        + (" and prices are" if priced else " are")
        + " unknown, never assumed to be zero.",
        "",
        "## Tokens by model",
        "",
        "| Agent | Model | Usage records | Input uncached | Cache read | Cache write | Output | Reasoning |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]

    def cell(value: object) -> str:
        return (
            str(value if value is not None else "unavailable")
            .replace("|", "\\|")
            .replace("\n", " ")
        )

    for model in models:
        lines.append(
            "| "
            + " | ".join(
                cell(model[field])
                for field in (
                    "agent",
                    "model",
                    "calls",
                    "input_uncached",
                    "input_cache_read",
                    "input_cache_write",
                    "output",
                    "reasoning",
                )
            )
            + " |"
        )
    lines += [
        "",
        "> **Token Breakdown Guide**:",
        "> - **Input uncached**: Fresh prompt and file contents processed from scratch at standard input rates.",
        "> - **Cache read**: Reused prompt context retrieved from fast provider cache (discounted 75–90%).",
        "> - **Cache write**: First-time context saved into provider prompt cache (tracked by Anthropic Claude; Google Gemini / Antigravity uses automatic prefix caching without a separate cache-write fee or counter, shown as unavailable).",
        "> - **Output**: Text, code completions, and tool call instructions generated by the model.",
        "> - **Reasoning**: Internal thinking tokens used by reasoning models to formulate their plan.",
    ]
    if priced:
        lines += [
            "",
            "## Cost by model",
            "",
            "| Agent | Model | Reported value | Estimated value | Unpriced records |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
        for model in models:
            lines.append(
                f"| {cell(model['agent'])} | {cell(model['model'])} | "
                f"{money(model['cost_reported'])} | {money(model['cost_estimated'])} | "
                f"{model['unpriced_calls']} |"
            )
        lines += [
            "",
            "Estimated value only fills records without reported cost. "
            "Unpriced records are excluded, so a partial sum is not a complete cost total.",
        ]
        for card in pricing:
            lines.append(
                f"Pricing as of {cell(card['as_of'])}: {cell(card['basis'])}. "
                f"Source: {cell(card['source'])}"
            )
    lines += ["", "## Findings", ""]
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


def pending(store: Store, config: Config, at: str | None = None) -> list[Json]:
    store.db.execute(
        "UPDATE outbox SET status='pending' WHERE status='sending' AND next_attempt<=?",
        (at or now(),),
    )
    return [
        item
        for item in store.rows(
            "SELECT o.*,r.body FROM outbox o JOIN reports r ON r.id=o.report_id WHERE o.status='pending' AND o.next_attempt<=? ORDER BY o.next_attempt LIMIT 20",
            (at or now(),),
        )
        if obj(config.delivery.get(item["channel"])).get("enabled")
    ]


def claim(store: Store, item: Json, at: str | None = None) -> bool:
    lease = utc((datetime.now(UTC) + timedelta(minutes=5)).isoformat())
    return (
        store.db.execute(
            "UPDATE outbox SET status='sending',next_attempt=? WHERE id=? AND status='pending' AND next_attempt<=?",
            (lease, item["id"], at or now()),
        ).rowcount
        == 1
    )


def send_delivery(item: Json, config: Config) -> None:
    settings = obj(config.delivery.get(item["channel"]))
    if item["channel"] == "file":
        directory = Path(str(settings.get("directory", config.root / "delivered"))).expanduser()
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
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
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
                    os.environ[str(settings.get("password_env", "TRACEBOOK_SMTP_PASSWORD"))],
                )
            client.send_message(message)
    else:
        raise ValueError("Unknown delivery channel")


def settle(store: Store, item: Json, error: str | None) -> int:
    if error:
        attempts = item["attempts"] + 1
        retry = utc(
            (
                datetime.now(UTC) + timedelta(seconds=min(86400, 30 * 2 ** min(attempts, 12)))
            ).isoformat()
        )
        # Errors never include URLs, credentials or response bodies.
        store.db.execute(
            "UPDATE outbox SET status='pending',attempts=?,next_attempt=?,last_error=? WHERE id=?",
            (attempts, retry, error, item["id"]),
        )
        return 0
    store.db.execute(
        "UPDATE outbox SET status='sent',attempts=attempts+1,last_error=NULL WHERE id=?",
        (item["id"],),
    )
    return 1


def deliver(store: Store, config: Config, at: str | None = None) -> int:
    delivered = 0
    for item in pending(store, config, at):
        if not claim(store, item, at):
            continue
        error = None
        try:
            send_delivery(item, config)
        except (OSError, ValueError, KeyError, smtplib.SMTPException) as exc:
            error = type(exc).__name__
        delivered += settle(store, item, error)
    return delivered


async def deliver_async(store: Store, config: Config) -> int:
    delivered = 0
    for item in pending(store, config):
        if not claim(store, item):
            continue
        error = None
        try:
            await asyncio.to_thread(send_delivery, item, config)
        except (OSError, ValueError, KeyError, smtplib.SMTPException) as exc:
            error = type(exc).__name__
        # Only transport runs off-thread; SQLite mutations remain on the daemon writer.
        delivered += settle(store, item, error)
    return delivered


def report_due(
    store: Store, config: Config, registry: dict[str, Adapter], at: datetime | None = None
) -> int:
    start, latest = nightly_window(config, at)
    state = store.state("nightly-scheduler")
    cursor = str(state.get("end", start))
    generated = 0
    # Catch up in bounded batches after sleep/outages; the cursor survives restarts.
    while cursor < latest and generated < 31:
        local = datetime.fromisoformat(cursor.replace("Z", "+00:00")).astimezone(
            ZoneInfo(config.timezone)
        )
        end = min(utc((local + timedelta(days=1)).isoformat()), latest)
        report(store, config, registry, cursor, end)
        store.checkpoint("nightly-scheduler", "scheduler", "generic", {"end": end})
        cursor = end
        generated += 1
    return generated
