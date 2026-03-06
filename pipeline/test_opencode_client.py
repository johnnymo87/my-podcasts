from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests


def _mock_response(
    status_code: int = 200, json_data: dict | list | None = None
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    if json_data is not None:
        resp.json.return_value = json_data
    resp.raise_for_status.side_effect = (
        None if resp.ok else Exception(f"HTTP {status_code}")
    )
    return resp


class TestCreateSession:
    @patch("pipeline.opencode_client.requests.post")
    def test_returns_session_id(self, mock_post: MagicMock) -> None:
        from pipeline.opencode_client import create_session

        mock_post.return_value = _mock_response(200, {"id": "sess-abc"})
        result = create_session("/home/dev/projects/my-podcasts")
        assert result == "sess-abc"
        call_kwargs = mock_post.call_args
        assert (
            call_kwargs[1]["headers"]["x-opencode-directory"]
            == "/home/dev/projects/my-podcasts"
        )

    @patch("pipeline.opencode_client.requests.post")
    def test_raises_on_failure(self, mock_post: MagicMock) -> None:
        from pipeline.opencode_client import create_session

        mock_post.return_value = _mock_response(500)
        with pytest.raises(Exception):  # noqa: B017
            create_session("/home/dev/projects/my-podcasts")


class TestSendPromptAsync:
    @patch("pipeline.opencode_client.requests.post")
    def test_sends_prompt(self, mock_post: MagicMock) -> None:
        from pipeline.opencode_client import send_prompt_async

        mock_post.return_value = _mock_response(204)
        send_prompt_async("sess-abc", "Hello agent")
        call_kwargs = mock_post.call_args
        body = call_kwargs[1]["json"]
        assert body == {"parts": [{"type": "text", "text": "Hello agent"}]}


class TestIsSessionActive:
    @patch("pipeline.opencode_client.requests.get")
    def test_returns_true_for_200(self, mock_get: MagicMock) -> None:
        from pipeline.opencode_client import is_session_active

        mock_get.return_value = _mock_response(200, {"id": "sess-abc"})
        assert is_session_active("sess-abc") is True

    @patch("pipeline.opencode_client.requests.get")
    def test_returns_false_for_404(self, mock_get: MagicMock) -> None:
        from pipeline.opencode_client import is_session_active

        mock_get.return_value = _mock_response(404)
        assert is_session_active("sess-abc") is False

    @patch("pipeline.opencode_client.requests.get")
    def test_returns_false_on_connection_error(self, mock_get: MagicMock) -> None:
        from pipeline.opencode_client import is_session_active

        mock_get.side_effect = Exception("Connection refused")
        assert is_session_active("sess-abc") is False


class TestDeleteSession:
    @patch("pipeline.opencode_client.requests.delete")
    def test_deletes_session(self, mock_delete: MagicMock) -> None:
        from pipeline.opencode_client import delete_session

        mock_delete.return_value = _mock_response(200)
        delete_session("sess-abc")
        mock_delete.assert_called_once()

    @patch("pipeline.opencode_client.requests.delete")
    def test_ignores_404(self, mock_delete: MagicMock) -> None:
        from pipeline.opencode_client import delete_session

        mock_delete.return_value = _mock_response(404)
        delete_session("sess-abc")  # Should not raise


class TestGetMessages:
    @patch("pipeline.opencode_client.requests.get")
    def test_returns_messages(self, mock_get: MagicMock) -> None:
        from pipeline.opencode_client import get_messages

        mock_get.return_value = _mock_response(
            200,
            [
                {"role": "user", "parts": [{"type": "text", "text": "hi"}]},
                {"role": "assistant", "parts": [{"type": "text", "text": "hello"}]},
            ],
        )
        msgs = get_messages("sess-abc")
        assert len(msgs) == 2
        assert msgs[1]["role"] == "assistant"


class TestGetLastAssistantText:
    def test_extracts_text_from_messages(self) -> None:
        from pipeline.opencode_client import get_last_assistant_text

        messages = [
            {"role": "user", "parts": [{"type": "text", "text": "hi"}]},
            {
                "role": "assistant",
                "parts": [
                    {"type": "text", "text": "Hello "},
                    {"type": "text", "text": "world"},
                ],
            },
        ]
        assert get_last_assistant_text(messages) == "Hello world"

    def test_returns_empty_when_no_assistant(self) -> None:
        from pipeline.opencode_client import get_last_assistant_text

        messages = [{"role": "user", "parts": [{"type": "text", "text": "hi"}]}]
        assert get_last_assistant_text(messages) == ""


class TestWaitForIdle:
    @patch("pipeline.opencode_client.requests.get")
    def test_returns_true_on_idle_event(self, mock_get: MagicMock) -> None:
        from pipeline.opencode_client import wait_for_idle

        # Simulate SSE stream with session.status idle event
        sse_lines = [
            b'data: {"type": "session.status", "properties": {"sessionID": "sess-abc", "status": {"type": "idle"}}}',  # noqa: E501
            b"",
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.ok = True
        mock_resp.iter_lines.return_value = iter(sse_lines)
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_get.return_value = mock_resp

        assert wait_for_idle("sess-abc", timeout=5) is True

    @patch("pipeline.opencode_client.requests.get")
    def test_ignores_other_session_idle(self, mock_get: MagicMock) -> None:
        from pipeline.opencode_client import wait_for_idle

        # Idle event for a different session, then our session
        sse_lines = [
            b'data: {"type": "session.status", "properties": {"sessionID": "sess-OTHER", "status": {"type": "idle"}}}',  # noqa: E501
            b"",
            b'data: {"type": "session.status", "properties": {"sessionID": "sess-abc", "status": {"type": "idle"}}}',  # noqa: E501
            b"",
        ]
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.ok = True
        mock_resp.iter_lines.return_value = iter(sse_lines)
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_get.return_value = mock_resp

        assert wait_for_idle("sess-abc", timeout=5) is True

    @patch("pipeline.opencode_client.time.sleep")  # Don't actually sleep
    @patch("pipeline.opencode_client.requests.get")
    def test_falls_back_to_polling_on_sse_failure(
        self, mock_get: MagicMock, mock_sleep: MagicMock
    ) -> None:
        from pipeline.opencode_client import wait_for_idle

        # First call (SSE) raises, second call (polling) returns idle status
        poll_resp = MagicMock()
        poll_resp.ok = True
        poll_resp.json.return_value = {}  # Session not in statuses = idle

        mock_get.side_effect = [
            requests.RequestException("SSE connection failed"),
            poll_resp,
        ]

        assert wait_for_idle("sess-abc", timeout=10) is True
