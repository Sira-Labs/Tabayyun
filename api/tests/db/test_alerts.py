"""Alerts against the database (spec 031): evaluation after a run, the throttle, delivery by
email and webhook, the routes and the audit log."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from admin_support import ADAM, BOB, MIA, NED, ORG_B
from fake_smtp import smtp  # noqa: F401  (fixture)
from synth_csv import faulty_csv
from tabayyun.alerts import delivery, evaluate, rules
from tabayyun.authz.scope import Scope
from tabayyun.connectors import NetPolicy
from tabayyun.db import for_org
from tabayyun.db.models import DEFAULT_ORG_ID, DEFAULT_WORKSPACE_ID
from tabayyun.mail import SmtpConfig
from tabayyun.secrets import Keyring
from tenancy import org_session

SCOPE = Scope(org_id=DEFAULT_ORG_ID, workspace_id=DEFAULT_WORKSPACE_ID)
T0 = datetime(2026, 11, 20, 2, 0, tzinfo=UTC)
KEY = base64.b64encode(os.urandom(32)).decode()
PUBLIC = "93.184.216.34"
EMAIL_RULE = {
    "name": "Critical findings",
    "kind": "finding",
    "condition": {"min_severity": "critical"},
    "channel": "email",
    "recipients": ["ops@example.org"],
}
HOOK_RULE = {
    "name": "Hook",
    "kind": "finding",
    "condition": {"min_severity": "low"},
    "channel": "webhook",
    "url": "https://hooks.example.org/t/abc",
}


def mail_settings(inbox) -> dict:
    return {
        "smtp_host": inbox.host,
        "smtp_port": inbox.port,
        "smtp_starttls": False,
        "smtp_from": "Tabayyun <tabayyun@example.org>",
        "public_url": "https://tabayyun.example.org",
    }


def deps(inbox=None, net: NetPolicy | None = None, keyring: Keyring | None = None) -> delivery.Deps:
    config = None
    if inbox is not None:
        config = SmtpConfig(
            host=inbox.host,
            port=inbox.port,
            starttls=False,
            username=None,
            password=None,
            sender="t@example.org",
        )
    return delivery.Deps(
        smtp=config, net=net or NetPolicy(), keyring=keyring, public_url="https://tabayyun.example.org"
    )


def text_of(message) -> str:
    return message.get_payload(decode=True).decode()


class World:
    """Series made through the API, and runs, findings and scores written as the owner, so
    each test decides exactly what a run stored."""

    def __init__(self, env, client) -> None:
        self.env, self.client, self.app = env, client, env.apps[-1]
        self.factory = for_org(self.app.state.session_factory, DEFAULT_ORG_ID)
        self.n = 0

    async def series(self, name: str, asset_path: str | None = None) -> str:
        rows = "ts,value\n" + "".join(f"2026-01-01T{h:02d}:00:00Z,{h}\n" for h in range(24))
        r = await self.client.post(
            "/api/runs", files={"file": ("f.csv", rows.encode(), "text/csv")}, data={"series_id": name}
        )
        run = (await self.client.get(f"/api/runs/{r.json()['id']}")).json()
        assert run["status"] == "succeeded", run
        sid = run["series"][0]["id"]
        if asset_path is not None:
            assert (
                await self.client.patch(f"/api/series/{sid}", json={"asset_path": asset_path})
            ).status_code == 200
        self.env.sql("DELETE FROM findings WHERE series_id = :s", s=sid)
        self.env.sql("DELETE FROM scores WHERE series_id = :s", s=sid)
        return sid

    def run(self) -> uuid.UUID:
        run_id = uuid.uuid4()
        self.env.sql(
            "INSERT INTO runs (id, org_id, workspace_id, trigger, status, finished_at) "
            "VALUES (:id, :o, :w, 'upload', 'succeeded', now())",
            id=run_id,
            o=DEFAULT_ORG_ID,
            w=DEFAULT_WORKSPACE_ID,
        )
        return run_id

    def finding(
        self, run_id, series, severity="critical", check="tby.flatline", dimension="plausibility", new=True
    ):
        """A finding the run stored as new, or (`new=False`) one it only saw again."""
        self.n += 1
        start = T0 + timedelta(hours=self.n)
        first = run_id if new else self.run()
        self.env.sql(
            "INSERT INTO findings (org_id, workspace_id, id, series_id, check_id, dimension, severity, "
            "window_start, window_end, summary, status, first_run_id, last_run_id) VALUES (:o, :w, :id, :s, "
            ":c, :d, :sev, :a, :b, :sum, 'open', :first, :last)",
            o=DEFAULT_ORG_ID,
            w=DEFAULT_WORKSPACE_ID,
            id=uuid.uuid4(),
            s=series,
            c=check,
            d=dimension,
            sev=severity,
            a=start,
            b=start + timedelta(minutes=30),
            sum=f"{check} number {self.n}",
            first=first,
            last=run_id,
        )

    def score(self, run_id, series, overall: float, at: datetime, **dimensions: float) -> None:
        self.env.sql(
            "INSERT INTO scores (org_id, series_id, run_id, layer, method_version, overall, dimensions, "
            "n_findings, computed_at) VALUES (:o, :s, :r, 'raw', 'v1', :v, CAST(:d AS jsonb), 0, :at)",
            o=DEFAULT_ORG_ID,
            s=series,
            r=run_id,
            v=overall,
            d=json.dumps(dimensions),
            at=at,
        )

    async def rule(self, **body) -> dict:
        r = await self.client.post("/api/alerts", json=body)
        assert r.status_code == 201, r.text
        return r.json()

    async def evaluate(self, run_id, now: datetime, defer: bool = False):
        async with org_session(self.app) as session, session.begin():
            return await evaluate.evaluate_run(session, SCOPE, run_id, now=now, defer=defer)

    def notifications(self, rule_id: str | None = None) -> list:
        where = "" if rule_id is None else "WHERE rule_id = :r"
        return self.env.sql(
            f"SELECT id, status, n_matches, due_at, attempts, error, kind FROM alert_notifications {where} "  # noqa: S608
            "ORDER BY created_at, due_at",
            **({} if rule_id is None else {"r": rule_id}),
        )


class Unread(httpx.AsyncByteStream):
    """A response body that fails the test when anything reads it."""

    async def __aiter__(self):
        raise AssertionError("the webhook's answer was read")
        yield b""  # pragma: no cover


def hook_net(handler) -> NetPolicy:
    """An address policy whose network is `handler`; names resolve to a public address."""

    async def resolver(host: str, port: int) -> list[str]:
        return [PUBLIC]

    return NetPolicy(resolver=resolver, transport=httpx.MockTransport(handler))


async def test_critical_finding_sends_one_email(admin_env, smtp):  # noqa: F811
    """End to end with inline jobs: a rule, an upload that stores new findings, one email that
    names them and links to the page. The same upload again finds nothing new and sends none."""
    async with admin_env.client(**mail_settings(smtp)) as c:
        rule = (await c.post("/api/alerts", json={**EMAIL_RULE, "condition": {"min_severity": "low"}})).json()
        csv = faulty_csv(["gap", "flatline", "negative"])
        first = await c.post(
            "/api/runs", files={"file": ("f.csv", csv, "text/csv")}, data={"series_id": "demo"}
        )
        assert first.status_code == 202
        run = (await c.get(f"/api/runs/{first.json()['id']}")).json()
        assert run["status"] == "succeeded" and run["stats"]["n_findings_new"] > 0
        again = await c.post(
            "/api/runs", files={"file": ("f.csv", csv, "text/csv")}, data={"series_id": "demo"}
        )
        assert (await c.get(f"/api/runs/{again.json()['id']}")).json()["stats"]["n_findings_new"] == 0
        shown = (await c.get(f"/api/alerts/{rule['id']}")).json()["last_notification"]
        history = (await c.get(f"/api/alerts/{rule['id']}/notifications")).json()["items"]
    [message] = smtp.messages
    new = run["stats"]["n_findings_new"]
    assert message["To"] == "ops@example.org"
    assert message["Subject"].startswith(f"[Tabayyun] Critical findings: {new} new finding")
    body = text_of(message)
    assert "demo" in body and "tby." in body and "Open: https://tabayyun.example.org/findings" in body
    assert (shown["status"], shown["n_matches"], shown["attempts"], shown["error"]) == ("sent", new, 1, None)
    assert shown["sent_at"] is not None and [n["id"] for n in history] == [shown["id"]]


async def test_recurrence_and_non_matching_send_nothing(admin_env):
    """Only findings the run stored as new and that fit the condition are told."""
    async with admin_env.client() as c:
        world = World(admin_env, c)
        feeder = await world.series("feeder", "plant/feeder-4")
        mill = await world.series("mill", "mill/line-1")
        rule = await world.rule(
            **{
                **EMAIL_RULE,
                "condition": {
                    "min_severity": "high",
                    "check_ids": ["tby.flatline"],
                    "dimensions": ["plausibility"],
                    "asset_prefix": "plant/",
                },
            }
        )
        disabled = await world.rule(**{**EMAIL_RULE, "name": "Off", "enabled": False})
        run = world.run()
        world.finding(run, feeder, new=False)  # a recurrence
        world.finding(run, feeder, severity="medium")  # not severe enough
        world.finding(run, feeder, check="tby.spikes")  # another check
        world.finding(run, feeder, dimension="completeness")  # another dimension
        world.finding(run, mill)  # another asset
        assert await world.evaluate(run, T0) == []
        assert world.notifications() == []

        # A run is evaluated once: asking again tells nothing, whatever it holds by then.
        world.finding(run, feeder)
        assert await world.evaluate(run, T0) == []
        run = world.run()
        world.finding(run, feeder)
        world.finding(run, feeder, severity="high")
        world.finding(run, mill)
        [started] = await world.evaluate(run, T0)
        assert (started.rule_id, started.n_matches, started.due_at) == (uuid.UUID(rule["id"]), 2, T0)
        assert [i["severity"] for i in started.items] == ["critical", "high"]
        assert (
            started.items[0]["series_name"] == "feeder" and started.items[0]["asset_path"] == "plant/feeder-4"
        )
        assert world.notifications(disabled["id"]) == []


async def test_score_rule_fires_on_crossing_only(admin_env):
    """A series that falls below the threshold is told once, not again while it stays below,
    and again after it recovered and fell."""
    async with admin_env.client() as c:
        world = World(admin_env, c)
        a, b = await world.series("a"), await world.series("b")
        rule = await world.rule(
            name="Low score",
            kind="score",
            condition={"metric": "overall", "below": 70},
            channel="email",
            recipients=["ops@example.org"],
            throttle_minutes=0,
        )
        by_dimension = await world.rule(
            name="Gaps",
            kind="score",
            condition={"metric": "completeness", "below": 90},
            channel="email",
            recipients=["ops@example.org"],
            throttle_minutes=0,
        )

        async def stored(minute: int, overall_a: float, overall_b: float, completeness: float = 99.0) -> list:
            run = world.run()
            at = T0 + timedelta(minutes=minute)
            world.score(run, a, overall_a, at, completeness=completeness)
            world.score(run, b, overall_b, at, completeness=99.0)
            started = await world.evaluate(run, at)
            # Told at once: a notification still waiting would take the next matches instead.
            admin_env.sql("UPDATE alert_notifications SET status = 'sent' WHERE status = 'pending'")
            return started

        assert await stored(0, 90, 60) != []  # b has no score before and is below
        assert await stored(1, 90, 55) == []  # b stays below
        [fell] = await stored(2, 65, 55)  # a falls
        assert fell.rule_id == uuid.UUID(rule["id"])
        [item] = fell.items
        assert (item["series_name"], item["score"], item["previous"], item["below"]) == (
            "a",
            65.0,
            90.0,
            70.0,
        )
        assert await stored(3, 64, 80) == []  # b recovers, a stays
        [again] = await stored(4, 64, 50)
        assert [i["series_name"] for i in again.items] == ["b"]
        [gaps] = await stored(5, 64, 50, completeness=80.0)
        assert gaps.rule_id == uuid.UUID(by_dimension["id"]) and gaps.items[0]["metric"] == "completeness"


async def test_throttle_gathers_matches(admin_env, smtp):  # noqa: F811
    """The first match is told at once; further ones join one notification that is due the
    throttle after the one before. With throttle 0 every match is told at once."""
    async with admin_env.client() as c:
        world = World(admin_env, c)
        series = await world.series("feeder")
        rule = await world.rule(**EMAIL_RULE, throttle_minutes=60)
        eager = await world.rule(**{**EMAIL_RULE, "name": "Eager"}, throttle_minutes=0)

        async def match(minute: int, findings: int = 1) -> list:
            run = world.run()
            for _ in range(findings):
                world.finding(run, series)
            return await world.evaluate(run, T0 + timedelta(minutes=minute))

        first = await match(0)
        assert sorted(n.due_at for n in first) == [T0, T0]
        for n in first:
            assert (
                await delivery.deliver(world.factory, n.id, deps(smtp), final_attempt=True, now=T0) == "sent"
            )

        # Ten minutes later: the throttled rule waits until T0 + 60, the eager one tells at once.
        second = await match(10)
        assert {n.rule_id: n.due_at for n in second} == {
            uuid.UUID(rule["id"]): T0 + timedelta(minutes=60),
            uuid.UUID(eager["id"]): T0 + timedelta(minutes=10),
        }
        # Twenty minutes later: the waiting notification takes the new matches; nothing new starts
        # for it, while the eager rule's own waiting one does the same.
        third = await match(20, findings=60)
        assert third == []
        waiting = world.notifications(rule["id"])[-1]
        assert (waiting[1], waiting[2], waiting[3]) == ("pending", 61, T0 + timedelta(minutes=60))
        items = admin_env.scalar(
            "SELECT jsonb_array_length(items) FROM alert_notifications WHERE id = :i", i=waiting[0]
        )
        assert items == evaluate.MAX_ITEMS

        # Not due yet: nothing is sent. When due, one email names all 61.
        early = T0 + timedelta(minutes=30)
        assert (
            await delivery.deliver(world.factory, waiting[0], deps(smtp), final_attempt=True, now=early)
            is None
        )
        before = len(smtp.messages)
        due = T0 + timedelta(minutes=60)
        assert (
            await delivery.deliver(world.factory, waiting[0], deps(smtp), final_attempt=True, now=due)
            == "sent"
        )
        assert len(smtp.messages) == before + 1
        assert "61 new findings" in smtp.messages[-1]["Subject"] and "… and 41 more." in text_of(
            smtp.messages[-1]
        )
        # Sent twice is sent once.
        assert (
            await delivery.deliver(world.factory, waiting[0], deps(smtp), final_attempt=True, now=due) is None
        )
        # The next one counts from when the one before was sent.
        [fourth] = [n for n in await match(70) if n.rule_id == uuid.UUID(rule["id"])]
        assert fourth.due_at == T0 + timedelta(minutes=120)
        # Sent late (a worker was down): the throttle counts from the send, not the due time.
        late = T0 + timedelta(minutes=170)
        assert (
            await delivery.deliver(world.factory, fourth.id, deps(smtp), final_attempt=True, now=late)
            == "sent"
        )
        [fifth] = [n for n in await match(180) if n.rule_id == uuid.UUID(rule["id"])]
        assert fifth.due_at == late + timedelta(minutes=60)
        # One that failed told nobody and holds nothing back.
        admin_env.sql("UPDATE alert_notifications SET status = 'failed' WHERE id = :i", i=fifth.id)
        [sixth] = [n for n in await match(185) if n.rule_id == uuid.UUID(rule["id"])]
        assert sixth.due_at == late + timedelta(minutes=60)


async def test_deferred_delivery_is_queued_for_its_due_time(admin_env):
    """Without inline jobs the delivery job is queued in the evaluation's transaction, for when
    the notification is due."""
    async with admin_env.client() as c:
        world = World(admin_env, c)
        series = await world.series("feeder")
        await world.rule(**EMAIL_RULE, throttle_minutes=30)
        admin_env.sql("DELETE FROM procrastinate_jobs")
        for minute in (0, 5):
            run = world.run()
            world.finding(run, series)
            started = await world.evaluate(run, T0 + timedelta(minutes=minute), defer=True)
            admin_env.sql("UPDATE alert_notifications SET status = 'sent' WHERE id = :i", i=started[0].id)
    jobs = admin_env.sql(
        "SELECT task_name, queue_name, args, scheduled_at FROM procrastinate_jobs ORDER BY scheduled_at"
    )
    ids = [str(row[0]) for row in admin_env.sql("SELECT id FROM alert_notifications ORDER BY due_at")]
    assert [(j[0], j[1]) for j in jobs] == [("tabayyun.deliver_alert", "mail")] * 2
    assert [j[2] for j in jobs] == [{"notification_id": i, "org_id": str(DEFAULT_ORG_ID)} for i in ids]
    assert [j[3] for j in jobs] == [T0, T0 + timedelta(minutes=30)]


async def test_webhook_delivery_headers_and_signature(admin_env):
    """A webhook gets the payload with the delivery headers, signed when the rule has a secret."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        # The answer's body is never read: a receiver cannot hold a delivery with one.
        return httpx.Response(204, stream=Unread())

    keyring = Keyring(base64.b64decode(KEY))
    async with admin_env.client(master_key=KEY) as c:
        world = World(admin_env, c)
        series = await world.series("feeder", "plant/feeder-4")
        signed = await world.rule(**HOOK_RULE, secret="s3cret-value")
        plain = await world.rule(**{**HOOK_RULE, "name": "Plain"})
        assert (signed["has_secret"], plain["has_secret"]) == (True, False)
        run = world.run()
        world.finding(run, series)
        started = await world.evaluate(run, T0)
        for n in started:
            status = await delivery.deliver(
                world.factory, n.id, deps(net=hook_net(handler), keyring=keyring), final_attempt=True, now=T0
            )
            assert status == "sent"
    by_delivery = {r.headers["X-Tabayyun-Delivery"]: r for r in seen}
    assert set(by_delivery) == {str(n.id) for n in started}
    for n in started:
        request = by_delivery[str(n.id)]
        assert (
            request.method == "POST"
            and request.url.path == "/t/abc"
            and request.headers["host"] == "hooks.example.org"
        )
        assert request.url.host == PUBLIC  # pinned to the checked address
        assert request.headers["content-type"] == "application/json"
        assert request.headers["user-agent"].startswith("Tabayyun/")
        assert request.headers["x-tabayyun-event"] == "alert"
        payload = json.loads(request.content)
        assert (
            payload["delivery"] == str(n.id) and payload["n_matches"] == 1 and payload["truncated"] is False
        )
        assert payload["workspace"]["id"] == str(DEFAULT_WORKSPACE_ID)
        assert payload["matches"][0]["asset_path"] == "plant/feeder-4" and payload["matches"][0][
            "run_id"
        ] == str(run)
        assert payload["url"] == "https://tabayyun.example.org/findings"
        if n.rule_id == uuid.UUID(signed["id"]):
            expected = hmac.new(b"s3cret-value", request.content, hashlib.sha256).hexdigest()
            assert request.headers["x-tabayyun-signature"] == f"sha256={expected}"
        else:
            assert "x-tabayyun-signature" not in request.headers
    # The secret is stored sealed, never in the clear.
    stored = admin_env.sql("SELECT secret_ciphertext, url FROM alert_rules WHERE id = :i", i=signed["id"])[0]
    assert b"s3cret-value" not in bytes(stored[0])


async def test_webhook_refused_address_redirect_and_missing_key(admin_env):
    """A refused address is not contacted, a redirect is not followed, a 4xx is final, and a
    signed rule is not sent unsigned when its secret cannot be read."""
    calls: list[str] = []

    def redirecting(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://internal.example.org/"})

    def gone(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(410)

    async def loopback(host: str, port: int) -> list[str]:
        return ["127.0.0.1"]

    keyring = Keyring(base64.b64decode(KEY))
    async with admin_env.client(master_key=KEY) as c:
        world = World(admin_env, c)
        series = await world.series("feeder")
        await world.rule(**HOOK_RULE, secret="s3cret-value", throttle_minutes=0)

        async def fresh() -> uuid.UUID:
            run = world.run()
            world.finding(run, series)
            [started] = await world.evaluate(run, T0)
            return started.id

        async def outcome(net: NetPolicy, ring: Keyring | None = keyring) -> tuple:
            notification = await fresh()
            status = await delivery.deliver(
                world.factory, notification, deps(net=net, keyring=ring), final_attempt=False, now=T0
            )
            return status, admin_env.scalar(
                "SELECT error FROM alert_notifications WHERE id = :i", i=notification
            )

        refused = NetPolicy(resolver=loopback, transport=httpx.MockTransport(gone))
        assert await outcome(refused) == ("failed", "target not allowed: hooks.example.org")
        assert calls == []
        assert await outcome(hook_net(redirecting)) == ("failed", "the webhook answered 302")
        assert len(calls) == 1
        assert await outcome(hook_net(gone)) == ("failed", "the webhook answered 410")
        status, error = await outcome(hook_net(gone), ring=None)
        assert status == "failed" and error.startswith("the signing secret cannot be read")
        other = Keyring(os.urandom(32))
        status, error = await outcome(hook_net(gone), ring=other)
        assert status == "failed" and error.startswith("the signing secret cannot be read")
        assert len(calls) == 2


async def test_retries_then_failed_and_stuck_sending_is_retaken(admin_env, smtp):  # noqa: F811
    """A send that may pass later is tried again and ends `failed` with the reason on the
    last attempt; a notification a dead worker left `sending` is taken again after ten minutes."""
    answers = [503, 503, 503]

    def flaky(request: httpx.Request) -> httpx.Response:
        return httpx.Response(answers.pop(0))

    async with admin_env.client() as c:
        world = World(admin_env, c)
        series = await world.series("feeder")
        hook = await world.rule(**HOOK_RULE)
        mailed = await world.rule(**EMAIL_RULE)
        run = world.run()
        world.finding(run, series)
        started = {n.rule_id: n.id for n in await world.evaluate(run, T0)}
        hook_id, mail_id = started[uuid.UUID(hook["id"])], started[uuid.UUID(mailed["id"])]
        net = hook_net(flaky)

        for attempt in (1, 2):
            with pytest.raises(delivery.DeliveryError, match="the webhook answered 503"):
                await delivery.deliver(world.factory, hook_id, deps(net=net), final_attempt=False, now=T0)
            assert world.notifications(hook["id"])[0][1:6:3] == ("pending", attempt)
        assert (
            await delivery.deliver(world.factory, hook_id, deps(net=net), final_attempt=True, now=T0)
            == "failed"
        )
        row = world.notifications(hook["id"])[0]
        assert (row[1], row[4], row[5]) == ("failed", 3, "the webhook answered 503")

        # The SMTP server refuses the recipient: retried, then failed with the server's words.
        smtp.refuse.add("ops@example.org")
        with pytest.raises(delivery.DeliveryError):
            await delivery.deliver(world.factory, mail_id, deps(smtp), final_attempt=False, now=T0)
        assert (
            await delivery.deliver(world.factory, mail_id, deps(smtp), final_attempt=True, now=T0) == "failed"
        )
        assert world.notifications(mailed["id"])[0][5].startswith("550 ")
        smtp.refuse.clear()

        # A worker died while sending: the notification is taken again after ten minutes.
        admin_env.sql(
            "UPDATE alert_notifications SET status = 'sending', claimed_at = :t, attempts = 1 WHERE id = :i",
            t=T0,
            i=mail_id,
        )
        soon, late = T0 + timedelta(minutes=9), T0 + timedelta(minutes=10)
        assert (
            await delivery.deliver(world.factory, mail_id, deps(smtp), final_attempt=True, now=soon) is None
        )
        assert smtp.messages == []
        assert (
            await delivery.deliver(world.factory, mail_id, deps(smtp), final_attempt=True, now=late) == "sent"
        )
        assert len(smtp.messages) == 1 and world.notifications(mailed["id"])[0][4] == 2


async def test_email_not_configured_is_skipped(admin_env, smtp):  # noqa: F811
    """Without SMTP an email notification is skipped with the reason, and the channels route
    says what the server can do."""
    async with admin_env.client() as c:
        world = World(admin_env, c)
        series = await world.series("feeder")
        rule = await world.rule(**EMAIL_RULE)
        assert (await c.get("/api/alerts/channels")).json() == {
            "email": {"available": False},
            "webhook": {"available": True, "signing": False},
        }
        run = world.run()
        world.finding(run, series)
        [started] = await world.evaluate(run, T0)
        assert (
            await delivery.deliver(world.factory, started.id, deps(), final_attempt=False, now=T0)
            == "skipped"
        )
        row = world.notifications(rule["id"])[0]
        assert (row[1], row[5]) == ("skipped", "email is not configured on this server")
    async with admin_env.client(MIA, master_key=KEY, **mail_settings(smtp)) as c:
        assert (await c.get("/api/alerts/channels")).json() == {
            "email": {"available": True},
            "webhook": {"available": True, "signing": True},
        }


async def test_routes_validation_roles_and_url_visibility(admin_env):
    """Rules are validated as a whole; only workspace admins change them or see a webhook's
    URL; other workspaces and orgs do not see them at all."""
    async with admin_env.client(master_key=KEY) as owner:
        bad = await owner.post("/api/alerts", json={**EMAIL_RULE, "name": "", "recipients": ["nope"]})
        assert bad.status_code == 422
        assert {e["field"] for e in bad.json()["detail"]} == {"name", "recipients"}
        assert (await owner.post("/api/alerts", json={**EMAIL_RULE, "extra": 1})).status_code == 422
        mail = (await owner.post("/api/alerts", json=EMAIL_RULE)).json()
        assert mail["condition"] == {
            "min_severity": "critical",
            "check_ids": [],
            "dimensions": [],
            "asset_prefix": None,
        }
        assert (mail["throttle_minutes"], mail["enabled"], mail["last_notification"]) == (60, True, None)
        taken = await owner.post("/api/alerts", json=EMAIL_RULE)
        assert (taken.status_code, taken.json()) == (409, {"detail": "name_taken"})
        hook = (await owner.post("/api/alerts", json={**HOOK_RULE, "secret": "s3cret-value"})).json()
        assert (hook["url"], hook["url_host"], hook["has_secret"]) == (
            HOOK_RULE["url"],
            "hooks.example.org",
            True,
        )
        assert "secret" not in hook

        # A change is validated together with what stays.
        wrong = await owner.patch(f"/api/alerts/{mail['id']}", json={"channel": "webhook"})
        assert wrong.status_code == 422 and wrong.json()["detail"][0]["field"] == "url"
        moved = await owner.patch(
            f"/api/alerts/{mail['id']}", json={"channel": "webhook", "url": "https://example.org/h"}
        )
        assert moved.status_code == 200 and moved.json()["recipients"] == []
        back = await owner.patch(
            f"/api/alerts/{mail['id']}",
            json={"channel": "email", "recipients": ["a@example.org"], "name": " Renamed "},
        )
        assert (back.json()["name"], back.json()["url"], back.json()["channel"]) == ("Renamed", None, "email")
        assert (await owner.patch(f"/api/alerts/{mail['id']}", json={"name": "Hook"})).json() == {
            "detail": "name_taken"
        }
        assert (
            await owner.patch(f"/api/alerts/{mail['id']}", json={"secret": "s3cret-value"})
        ).status_code == 422
        # The secret is kept unless the request names it; null removes it.
        kept = (await owner.patch(f"/api/alerts/{hook['id']}", json={"throttle_minutes": 5})).json()
        assert (kept["has_secret"], kept["throttle_minutes"]) == (True, 5)
        cleared = (await owner.patch(f"/api/alerts/{hook['id']}", json={"secret": None})).json()
        assert cleared["has_secret"] is False
        listed = (await owner.get("/api/alerts")).json()
        assert [r["name"] for r in listed["items"]] == ["Hook", "Renamed"] and listed["next_cursor"] is None
        assert (await owner.get("/api/alerts/not-a-uuid")).status_code == 404

    async with admin_env.client() as no_key:
        refused = await no_key.patch(f"/api/alerts/{hook['id']}", json={"secret": "s3cret-value"})
        assert (refused.status_code, refused.json()) == (409, {"detail": "credentials_unavailable"})

    async with admin_env.client(ADAM) as admin:
        assert (await admin.get(f"/api/alerts/{hook['id']}")).json()["url"] == HOOK_RULE["url"]
    async with admin_env.client(MIA) as viewer:
        seen = (await viewer.get(f"/api/alerts/{hook['id']}")).json()
        assert (seen["url"], seen["url_host"]) == (None, "hooks.example.org")
        assert all(r["url"] is None for r in (await viewer.get("/api/alerts")).json()["items"])
        assert (await viewer.get(f"/api/alerts/{hook['id']}/notifications")).json() == {"items": []}
        for method, path, body in (
            ("post", "/api/alerts", EMAIL_RULE),
            ("patch", f"/api/alerts/{hook['id']}", {"enabled": False}),
            ("delete", f"/api/alerts/{hook['id']}", None),
            ("post", f"/api/alerts/{hook['id']}/test", None),
        ):
            answer = await getattr(viewer, method)(path, **({} if body is None else {"json": body}))
            assert answer.status_code == 403, (method, path, answer.text)
    async with admin_env.client(NED) as stranger:
        assert (await stranger.get("/api/alerts")).status_code == 404
    async with admin_env.client(BOB, org=ORG_B) as other:
        assert (await other.get("/api/alerts")).json()["items"] == []
        for method, path in (
            ("get", ""),
            ("patch", ""),
            ("delete", ""),
            ("get", "/notifications"),
            ("post", "/test"),
        ):
            answer = await getattr(other, method)(
                f"/api/alerts/{hook['id']}{path}",
                **({"json": {"enabled": False}} if method == "patch" else {}),
            )
            assert answer.status_code == 404, (method, path)

    async with admin_env.client() as owner:
        assert (await owner.delete(f"/api/alerts/{hook['id']}")).status_code == 204
        assert (await owner.get(f"/api/alerts/{hook['id']}")).status_code == 404


async def test_test_notification_and_audit(admin_env, smtp):  # noqa: F811
    """ "Send test" delivers at once whatever the throttle says and does not count for it;
    every change of a rule is audited without the secret or the full URL."""
    async with admin_env.client(master_key=KEY, **mail_settings(smtp)) as c:
        world = World(admin_env, c)
        series = await world.series("feeder")
        rule = await world.rule(**EMAIL_RULE, throttle_minutes=600)
        for _ in range(2):
            sent = await c.post(f"/api/alerts/{rule['id']}/test")
            assert sent.status_code == 202 and uuid.UUID(sent.json()["notification_id"])
        assert [m["Subject"] for m in smtp.messages] == [
            "[Tabayyun] Critical findings: test notification"
        ] * 2
        assert "Example series" in text_of(smtp.messages[0])
        history = (await c.get(f"/api/alerts/{rule['id']}/notifications")).json()["items"]
        assert [(n["kind"], n["status"]) for n in history] == [("test", "sent")] * 2
        # A real match after the tests is still due at once.
        run = world.run()
        world.finding(run, series)
        [started] = await world.evaluate(run, T0)
        assert started.due_at == T0

        hook = await world.rule(**HOOK_RULE, secret="s3cret-value")
        await c.patch(f"/api/alerts/{hook['id']}", json={"secret": "another-secret", "enabled": False})
        await c.patch(f"/api/alerts/{hook['id']}", json={"enabled": False})  # nothing changes: no event
        await c.delete(f"/api/alerts/{hook['id']}")
    events = [e for e in admin_env.events() if e.action.startswith("alert_rule.")]
    assert [e.action for e in events] == [
        "alert_rule.created",
        "alert_rule.tested",
        "alert_rule.tested",
        "alert_rule.created",
        "alert_rule.updated",
        "alert_rule.deleted",
    ]
    assert events[0].details["recipients"] == ["ops@example.org"] and events[0].target_id == rule["id"]
    created, updated = events[3].details, events[4].details
    assert (created["url_host"], created["has_secret"]) == ("hooks.example.org", True)
    assert updated["secret_changed"] is True and updated["after"]["enabled"] is False
    whole = json.dumps([e.details for e in events])
    assert "secret-value" not in whole and "another-secret" not in whole and "/t/abc" not in whole


async def test_rotation_reseals_rule_secrets(admin_env, db_url):
    """Key rotation re-encrypts the rules' secrets under the new key; they still open."""
    old, new = os.urandom(32), os.urandom(32)
    async with admin_env.client(master_key=base64.b64encode(old).decode()) as c:
        world = World(admin_env, c)
        rule = await world.rule(**HOOK_RULE, secret="s3cret-value")
        await world.rule(**{**HOOK_RULE, "name": "Plain"})
    ring = Keyring(new, old)
    engine = create_async_engine(db_url)
    try:
        async with async_sessionmaker(engine)() as session, session.begin():
            assert await rules.rotate_secrets(session, ring) == 1
        async with async_sessionmaker(engine)() as session, session.begin():
            assert await rules.rotate_secrets(session, ring) == 0
            stored = await session.get(rules.AlertRule, uuid.UUID(rule["id"]))
            assert stored is not None and stored.secret_key_id == Keyring(new).current_id
            assert rules.open_secret(stored, Keyring(new)) == "s3cret-value"
    finally:
        await engine.dispose()


async def test_evaluation_error_does_not_fail_the_run(admin_env, monkeypatch):
    """Whatever goes wrong while evaluating or delivering, the run stays succeeded."""

    async def broken(*args, **kwargs):
        raise RuntimeError("alerting is broken")

    async with admin_env.client() as c:
        await c.post("/api/alerts", json={**EMAIL_RULE, "condition": {"min_severity": "low"}})
        monkeypatch.setattr(evaluate, "evaluate_run", broken)
        r = await c.post(
            "/api/runs",
            files={"file": ("f.csv", faulty_csv(["gap"]), "text/csv")},
            data={"series_id": "demo"},
        )
        run = (await c.get(f"/api/runs/{r.json()['id']}")).json()
    assert run["status"] == "succeeded" and run["stats"]["n_findings_new"] > 0
    assert admin_env.scalar("SELECT count(*) FROM alert_notifications") == 0


async def test_inline_mode_queues_what_is_due_later(admin_env):
    """Without `defer` the caller delivers what is due now; a notification due later still
    gets its job, or nothing would ever deliver it."""
    async with admin_env.client() as c:
        world = World(admin_env, c)
        series = await world.series("feeder")
        await world.rule(**EMAIL_RULE, throttle_minutes=30)
        admin_env.sql("DELETE FROM procrastinate_jobs")
        for minute in (0, 5):
            run = world.run()
            world.finding(run, series)
            [started] = await world.evaluate(run, T0 + timedelta(minutes=minute))
            admin_env.sql("UPDATE alert_notifications SET status = 'sent' WHERE id = :i", i=started.id)
    jobs = admin_env.sql("SELECT args, scheduled_at FROM procrastinate_jobs")
    assert jobs == [
        ({"notification_id": str(started.id), "org_id": str(DEFAULT_ORG_ID)}, T0 + timedelta(minutes=30))
    ]


async def test_unexpected_send_error_is_recorded(admin_env, smtp, monkeypatch):  # noqa: F811
    """An error the send does not expect ends the notification `failed` with the reason; it
    is not left `sending`."""

    async def broken(*args, **kwargs):
        raise TypeError("cannot format this")

    async with admin_env.client() as c:
        world = World(admin_env, c)
        series = await world.series("feeder")
        rule = await world.rule(**EMAIL_RULE)
        run = world.run()
        world.finding(run, series)
        [started] = await world.evaluate(run, T0)
        monkeypatch.setattr(delivery, "_send_email", broken)
        assert (
            await delivery.deliver(world.factory, started.id, deps(smtp), final_attempt=False, now=T0)
            == "failed"
        )
        row = world.notifications(rule["id"])[0]
        assert (row[1], row[5]) == ("failed", "TypeError: cannot format this")


async def test_stuck_notifications_are_requeued(admin_env, smtp):  # noqa: F811
    """The periodic task puts a notification a lost worker left `sending` back to `pending`
    and queues its delivery, across orgs; a fresh claim is left alone, and the retaken one
    ends after its own third attempt."""
    async with admin_env.client() as c:
        world = World(admin_env, c)
        series = await world.series("feeder")
        rule = await world.rule(**EMAIL_RULE, throttle_minutes=0)
        ids = []
        for claimed in (T0, T0 + timedelta(minutes=9)):
            run = world.run()
            world.finding(run, series)
            [started] = await world.evaluate(run, T0)
            admin_env.sql(
                "UPDATE alert_notifications SET status = 'sending', claimed_at = :t, attempts = 2 "
                "WHERE id = :i",
                t=claimed,
                i=started.id,
            )
            ids.append(started.id)
        admin_env.sql("DELETE FROM procrastinate_jobs")
        now = T0 + timedelta(minutes=12)
        assert await delivery.requeue_stuck(world.app.state.session_factory, now) == 1
        assert await delivery.requeue_stuck(world.app.state.session_factory, now) == 0
        statuses = dict(admin_env.sql("SELECT id, status FROM alert_notifications"))
        assert (statuses[ids[0]], statuses[ids[1]]) == ("pending", "sending")
        jobs = admin_env.sql("SELECT task_name, queue_name, args FROM procrastinate_jobs")
        assert jobs == [
            (
                "tabayyun.deliver_alert",
                "mail",
                {"notification_id": str(ids[0]), "org_id": str(DEFAULT_ORG_ID)},
            )
        ]
        # The new job counts its attempts from zero; the notification's own count ends them.
        smtp.refuse.add("ops@example.org")
        assert (
            await delivery.deliver(world.factory, ids[0], deps(smtp), final_attempt=False, now=now)
            == "failed"
        )
        assert world.notifications(rule["id"])[0][4] == 3


async def test_late_worker_does_not_overwrite_a_later_claim(admin_env, smtp, monkeypatch):  # noqa: F811
    """A send that outlasts its claim: the notification is taken again and sent by another
    worker. When the first one comes back with a failure, its outcome is dropped: `sent`
    stays, and nothing is sent again."""
    async with admin_env.client() as c:
        world = World(admin_env, c)
        series = await world.series("feeder")
        rule = await world.rule(**EMAIL_RULE)
        run = world.run()
        world.finding(run, series)
        [started] = await world.evaluate(run, T0)
        later = T0 + delivery.STUCK_AFTER
        real_send = delivery._send_email

        async def slow_then_refused(claimed, dependencies):
            # While the first worker is still sending, the second takes the notification over.
            monkeypatch.setattr(delivery, "_send_email", real_send)
            second = await delivery.deliver(
                world.factory, started.id, deps(smtp), final_attempt=False, now=later
            )
            assert second == "sent"
            return delivery._Outcome("failed", "421 try again later", retry=True)

        monkeypatch.setattr(delivery, "_send_email", slow_then_refused)
        first = await delivery.deliver(world.factory, started.id, deps(smtp), final_attempt=False, now=T0)
        assert first is None
        row = world.notifications(rule["id"])[0]
        assert (row[1], row[4], row[5]) == ("sent", 2, None)
        assert len(smtp.messages) == 1


async def test_throttle_is_kept_when_the_one_before_is_sent_late(admin_env, smtp):  # noqa: F811
    """A match while the notification before is being sent, late: the next one is planned
    from its due time, and the delivery moves it to a throttle after the real send."""
    async with admin_env.client() as c:
        world = World(admin_env, c)
        series = await world.series("feeder")
        rule = await world.rule(**EMAIL_RULE, throttle_minutes=60)

        async def match(minute: int):
            run = world.run()
            world.finding(run, series)
            return await world.evaluate(run, T0 + timedelta(minutes=minute))

        [first] = await match(0)
        sent_late = T0 + timedelta(minutes=55)
        admin_env.sql(
            "UPDATE alert_notifications SET status = 'sending', claimed_at = :t, attempts = 1 WHERE id = :i",
            t=sent_late,
            i=first.id,
        )
        [second] = await match(56)
        assert second.due_at == T0 + timedelta(minutes=60)
        admin_env.sql(
            "UPDATE alert_notifications SET status = 'sent', sent_at = :t WHERE id = :i",
            t=sent_late,
            i=first.id,
        )
        admin_env.sql("DELETE FROM procrastinate_jobs")
        at_plan, at_last = T0 + timedelta(minutes=60), sent_late + timedelta(minutes=60)
        assert (
            await delivery.deliver(world.factory, second.id, deps(smtp), final_attempt=False, now=at_plan)
            is None
        )
        row = world.notifications(rule["id"])[1]
        assert (row[1], row[3], row[4]) == ("pending", at_last, 0) and smtp.messages == []
        assert [j[0] for j in admin_env.sql("SELECT scheduled_at FROM procrastinate_jobs")] == [at_last]
        # The send itself takes time: the throttle counts from when it was done.
        done = at_last + timedelta(seconds=8)
        sent = await delivery.deliver(
            world.factory, second.id, deps(smtp), final_attempt=False, now=at_last, clock=lambda: done
        )
        assert sent == "sent" and len(smtp.messages) == 1
        assert admin_env.scalar("SELECT sent_at FROM alert_notifications WHERE id = :i", i=second.id) == done
        [third] = await match(116)
        assert third.due_at == done + timedelta(minutes=60)


async def test_retry_of_the_one_before_holds_the_next_back(admin_env, smtp):  # noqa: F811
    """A notification comes back for a retry after the next one was started: two wait. New
    matches join the later one, and it goes a throttle after the first was really sent."""
    async with admin_env.client() as c:
        world = World(admin_env, c)
        series = await world.series("feeder")
        rule = await world.rule(**EMAIL_RULE, throttle_minutes=60)

        async def match(minute: int):
            run = world.run()
            world.finding(run, series)
            return await world.evaluate(run, T0 + timedelta(minutes=minute))

        def minutes(n: int) -> datetime:
            return T0 + timedelta(minutes=n)

        [first] = await match(0)
        smtp.refuse.add("ops@example.org")
        admin_env.sql(
            "UPDATE alert_notifications SET status = 'sending', claimed_at = :t, attempts = 1 WHERE id = :i",
            t=T0,
            i=first.id,
        )
        [second] = await match(1)  # started while the first is being sent
        admin_env.sql(
            "UPDATE alert_notifications SET status = 'pending' WHERE id = :i", i=first.id
        )  # its retry
        assert await match(2) == []  # joins the later one
        rows = world.notifications(rule["id"])
        assert [(r[1], r[2]) for r in rows] == [("pending", 1), ("pending", 2)]

        # The second is due while the first still waits for its retry: it is held back.
        assert (
            await delivery.deliver(world.factory, second.id, deps(smtp), final_attempt=False, now=minutes(60))
            is None
        )
        assert world.notifications(rule["id"])[1][3] == minutes(120)
        smtp.refuse.clear()
        assert (
            await delivery.deliver(world.factory, first.id, deps(smtp), final_attempt=False, now=minutes(90))
            == "sent"
        )
        # At its new time the first was sent 30 minutes ago: held back to a throttle after that.
        assert (
            await delivery.deliver(
                world.factory, second.id, deps(smtp), final_attempt=False, now=minutes(120)
            )
            is None
        )
        assert world.notifications(rule["id"])[1][3] == minutes(150)
        assert (
            await delivery.deliver(
                world.factory, second.id, deps(smtp), final_attempt=False, now=minutes(150)
            )
            == "sent"
        )
        assert ["1 new finding" in m["Subject"] for m in smtp.messages] == [True, False]


async def test_a_rules_kind_cannot_change(admin_env):
    """What a rule watches is fixed: its notifications hold matches of that kind. The change
    is refused at `kind` and nothing of the rule or its waiting notification changes."""
    async with admin_env.client() as c:
        world = World(admin_env, c)
        series = await world.series("feeder")
        rule = await world.rule(**EMAIL_RULE)
        run = world.run()
        world.finding(run, series)
        await world.evaluate(run, T0)
        refused = await c.patch(
            f"/api/alerts/{rule['id']}",
            json={"kind": "score", "condition": {"metric": "overall", "below": 70}, "name": "Other"},
        )
        assert refused.status_code == 422
        assert refused.json()["detail"] == [{"field": "kind", "message": rules.KIND_FIXED}]
        same = await c.patch(f"/api/alerts/{rule['id']}", json={"kind": "finding", "throttle_minutes": 5})
        assert same.status_code == 200
        stored = (await c.get(f"/api/alerts/{rule['id']}")).json()
        assert (stored["kind"], stored["name"], stored["throttle_minutes"]) == (
            "finding",
            EMAIL_RULE["name"],
            5,
        )
        assert world.notifications(rule["id"])[0][1] == "pending"


async def test_inline_delivery_records_when_it_was_sent(admin_env, smtp, monkeypatch):  # noqa: F811
    """The inline path takes what is due by the evaluation's time and, like the worker,
    records the time after the send as `sent_at`."""
    async with admin_env.client() as c:
        world = World(admin_env, c)
        series = await world.series("feeder")
        rule = await world.rule(**EMAIL_RULE)
        run = world.run()
        world.finding(run, series)
        started = await world.evaluate(run, T0)
        done = T0 + timedelta(seconds=8)
        monkeypatch.setattr(delivery, "_now", lambda: done)
        await delivery.deliver_inline(world.factory, started, deps(smtp), T0)
        assert (
            admin_env.scalar("SELECT sent_at FROM alert_notifications WHERE rule_id = :r", r=rule["id"])
            == done
        )
    assert len(smtp.messages) == 1


async def test_a_send_in_flight_holds_an_earlier_one_back(admin_env, smtp):  # noqa: F811
    """Whatever its due time, a notification that is being sent right now holds the rule's
    others back: its send time is what the throttle will count from."""
    async with admin_env.client() as c:
        world = World(admin_env, c)
        series = await world.series("feeder")
        rule = await world.rule(**EMAIL_RULE, throttle_minutes=60)
        run = world.run()
        world.finding(run, series)
        [waiting] = await world.evaluate(run, T0)
        # Another one of the rule, due later, is being sent at this moment.
        admin_env.sql(
            "INSERT INTO alert_notifications (id, org_id, workspace_id, rule_id, kind, status, due_at, "
            "n_matches, items, attempts, claimed_at, created_at) "
            "VALUES (gen_random_uuid(), :o, :w, :r, 'alert', 'sending', :due, 1, '[]'::jsonb, 1, :now, :now)",
            o=DEFAULT_ORG_ID,
            w=DEFAULT_WORKSPACE_ID,
            r=rule["id"],
            due=T0 + timedelta(minutes=5),
            now=T0 + timedelta(minutes=6),
        )
        at = T0 + timedelta(minutes=6)
        assert (
            await delivery.deliver(world.factory, waiting.id, deps(smtp), final_attempt=False, now=at) is None
        )
        held = admin_env.sql("SELECT status, due_at FROM alert_notifications WHERE id = :i", i=waiting.id)[0]
        assert tuple(held) == ("pending", at + timedelta(minutes=60))
    assert smtp.messages == []


async def test_webhook_has_an_overall_deadline(admin_env, monkeypatch):
    """A receiver that keeps the call open is given up on after the deadline, long before the
    notification would count as left behind; the send is tried again."""
    import asyncio

    async def stalls(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(204)

    monkeypatch.setattr(delivery, "WEBHOOK_DEADLINE_S", 0.05)
    assert timedelta(seconds=30) < delivery.STUCK_AFTER
    async with admin_env.client() as c:
        world = World(admin_env, c)
        series = await world.series("feeder")
        rule = await world.rule(**HOOK_RULE)
        run = world.run()
        world.finding(run, series)
        [started] = await world.evaluate(run, T0)
        with pytest.raises(delivery.DeliveryError, match="no answer within 0.05 s"):
            await delivery.deliver(
                world.factory, started.id, deps(net=hook_net(stalls)), final_attempt=False, now=T0
            )
        row = world.notifications(rule["id"])[0]
        assert (row[1], row[4], row[5]) == ("pending", 1, "no answer within 0.05 s")
