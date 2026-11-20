"""Alert rules without a database (spec 031): validation, matching and what a notification says."""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime

import pytest

from tabayyun.alerts import messages, rules
from tabayyun.alerts.matching import ScoreValues, finding_matches, score_crossing

FINDING = {"min_severity": "high", "check_ids": [], "dimensions": [], "asset_prefix": None}


def _rule(**changes) -> rules.RuleData:
    base = {
        "name": "Critical findings",
        "kind": "finding",
        "condition": dict(FINDING),
        "channel": "email",
        "recipients": ["Ops@Example.org"],
    }
    return rules.RuleData(**(base | changes))


def test_validate_normalises_a_good_rule():
    """Names are trimmed, addresses lower-cased and de-duplicated, the condition completed."""
    rule = rules.validate(
        _rule(
            name="  Critical findings ",
            recipients=["Ops@Example.org", "ops@example.org", "b@example.org"],
            condition={"min_severity": "critical", "asset_prefix": " plant/feeder "},
        )
    )
    assert rule.name == "Critical findings"
    assert rule.recipients == ["ops@example.org", "b@example.org"]
    assert rule.condition == {
        "min_severity": "critical",
        "check_ids": [],
        "dimensions": [],
        "asset_prefix": "plant/feeder",
    }
    assert (rule.url, rule.throttle_minutes, rule.enabled) == (None, 60, True)

    score = rules.validate(
        _rule(
            kind="score",
            condition={"metric": "completeness", "below": 70},
            channel="webhook",
            recipients=[],
            url=" https://hooks.example.org/t/abc ",
            throttle_minutes=0,
        )
    )
    assert score.condition == {"metric": "completeness", "below": 70.0}
    assert (score.url, score.recipients, score.throttle_minutes) == ("https://hooks.example.org/t/abc", [], 0)


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"name": "  "}, "name"),
        ({"name": "x" * 121}, "name"),
        ({"name": "Critical\nfeeders"}, "name"),
        ({"name": "Tab\there"}, "name"),
        ({"kind": "other"}, "kind"),
        ({"condition": {"min_severity": "urgent"}}, "condition.min_severity"),
        ({"condition": {**FINDING, "check_ids": ["tby.no_such_check"]}}, "condition.check_ids"),
        ({"condition": {**FINDING, "dimensions": ["speed"]}}, "condition.dimensions"),
        ({"condition": {**FINDING, "asset_prefix": "x" * 201}}, "condition.asset_prefix"),
        ({"condition": {**FINDING, "extra": 1}}, "condition.extra"),
        ({"kind": "score", "condition": {"metric": "speed", "below": 50}}, "condition.metric"),
        ({"kind": "score", "condition": {"metric": "overall", "below": 101}}, "condition.below"),
        ({"kind": "score", "condition": {"metric": "overall", "below": True}}, "condition.below"),
        ({"kind": "score", "condition": {"metric": "overall"}}, "condition.below"),
        ({"channel": "pager"}, "channel"),
        ({"recipients": []}, "recipients"),
        ({"recipients": [f"a{n}@example.org" for n in range(11)]}, "recipients"),
        ({"recipients": ["not an address"]}, "recipients"),
        ({"url": "https://example.org/hook"}, "url"),
        ({"channel": "webhook", "recipients": [], "url": None}, "url"),
        ({"channel": "webhook", "recipients": [], "url": "http://example.org/hook"}, "url"),
        ({"channel": "webhook", "recipients": [], "url": "https://user:pw@example.org/hook"}, "url"),
        ({"channel": "webhook", "recipients": [], "url": "https://["}, "url"),
        ({"channel": "webhook", "url": "https://example.org/hook"}, "recipients"),
        ({"throttle_minutes": -1}, "throttle_minutes"),
        ({"throttle_minutes": 1441}, "throttle_minutes"),
        ({"throttle_minutes": 1.5}, "throttle_minutes"),
    ],
)
def test_validate_names_the_wrong_field(changes, field):
    with pytest.raises(rules.RuleError) as refused:
        rules.validate(_rule(**changes))
    assert field in [e["field"] for e in refused.value.errors]


def test_validate_reports_every_wrong_field_at_once():
    with pytest.raises(rules.RuleError) as refused:
        rules.validate(_rule(name="", recipients=["x"], throttle_minutes=9999))
    assert {e["field"] for e in refused.value.errors} == {"name", "recipients", "throttle_minutes"}


def test_secret_rules():
    """A secret belongs to a webhook and has 8 to 200 characters."""
    assert rules.validate_secret("s3cret-value", "webhook") == "s3cret-value"
    for secret, channel in (("short", "webhook"), ("x" * 201, "webhook"), ("long-enough", "email")):
        with pytest.raises(rules.RuleError) as refused:
            rules.validate_secret(secret, channel)
        assert refused.value.errors[0]["field"] == "secret"


def test_finding_condition_matches():
    """Severity is a floor; checks, dimensions and the asset prefix narrow only when given."""
    finding = {
        "severity": "high",
        "check_id": "tby.flatline",
        "dimension": "plausibility",
        "asset_path": "plant/feeder-4",
    }
    assert finding_matches(FINDING, **finding)
    assert finding_matches({**FINDING, "min_severity": "low"}, **finding)
    assert not finding_matches({**FINDING, "min_severity": "critical"}, **finding)
    assert finding_matches({**FINDING, "check_ids": ["tby.flatline", "tby.spikes"]}, **finding)
    assert not finding_matches({**FINDING, "check_ids": ["tby.spikes"]}, **finding)
    assert finding_matches({**FINDING, "dimensions": ["plausibility"]}, **finding)
    assert not finding_matches({**FINDING, "dimensions": ["completeness"]}, **finding)
    assert finding_matches({**FINDING, "asset_prefix": "plant/"}, **finding)
    assert not finding_matches({**FINDING, "asset_prefix": "mill/"}, **finding)
    assert not finding_matches({**FINDING, "asset_prefix": "plant/"}, **{**finding, "asset_path": None})


def test_score_crossing():
    """A score rule tells that a series fell below the threshold, not that it is still there."""
    rule = {"metric": "overall", "below": 70.0}
    good, bad, worse = ScoreValues(82.0, {}), ScoreValues(64.0, {}), ScoreValues(41.0, {})
    crossing = score_crossing(rule, bad, good)
    assert crossing is not None
    assert (crossing.score, crossing.previous, crossing.below) == (64.0, 82.0, 70.0)
    first = score_crossing(rule, bad, None)
    assert first is not None and first.previous is None
    assert score_crossing(rule, worse, bad) is None
    assert score_crossing(rule, good, bad) is None
    assert score_crossing(rule, ScoreValues(70.0, {}), good) is None

    by_dimension = {"metric": "completeness", "below": 90.0}
    now, before = ScoreValues(95.0, {"completeness": 80.0}), ScoreValues(95.0, {"completeness": 99.0})
    found = score_crossing(by_dimension, now, before)
    assert found is not None and found.metric == "completeness" and found.score == 80.0
    # A score without the dimension matches nothing.
    assert score_crossing(by_dimension, ScoreValues(10.0, {}), None) is None


def _content(**changes) -> messages.Content:
    item = {
        "finding_id": "f1",
        "series_id": "s1",
        "series_name": "Feeder 4 pressure",
        "asset_path": "plant/feeder-4",
        "check_id": "tby.flatline",
        "dimension": "plausibility",
        "severity": "critical",
        "window_start": "2026-11-20T02:00:00+00:00",
        "window_end": "2026-11-20T03:00:00+00:00",
        "summary": "The value did not change for 60 minutes.",
        "run_id": "r1",
    }
    base = {
        "delivery": uuid.UUID(int=7),
        "event": "alert",
        "rule_id": uuid.UUID(int=1),
        "rule_name": "Critical on feeders",
        "rule_kind": "finding",
        "condition": {"min_severity": "critical"},
        "workspace_id": uuid.UUID(int=2),
        "workspace_name": "default",
        "n_matches": 1,
        "items": [item],
        "sent_at": datetime(2026, 11, 20, 2, 1, tzinfo=UTC),
        "link": "https://tabayyun.example.org/findings",
    }
    return messages.Content(**(base | changes))


def test_email_message():
    """The email names the rule, the finding with its series, check and window, and the link."""
    message = messages.email_message(
        _content(), sender="Tabayyun <tabayyun@example.org>", recipients=["ops@example.org", "b@example.org"]
    )
    assert message["Subject"] == "[Tabayyun] Critical on feeders: 1 new finding of severity critical"
    assert message["To"] == "ops@example.org, b@example.org"
    assert message["Auto-Submitted"] == "auto-generated"
    body = message.get_content()
    for part in (
        "Rule: Critical on feeders",
        "Workspace: default",
        "[critical] Feeder 4 pressure (plant/feeder-4): tby.flatline",
        "The value did not change for 60 minutes.",
        "Open: https://tabayyun.example.org/findings",
    ):
        assert part in body

    many = _content(n_matches=30, items=[_content().items[0]] * 25, condition={"min_severity": "high"})
    text = messages.email_message(many, sender="t@example.org", recipients=["a@example.org"])
    assert text["Subject"].endswith("30 new findings of severity high or higher")
    assert text.get_content().count("[critical]") == messages.EMAIL_ITEMS
    assert "… and 10 more." in text.get_content()

    score = _content(
        rule_kind="score",
        condition={"metric": "overall", "below": 70.0},
        items=[
            {
                "series_name": "Feeder 5 flow",
                "asset_path": None,
                "metric": "overall",
                "score": 64.25,
                "previous": None,
            }
        ],
        link=None,
    )
    text = messages.email_message(score, sender="t@example.org", recipients=["a@example.org"])
    assert text["Subject"] == "[Tabayyun] Critical on feeders: 1 series below 70 (overall)"
    assert "- Feeder 5 flow: overall 64.2, no score before" in text.get_content()
    assert "Open:" not in text.get_content()

    test = messages.email_message(
        _content(event="test"), sender="t@example.org", recipients=["a@example.org"]
    )
    assert test["Subject"] == "[Tabayyun] Critical on feeders: test notification"
    assert "This is a test" in test.get_content()


def test_webhook_request_signs():
    """The body is JSON with the matches; with a secret the signature is its HMAC-SHA256."""
    content = _content(n_matches=3)
    body = messages.webhook_body(content)
    payload = json.loads(body)
    assert payload["event"] == "alert" and payload["delivery"] == str(uuid.UUID(int=7))
    assert payload["rule"] == {"id": str(uuid.UUID(int=1)), "name": "Critical on feeders", "kind": "finding"}
    assert payload["workspace"] == {"id": str(uuid.UUID(int=2)), "name": "default"}
    assert (payload["n_matches"], payload["truncated"], len(payload["matches"])) == (3, True, 1)
    assert payload["url"] == "https://tabayyun.example.org/findings"

    plain = messages.webhook_headers(content, body, secret=None, version="1.2.3")
    assert plain == {
        "Content-Type": "application/json",
        "User-Agent": "Tabayyun/1.2.3",
        "X-Tabayyun-Event": "alert",
        "X-Tabayyun-Delivery": str(uuid.UUID(int=7)),
    }
    signed = messages.webhook_headers(content, body, secret="s3cret-value", version="1.2.3")
    expected = hmac.new(b"s3cret-value", body, hashlib.sha256).hexdigest()
    assert signed["X-Tabayyun-Signature"] == f"sha256={expected}"


def test_email_subject_is_one_line():
    """A rule name stored with a line break still gives a header the mail library accepts."""
    content = dataclasses.replace(_content(), rule_name="Critical\non  feeders")
    message = messages.email_message(content, sender="t@example.org", recipients=["ops@example.org"])
    assert message["Subject"] == "[Tabayyun] Critical on feeders: 1 new finding of severity critical"
