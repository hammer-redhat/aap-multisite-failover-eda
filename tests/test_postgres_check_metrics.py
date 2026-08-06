import os
import sys
from unittest.mock import MagicMock, patch, call
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "files", "postgres_checks"))


class TestRunSingleCheck:
    def test_returns_false_and_success_when_primary(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.return_value = (False,)

        with patch("psycopg2.connect", return_value=mock_conn):
            import postgres_check_example as mod
            in_recovery, success, duration, error = mod._run_single_check(
                {"host": "h", "port": 5432, "dbname": "db", "user": "u", "password": "p"}
            )

        assert in_recovery is False
        assert success is True
        assert duration >= 0
        assert error is None

    def test_returns_true_and_success_when_standby(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.return_value = (True,)

        with patch("psycopg2.connect", return_value=mock_conn):
            import postgres_check_example as mod
            in_recovery, success, duration, error = mod._run_single_check(
                {"host": "h", "port": 5432, "dbname": "db", "user": "u", "password": "p"}
            )

        assert in_recovery is True
        assert success is True
        assert error is None

    def test_returns_none_and_failure_on_connection_error(self):
        with patch("psycopg2.connect", side_effect=Exception("connection refused")):
            import postgres_check_example as mod
            in_recovery, success, duration, error = mod._run_single_check(
                {"host": "bad", "port": 5432, "dbname": "db", "user": "u", "password": "p"}
            )

        assert in_recovery is None
        assert success is False
        assert duration >= 0
        assert "connection refused" in error

    def test_closes_connection_after_successful_check(self):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.return_value = (False,)

        with patch("psycopg2.connect", return_value=mock_conn):
            import postgres_check_example as mod
            mod._run_single_check(
                {"host": "h", "port": 5432, "dbname": "db", "user": "u", "password": "p"}
            )

        mock_cur.close.assert_called_once()
        mock_conn.close.assert_called_once()


class TestRunMetricsLoop:
    def test_sets_check_success_gauge_on_success(self, monkeypatch):
        monkeypatch.setenv("SITE_LABEL", "site1")

        mock_check = MagicMock(return_value=(False, True, 0.05, None))
        call_count = 0

        def fake_sleep(_):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                raise StopIteration

        import postgres_check_example as mod

        mock_success = MagicMock()
        mock_recovery = MagicMock()
        mock_duration = MagicMock()
        mock_ts = MagicMock()

        with patch.object(mod, "_run_single_check", mock_check), \
             patch.object(mod, "_METRIC_CHECK_SUCCESS", mock_success), \
             patch.object(mod, "_METRIC_IN_RECOVERY", mock_recovery), \
             patch.object(mod, "_METRIC_DURATION", mock_duration), \
             patch.object(mod, "_METRIC_LAST_RUN_TS", mock_ts), \
             patch("time.sleep", side_effect=fake_sleep):
            with pytest.raises(StopIteration):
                mod.run_metrics_loop(
                    {"host": "h", "port": 5432, "dbname": "db", "user": "u", "password": "p"},
                    interval=60,
                )

        mock_success.labels.return_value.set.assert_called_with(1)
        mock_recovery.labels.return_value.set.assert_called_with(0)  # in_recovery=False → 0

    def test_sets_check_success_zero_and_skips_recovery_on_failure(self, monkeypatch):
        monkeypatch.setenv("SITE_LABEL", "site1")

        mock_check = MagicMock(return_value=(None, False, 0.01, "refused"))
        call_count = 0

        def fake_sleep(_):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                raise StopIteration

        import postgres_check_example as mod

        mock_success = MagicMock()
        mock_recovery = MagicMock()
        mock_duration = MagicMock()
        mock_ts = MagicMock()

        with patch.object(mod, "_run_single_check", mock_check), \
             patch.object(mod, "_METRIC_CHECK_SUCCESS", mock_success), \
             patch.object(mod, "_METRIC_IN_RECOVERY", mock_recovery), \
             patch.object(mod, "_METRIC_DURATION", mock_duration), \
             patch.object(mod, "_METRIC_LAST_RUN_TS", mock_ts), \
             patch("time.sleep", side_effect=fake_sleep):
            with pytest.raises(StopIteration):
                mod.run_metrics_loop(
                    {"host": "h", "port": 5432, "dbname": "db", "user": "u", "password": "p"},
                    interval=60,
                )

        mock_success.labels.return_value.set.assert_called_with(0)
        # in_recovery gauge must NOT be updated on failure (stale value preserved)
        mock_recovery.labels.return_value.set.assert_not_called()


class TestMainModeRouting:
    def test_main_calls_run_metrics_server_in_metrics_mode(self, monkeypatch):
        monkeypatch.setenv("MODE", "metrics")

        import postgres_check_example as mod

        with patch.object(mod, "run_metrics_server") as mock_server:
            mod.main()

        mock_server.assert_called_once()

    def test_main_uses_cron_path_by_default(self, monkeypatch):
        monkeypatch.delenv("MODE", raising=False)
        monkeypatch.setenv("WEBHOOK_URL", "https://eda.example.com/webhook")
        monkeypatch.setenv("AUTH_TOKEN", "token123")

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_conn.cursor.return_value = mock_cur
        mock_cur.fetchone.return_value = (False,)
        mock_response = MagicMock()
        mock_response.ok = True
        mock_response.status_code = 200
        mock_response.text = "OK"

        import postgres_check_example as mod

        with patch("psycopg2.connect", return_value=mock_conn), \
             patch("requests.post", return_value=mock_response) as mock_post, \
             patch.dict(os.environ, {"DB_CONFIG": '{"host":"h","port":5432,"dbname":"db","user":"u","password":"p"}'}):
            result = mod.main()

        assert result == 0
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs[1]["json"] == {"in_recovery": False}
