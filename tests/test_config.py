from pathlib import Path

import pytest

from cli import app
from config import Config, ConfigError, load_config

SECRET = "sk-super-secret-value-12345"


def good_env(**overrides: str) -> dict[str, str]:
    env = {
        "OPENAI_API_KEY": SECRET,
        "OPENAI_BASE_URL": "https://gateway.example/v1",
        "OPENAI_MODEL": "test-model",
    }
    env.update(overrides)
    return env


def test_valid_config_loads_with_defaults(tmp_path: Path) -> None:
    cfg = load_config(good_env(), base_dir=tmp_path)
    assert cfg.model == "test-model"
    assert cfg.request_timeout == 60
    assert cfg.max_tool_rounds == 6
    assert cfg.report_dir == tmp_path / "reports"


def test_valid_config_with_overrides(tmp_path: Path) -> None:
    env = good_env(
        AGENT_REQUEST_TIMEOUT="12.5",
        AGENT_MAX_TOOL_ROUNDS="3",
        AGENT_REPORT_DIR=str(tmp_path / "out"),
    )
    cfg = load_config(env, base_dir=tmp_path)
    assert cfg.request_timeout == 12.5
    assert cfg.max_tool_rounds == 3
    assert cfg.report_dir == tmp_path / "out"


def test_missing_api_key_fails_with_useful_error() -> None:
    env = good_env()
    del env["OPENAI_API_KEY"]
    with pytest.raises(ConfigError, match="OPENAI_API_KEY is not set"):
        load_config(env)


def test_placeholder_api_key_rejected() -> None:
    with pytest.raises(ConfigError, match="placeholder"):
        load_config(good_env(OPENAI_API_KEY="replace_me"))


def test_all_problems_reported_together() -> None:
    with pytest.raises(ConfigError) as exc:
        load_config({})
    assert len(exc.value.problems) == 3


@pytest.mark.parametrize("value", ["abc", "0", "-5", "601", "nan", "inf"])
def test_invalid_timeout_fails(value: str) -> None:
    with pytest.raises(ConfigError, match="AGENT_REQUEST_TIMEOUT"):
        load_config(good_env(AGENT_REQUEST_TIMEOUT=value))


@pytest.mark.parametrize("value", ["abc", "0", "-1", "1.5", "51"])
def test_invalid_tool_rounds_fails(value: str) -> None:
    with pytest.raises(ConfigError, match="AGENT_MAX_TOOL_ROUNDS"):
        load_config(good_env(AGENT_MAX_TOOL_ROUNDS=value))


@pytest.mark.parametrize("value", ["not a url", "ftp://host/v1", "https://", "//x"])
def test_malformed_base_url_fails(value: str) -> None:
    with pytest.raises(ConfigError, match="OPENAI_BASE_URL"):
        load_config(good_env(OPENAI_BASE_URL=value))


def test_report_dir_not_created_on_load(tmp_path: Path) -> None:
    cfg = load_config(good_env(), base_dir=tmp_path)
    assert not cfg.report_dir.exists()


def test_report_dir_created_only_when_requested(tmp_path: Path) -> None:
    cfg = load_config(good_env(), base_dir=tmp_path)
    assert cfg.ensure_report_dir().is_dir()


def test_report_dir_that_is_a_file_fails(tmp_path: Path) -> None:
    f = tmp_path / "afile"
    f.write_text("x")
    with pytest.raises(ConfigError, match="not a directory"):
        load_config(good_env(AGENT_REPORT_DIR=str(f)))


def test_secret_not_in_repr_or_redacted_summary(tmp_path: Path) -> None:
    cfg = load_config(good_env(), base_dir=tmp_path)
    assert SECRET not in repr(cfg)
    assert SECRET not in str(cfg.redacted())


def test_secret_not_in_exception_output() -> None:
    env = good_env(AGENT_REQUEST_TIMEOUT="bad", OPENAI_BASE_URL=f"x://{SECRET}")
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert SECRET not in str(exc.value)
    assert SECRET not in repr(exc.value)


def test_embedded_url_credentials_not_in_redacted_summary(tmp_path: Path) -> None:
    env = good_env(OPENAI_BASE_URL="https://user:pw-secret@gateway.example/v1")
    cfg = load_config(env, base_dir=tmp_path)
    assert "pw-secret" not in str(cfg.redacted())


def test_help_works_without_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as exc:
        app.main(["--help"])
    assert exc.value.code == 0
    assert "linux-agent" in capsys.readouterr().out


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        app.main(["--version"])
    assert exc.value.code == 0
    assert "linux-agent" in capsys.readouterr().out


def test_startup_reports_config_errors_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert app.main([]) == app.EXIT_CONFIG_ERROR
    err = capsys.readouterr().err
    assert "OPENAI_API_KEY is not set" in err
    assert "Traceback" not in err


def test_startup_succeeds_and_never_prints_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import io

    monkeypatch.chdir(tmp_path)
    for k, v in good_env().items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr("sys.stdin", io.StringIO("/help\n/inspect\n/exit\n"))
    assert app.main([]) == app.EXIT_OK
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert "Goodbye" in captured.out


def test_keyboard_interrupt_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    for k, v in good_env().items():
        monkeypatch.setenv(k, v)

    def boom(_: Config) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(app, "run", boom)
    assert app.main([]) == app.EXIT_INTERRUPTED


def test_check_gateway_flag_exit_codes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agent.llm_client import CheckResult

    monkeypatch.chdir(tmp_path)
    for k, v in good_env().items():
        monkeypatch.setenv(k, v)

    monkeypatch.setattr(app, "check_gateway", lambda c: [CheckResult("a", True, "")])
    assert app.main(["--check-gateway"]) == app.EXIT_OK
    monkeypatch.setattr(app, "check_gateway", lambda c: [CheckResult("a", False, "x")])
    assert app.main(["--check-gateway"]) == app.EXIT_GATEWAY_ERROR


def test_gateway_error_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agent.llm_client import LLMAuthError

    monkeypatch.chdir(tmp_path)
    for k, v in good_env().items():
        monkeypatch.setenv(k, v)

    def boom(_: object) -> list[object]:
        raise LLMAuthError("Gateway rejected the credentials (401)", 401)

    monkeypatch.setattr(app, "check_gateway", boom)
    assert app.main(["--check-gateway"]) == app.EXIT_GATEWAY_ERROR
    err = capsys.readouterr().err
    assert "gateway error" in err and "Traceback" not in err and SECRET not in err


# -- --preview-plan -------------------------------------------------------------


def _preview(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    data: object,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    import json

    monkeypatch.chdir(tmp_path)
    for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    f = tmp_path / "plan.json"
    f.write_text(data if isinstance(data, str) else json.dumps(data))
    code = app.main(["--preview-plan", str(f)])
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_preview_plan_shows_classification_without_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import subprocess

    def forbidden(*a: object, **k: object) -> None:
        raise AssertionError("preview must not start a process")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    plan = {
        "steps": [{"id": "a", "purpose": "os", "executable": "uname", "args": ["-a"]}]
    }
    code, out, _ = _preview(monkeypatch, tmp_path, plan, capsys)
    assert code == app.EXIT_OK
    assert "[READ_ONLY] uname -a" in out and "nothing is executed" in out


def test_preview_plan_reports_blocked_steps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = {
        "steps": [
            {"id": "a", "purpose": "x", "executable": "cat", "args": ["/etc/shadow"]}
        ]
    }
    code, out, _ = _preview(monkeypatch, tmp_path, plan, capsys)
    assert code == app.EXIT_PLAN_INVALID
    assert "BLOCKED" in out and "would not run" in out


def test_preview_plan_reports_override_steps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = {
        "steps": [
            {"id": "a", "purpose": "x", "executable": "bash", "args": ["-c", "id"]}
        ]
    }
    code, out, _ = _preview(monkeypatch, tmp_path, plan, capsys)
    assert code == app.EXIT_OK
    assert "unless you override it" in out


@pytest.mark.parametrize("bad", ["{not json", '{"steps": 5}', '{"steps": []}'])
def test_preview_plan_rejects_invalid_input(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    bad: str,
) -> None:
    code, _, err = _preview(monkeypatch, tmp_path, bad, capsys)
    assert code == app.EXIT_PLAN_INVALID
    assert "Traceback" not in err


def test_preview_plan_missing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert (
        app.main(["--preview-plan", str(tmp_path / "nope.json")])
        == app.EXIT_PLAN_INVALID
    )
    assert "Traceback" not in capsys.readouterr().err
