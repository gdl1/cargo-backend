from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from app import check_integrations
from app.services.products import SourcedProducts


@pytest.fixture
def check_settings(settings, monkeypatch, tmp_path):
    credentials = tmp_path / "private-credential.json"
    credentials.write_text("{}", encoding="utf-8")
    settings.openai_api_key = SecretStr("private-api-key")
    settings.firebase_project_id = "cargo-test"
    settings.google_application_credentials = str(credentials)
    settings.notification_api_key = SecretStr("private-notification-key-1234567890")
    monkeypatch.setattr(check_integrations, "Settings", lambda: settings)
    return settings


def test_offline_check_never_calls_external_services(check_settings, monkeypatch, capsys):
    external = Mock(side_effect=AssertionError("must not call"))
    monkeypatch.setattr(check_integrations, "ResponsesClient", external)
    monkeypatch.setattr(check_integrations, "PushSender", external)

    assert check_integrations.main([]) == 0
    external.assert_not_called()
    output = capsys.readouterr().out
    assert "실제 연동은 미검증" in output
    assert "private" not in output
    assert str(Path(check_settings.google_application_credentials).parent) not in output


def test_missing_settings_skip_live_calls(check_settings, monkeypatch, capsys):
    check_settings.openai_api_key = None
    check_settings.google_application_credentials = "missing-private-credential.json"
    external = Mock(side_effect=AssertionError("must not call"))
    monkeypatch.setattr(check_integrations, "_check_live", external)

    assert check_integrations.main(["--live"]) == 1
    external.assert_not_called()
    assert capsys.readouterr().out.count("NOT_CONFIGURED") == 3


def test_live_ai_checks_extraction_without_search_or_push(check_settings, monkeypatch, capsys):
    client = Mock()
    client.call_function.return_value = check_integrations._CargoCheck(name="캠핑 의자", quantity=3)
    monkeypatch.setattr(check_integrations, "ResponsesClient", Mock(return_value=client))
    push = Mock(side_effect=AssertionError("must not call"))
    monkeypatch.setattr(check_integrations, "PushSender", push)

    assert check_integrations.main(["--live", "--service", "ai"]) == 0
    client.call_function.assert_called_once()
    client.search.assert_not_called()
    push.assert_not_called()
    assert "AI_OK" in capsys.readouterr().out


def test_live_ai_does_not_accept_incorrect_quantity(check_settings, monkeypatch, capsys):
    client = Mock()
    client.call_function.return_value = check_integrations._CargoCheck(name="캠핑 의자", quantity=1)
    monkeypatch.setattr(check_integrations, "ResponsesClient", Mock(return_value=client))

    assert check_integrations.main(["--live", "--service", "ai"]) == 1
    assert "CHECK_FAILED" in capsys.readouterr().out


def test_live_search_allows_empty_results_without_claiming_accuracy(
    check_settings, monkeypatch, capsys
):
    client = Mock()
    client.search.return_value = (SourcedProducts(candidates=[]), set())
    monkeypatch.setattr(check_integrations, "ResponsesClient", Mock(return_value=client))

    assert check_integrations.main(["--live", "--service", "search"]) == 0
    client.search.assert_called_once()
    client.call_function.assert_not_called()
    output = capsys.readouterr().out
    assert "후보 0개" in output
    assert "정확도는 별도 확인" in output


def test_live_push_checks_authentication_without_sending(check_settings, monkeypatch, capsys):
    sender = Mock()
    monkeypatch.setattr(check_integrations, "PushSender", Mock(return_value=sender))

    assert check_integrations.main(["--live", "--service", "push"]) == 0
    sender.ensure_ready.assert_called_once()
    sender.send.assert_not_called()
    assert "FCM 발송과 휴대폰 수신은 검사하지 않았습니다" in capsys.readouterr().out


def test_provider_error_does_not_expose_credentials(check_settings, monkeypatch, capsys):
    monkeypatch.setattr(
        check_integrations,
        "_check_live",
        Mock(side_effect=ValueError("private-api-key /private/credential.json provider-response")),
    )

    assert check_integrations.main(["--live", "--service", "ai"]) == 1
    output = capsys.readouterr().out
    assert "CHECK_FAILED" in output
    assert "private" not in output
    assert "provider-response" not in output


def test_invalid_settings_does_not_print_input_values(monkeypatch, capsys):
    monkeypatch.setattr(
        check_integrations, "Settings", Mock(side_effect=ValueError("private-notification-key"))
    )

    assert check_integrations.main([]) == 1
    output = capsys.readouterr().out
    assert "SETTINGS_INVALID" in output
    assert "private" not in output
