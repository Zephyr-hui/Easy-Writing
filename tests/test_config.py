import pytest

from easy_writing.config import ConfigurationError, Settings, load_settings


def test_defaults_work_without_key():
    settings = load_settings(environ={})
    assert settings.provider == "fake"
    assert settings.max_retries == 2


def test_environment_overrides_explicit_file_without_mutating_environment(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("ANALYSIS_MODEL=file-model\nLLM_MAX_RETRIES=1\n", encoding="utf-8-sig")
    settings = load_settings(path, environ={"ANALYSIS_MODEL": "env-model"})
    assert settings.analysis_model == "env-model"
    assert settings.max_retries == 1
    import os
    assert "ANALYSIS_MODEL" not in os.environ


def test_no_parent_env_lookup(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("LLM_PROVIDER=deepseek", encoding="utf-8")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    assert load_settings(environ={}).provider == "fake"


def test_no_secret_interpolation(tmp_path):
    path = tmp_path / ".env"
    path.write_text("DEEPSEEK_API_KEY=${SECRET}\n", encoding="utf-8")
    assert load_settings(path, environ={"SECRET": "private"}).api_key.get_secret_value() == "${SECRET}"


def test_missing_key_reports_actionable_error():
    with pytest.raises(ConfigurationError, match="DEEPSEEK_API_KEY"):
        Settings().require_deepseek_key()


@pytest.mark.parametrize("name,value", [
    ("LLM_MAX_RETRIES", "-1"), ("LLM_MAX_RETRIES", "30"),
    ("LLM_TIMEOUT_SECONDS", "nan"), ("LLM_TIMEOUT_SECONDS", "0"),
    ("LLM_MAX_OUTPUT_TOKENS", "bad"), ("ANALYSIS_THINKING", "maybe"),
    ("DEEPSEEK_BASE_URL", "http://api.deepseek.com"),
    ("DEEPSEEK_BASE_URL", "https://private:secret@example.com"),
    ("DEEPSEEK_BASE_URL", "https://api.deepseek.com?key=secret"),
    ("DEEPSEEK_BASE_URL", "https://@api.deepseek.com"),
    ("DEEPSEEK_BASE_URL", "https://api.deepseek.com:bad"),
    ("ANALYSIS_MODEL", " "),
])
def test_invalid_config_does_not_echo_input(name, value):
    with pytest.raises(ConfigurationError) as caught:
        load_settings(environ={name: value, "DEEPSEEK_API_KEY": "private-api-key"})
    assert value not in str(caught.value)
    assert "private-api-key" not in str(caught.value)


def test_key_repr_is_hidden():
    settings = Settings(api_key="private-api-key")
    assert "private-api-key" not in repr(settings)
    assert "private-api-key" not in settings.model_dump_json()


@pytest.mark.parametrize("key", ["请填写你的密钥", "sk key", "sk\nkey"])
def test_invalid_key_never_echoed(key):
    with pytest.raises(ConfigurationError) as caught:
        load_settings(environ={"DEEPSEEK_API_KEY": key})
    assert "DEEPSEEK_API_KEY" in str(caught.value)
    assert key not in str(caught.value)


def test_bad_encoding_is_actionable(tmp_path):
    path = tmp_path / "bad.env"
    path.write_bytes(b"\xff\xfe\xff")
    with pytest.raises(ConfigurationError, match="UTF-8"):
        load_settings(path, environ={})
