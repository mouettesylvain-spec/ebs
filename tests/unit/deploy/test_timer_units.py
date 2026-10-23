"""P3-05 R4: a quarterly revalidation per domain (systemd timer) with a notification hook."""

from __future__ import annotations

import configparser
import json
import smtplib
import tomllib
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path

import pytest

from ebs.core.digest import hash_bytes
from ebs.core.errors import ConfigError
from ebs.meta.api import ReleaseId, RevalidationRecord, RevalidationVerdict
from ebs.release.revalidate import Notifier, NotifySettings, Revalidation, RevalidationSettings

ROOT = Path(__file__).resolve().parents[3]
UNITS = ROOT / "deploy" / "revalidate"


def _unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string((UNITS / name).read_text())
    return parser


# R4: quarterly, catching up a run missed while the host was down
def test_timer_is_quarterly() -> None:
    timer = _unit("ebs-revalidate@.timer")["Timer"]
    assert timer["OnCalendar"] == "*-01,04,07,10-01 03:00:00"
    assert timer["Persistent"] == "true"
    assert _unit("ebs-revalidate@.timer")["Install"]["WantedBy"] == "timers.target"


# R4: one instance per domain, as the service account, on the grid, every archived release
def test_service_per_domain() -> None:
    service = _unit("ebs-revalidate@.service")["Service"]
    assert service["Type"] == "oneshot"
    assert service["User"] == "ebs-revalidate"
    assert service["SupplementaryGroups"] == "%i"  # the domain's group: its CAS and debug area
    argv = service["ExecStart"].split()
    assert argv[:3] == ["/opt/ebs/bin/ebs", "release", "revalidate"]
    assert argv[argv.index("--domain") + 1] == "%i"
    assert "--all" in argv
    assert argv[argv.index("--executor") + 1] == "slurm"
    assert not any(a.startswith("--cache") for a in argv)  # a revalidation never writes it
    assert service["Environment"] == "EBS_CONFIG=/etc/ebs/revalidate.toml"
    assert service["WorkingDirectory"] == "/var/lib/ebs-revalidate/%i"
    assert service["StateDirectory"] == "ebs-revalidate/%i"
    assert service["SuccessExitStatus"] == "1"  # "did not reproduce" is reported, not a crash
    assert service["NoNewPrivileges"] == "yes"


def test_example_config_is_valid() -> None:
    config = tomllib.loads((UNITS / "revalidate.toml.example").read_text())
    settings = RevalidationSettings.from_config(config)
    assert settings.toolchain_overrides == {"questa": "latest"}
    assert {k: c.name for k, c in settings.comparators.items()} == {
        "questa.sim": "sim", "lint_legacy": "lint",
    }  # fmt: skip
    notify = settings.notify
    assert notify.webhook.startswith("https://")
    assert notify.email_to == ("cpu-signoff@example.invalid",)
    assert notify.when == "always"


def _result(verdict: RevalidationVerdict) -> Revalidation:
    record = RevalidationRecord(
        id=1, release=ReleaseId(7), build=None, verdict=verdict, report=hash_bytes(b"r"),
        markdown=hash_bytes(b"m"), toolchains={"questa": "questa/2026.1"},
        counts={"equivalent": 1}, created_at=datetime(2026, 10, 1, tzinfo=UTC),
        created_by="ebs-revalidate",
    )  # fmt: skip
    report = {"release": {"domain": "cpu-nda", "name": "rtl/cpu", "version": "2026.09.3"}}
    return Revalidation(record=record, report=report, markdown="# Revalidation\n")  # type: ignore[arg-type]


class _Sent:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, str]]] = []
        self.mails: list[EmailMessage] = []

    def post(self, url: str, body: bytes, timeout_s: float) -> None:
        self.posts.append((url, json.loads(body)))

    def mail(self, message: EmailMessage, settings: NotifySettings) -> None:
        self.mails.append(message)


SETTINGS = NotifySettings(webhook="https://hooks.example.invalid/x",
                          email_to=("team@example.invalid",))  # fmt: skip


# R4: the webhook gets a JSON summary, the recipients the Markdown report
def test_notifier_sends_webhook_and_email() -> None:
    sent = _Sent()
    errors = Notifier(SETTINGS, post_json=sent.post, send_mail=sent.mail).notify(
        _result("differences")
    )
    assert errors == []
    [(url, body)] = sent.posts
    assert url == SETTINGS.webhook
    assert body["verdict"] == "differences"
    assert body["release"] == "rtl/cpu 2026.09.3"
    assert body["domain"] == "cpu-nda"
    assert body["report"] == str(hash_bytes(b"r"))
    assert "# Revalidation" in body["text"]
    [mail] = sent.mails
    assert mail["Subject"] == "[ebs] revalidation of rtl/cpu 2026.09.3: differences"
    assert mail["To"] == "team@example.invalid"
    assert "# Revalidation" in mail.get_content()


# R4: `when = "problems"` stays quiet for a reproduced release; nothing configured, nothing sent
def test_notifier_when() -> None:
    sent = _Sent()
    quiet = NotifySettings(webhook=SETTINGS.webhook, when="problems")
    notifier = Notifier(quiet, post_json=sent.post, send_mail=sent.mail)
    assert notifier.notify(_result("reproduced")) == []
    assert sent.posts == []
    notifier.notify(_result("failed"))
    notifier.notify(_result("differences"))
    assert [body["verdict"] for _, body in sent.posts] == ["failed", "differences"]
    del sent.posts[1:]
    Notifier(NotifySettings(), post_json=sent.post, send_mail=sent.mail).notify(_result("failed"))
    assert (len(sent.posts), sent.mails) == (1, [])


# R4: a failed delivery is returned, never raised (the report is already recorded)
def test_notifier_failures_are_reported() -> None:
    def down(url: str, body: bytes, timeout_s: float) -> None:
        raise OSError("connection refused")

    def no_smtp(message: EmailMessage, settings: NotifySettings) -> None:
        raise smtplib.SMTPException("relay denied")

    errors = Notifier(SETTINGS, post_json=down, send_mail=no_smtp).notify(_result("failed"))
    assert len(errors) == 2
    assert "connection refused" in errors[0]
    assert "relay denied" in errors[1]


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ({"webhook": "http://plain.example.invalid"}, "https"),
        ({"email_to": ["not-an-address"]}, "email_to"),
        ({"when": "sometimes"}, "when"),
        ({"smtp_port": 0}, "smtp_port"),
        ({"bogus": 1}, "unknown key"),
    ],
)
def test_notify_settings_are_checked(section: dict[str, object], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        NotifySettings.from_mapping(section)
