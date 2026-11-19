"""Configuration must be supplied by the operator, never by the source tree."""
import info


CLEAN_ENV = {
    "BOT_TOKEN": "123456:AA-valid-looking-token",
    "API_ID": "12345",
    "API_HASH": "a" * 32,
    "DATABASE_URI": "mongodb+srv://user:pass@cluster.example/db",
}


def test_clean_environment_has_no_errors():
    assert info.required_config_issues(CLEAN_ENV) == []


def test_missing_credentials_are_reported_with_actionable_messages():
    issues = info.required_config_issues({})
    joined = " ".join(issues)
    for name in ("BOT_TOKEN", "API_ID", "API_HASH", "DATABASE_URI"):
        assert name in joined
    assert "my.telegram.org" in joined
    assert "BotFather" in joined


def test_malformed_values_are_rejected():
    env = dict(CLEAN_ENV, BOT_TOKEN="not-a-token", API_ID="abc", DATABASE_URI="https://example.com")
    issues = info.required_config_issues(env)
    assert len(issues) == 3


def test_multiple_db_and_clones_need_their_own_configuration():
    env = dict(CLEAN_ENV, MULTIPLE_DB="True")
    assert any("DATABASE_URI2" in i for i in info.required_config_issues(env))

    env = dict(env, DATABASE_URI2="mongodb://localhost:27017", CLONE_ENABLED="True")
    issues = info.required_config_issues(env)
    assert any("CLONE_ENCRYPTION_KEY" in i for i in issues)
    assert any("CLONE_DATABASE_URI" in i for i in issues)


def test_non_fatal_warnings_cover_common_deployment_mistakes():
    warnings = info.config_warnings({})
    joined = " ".join(warnings)
    for name in ("ADMINS", "LOG_CHANNEL", "SHORTENER_API", "FQDN"):
        assert name in joined
    complete = dict(CLEAN_ENV, ADMINS="123", LOG_CHANNEL="-100123",
                    SHORTENER_API="key", FQDN="streams.example.com")
    assert info.config_warnings(complete) == []
