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
    @patch("pipeline.opencode_client.requests.get")
    @patch("pipeline.opencode_client.requests.post")
    def test_returns_session_id(
        self, mock_post: MagicMock, mock_get: MagicMock
    ) -> None:
        from pipeline.opencode_client import create_session

        mock_post.return_value = _mock_response(200, {"id": "sess-abc"})
        # create_session resolves the owning serve via pigeon /route after create.
        mock_get.return_value = _mock_response(
            200, {"apiBase": "http://127.0.0.1:4096"}
        )
        result = create_session("/home/dev/projects/my-podcasts")
        assert result == "sess-abc"
        # The session row is created on serve-0 (the OPENCODE_URL fallback).
        create_url = mock_post.call_args_list[0][0][0]
        assert create_url == "http://127.0.0.1:4096/session"
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


class TestResolveServeUrl:
    """`resolve_serve_url` asks pigeon which serve owns a session, degrading to
    OPENCODE_URL (serve-0) on any failure so routing is never worse than the
    pre-pool single-serve behavior."""

    @patch("pipeline.opencode_client.requests.get")
    def test_returns_apibase_from_route(self, mock_get: MagicMock) -> None:
        from pipeline.opencode_client import resolve_serve_url

        mock_get.return_value = _mock_response(
            200, {"apiBase": "http://127.0.0.1:4098"}
        )
        assert resolve_serve_url("sess-abc") == "http://127.0.0.1:4098"
        # Routed by session id against the pigeon daemon.
        call = mock_get.call_args
        assert call[1]["params"] == {"session_id": "sess-abc"}
        assert call[0][0] == "http://127.0.0.1:4731/route"

    @patch("pipeline.opencode_client.requests.get")
    def test_falls_back_on_non_200(self, mock_get: MagicMock) -> None:
        from pipeline.opencode_client import OPENCODE_URL, resolve_serve_url

        mock_get.return_value = _mock_response(503)
        assert resolve_serve_url("sess-abc") == OPENCODE_URL.rstrip("/")

    @patch("pipeline.opencode_client.requests.get")
    def test_falls_back_on_connection_error(self, mock_get: MagicMock) -> None:
        from pipeline.opencode_client import OPENCODE_URL, resolve_serve_url

        mock_get.side_effect = requests.RequestException("refused")
        assert resolve_serve_url("sess-abc") == OPENCODE_URL.rstrip("/")

    @patch("pipeline.opencode_client.requests.get")
    def test_falls_back_on_missing_apibase(self, mock_get: MagicMock) -> None:
        from pipeline.opencode_client import OPENCODE_URL, resolve_serve_url

        mock_get.return_value = _mock_response(200, {"serveId": "serve-1"})
        assert resolve_serve_url("sess-abc") == OPENCODE_URL.rstrip("/")


class TestRouteAfterCreate:
    """End-to-end: a session is created on serve-0 but subsequent prompt/poll
    calls go to the serve that pigeon says OWNS it (HRW placement)."""

    @patch("pipeline.opencode_client.requests.get")
    @patch("pipeline.opencode_client.requests.post")
    def test_prompt_goes_to_routed_owner(
        self, mock_post: MagicMock, mock_get: MagicMock
    ) -> None:
        from pipeline import opencode_client

        opencode_client._session_serve.clear()
        mock_post.side_effect = [
            _mock_response(200, {"id": "sess-xyz"}),  # POST /session (create)
            _mock_response(204),  # POST prompt_async
        ]
        mock_get.return_value = _mock_response(
            200, {"apiBase": "http://127.0.0.1:4098"}
        )

        sid = opencode_client.create_session("/home/dev/projects/my-podcasts")
        opencode_client.send_prompt_async(sid, "hi")

        # create lands on serve-0
        assert mock_post.call_args_list[0][0][0] == "http://127.0.0.1:4096/session"
        # /route consulted with the new sid
        assert mock_get.call_args[1]["params"] == {"session_id": "sess-xyz"}
        # prompt goes to the routed owner, NOT serve-0
        assert (
            mock_post.call_args_list[1][0][0]
            == "http://127.0.0.1:4098/session/sess-xyz/prompt_async"
        )

    @patch("pipeline.opencode_client.requests.get")
    @patch("pipeline.opencode_client.requests.post")
    def test_falls_back_to_serve0_when_route_unavailable(
        self, mock_post: MagicMock, mock_get: MagicMock
    ) -> None:
        from pipeline import opencode_client

        opencode_client._session_serve.clear()
        mock_post.side_effect = [
            _mock_response(200, {"id": "sess-down"}),
            _mock_response(204),
        ]
        mock_get.side_effect = requests.RequestException("pigeon down")

        sid = opencode_client.create_session("/x")
        opencode_client.send_prompt_async(sid, "hi")

        assert (
            mock_post.call_args_list[1][0][0]
            == "http://127.0.0.1:4096/session/sess-down/prompt_async"
        )


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

    def test_extracts_text_from_nested_role(self) -> None:
        """Handle opencode API format where role is under info.role."""
        from pipeline.opencode_client import get_last_assistant_text

        messages = [
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "hi"}]},
            {
                "info": {"role": "assistant"},
                "parts": [
                    {"type": "step-start", "id": "s1"},
                    {"type": "text", "text": "PONG"},
                    {"type": "step-finish", "reason": "done"},
                ],
            },
        ]
        assert get_last_assistant_text(messages) == "PONG"

    def test_returns_empty_when_no_assistant(self) -> None:
        from pipeline.opencode_client import get_last_assistant_text

        messages = [{"role": "user", "parts": [{"type": "text", "text": "hi"}]}]
        assert get_last_assistant_text(messages) == ""


class TestWaitForIdle:
    """`wait_for_idle` polls `/session/{id}/message` for the assistant
    message's `step-finish` part. SSE on opencode-serve 1.14.x does not emit
    `session.status: idle` reliably, so polling is the source of truth.
    """

    @patch("pipeline.opencode_client.time.sleep")
    @patch("pipeline.opencode_client.requests.get")
    def test_returns_true_when_assistant_has_step_finish(
        self, mock_get: MagicMock, mock_sleep: MagicMock
    ) -> None:
        from pipeline.opencode_client import wait_for_idle

        # First poll: assistant message exists but only step-start
        # Second poll: assistant message has step-finish — done.
        in_progress = _mock_response(
            200,
            [
                {"role": "user", "parts": [{"type": "text", "text": "hi"}]},
                {
                    "info": {"role": "assistant"},
                    "parts": [{"type": "step-start", "id": "s1"}],
                },
            ],
        )
        finished = _mock_response(
            200,
            [
                {"role": "user", "parts": [{"type": "text", "text": "hi"}]},
                {
                    "info": {"role": "assistant"},
                    "parts": [
                        {"type": "step-start", "id": "s1"},
                        {"type": "text", "text": "hello"},
                        {"type": "step-finish", "reason": "stop"},
                    ],
                },
            ],
        )
        mock_get.side_effect = [in_progress, finished]

        assert wait_for_idle("sess-abc", timeout=10) is True

    @patch("pipeline.opencode_client.time.sleep")
    @patch("pipeline.opencode_client.time.time")
    @patch("pipeline.opencode_client.requests.get")
    def test_returns_false_on_timeout(
        self, mock_get: MagicMock, mock_time: MagicMock, mock_sleep: MagicMock
    ) -> None:
        from pipeline.opencode_client import wait_for_idle

        # Time progresses 0, 1, 2, ... then exceeds deadline
        mock_time.side_effect = [0.0, 1.0, 2.0, 3.0, 100.0]

        # All polls show no step-finish (still generating)
        in_progress = _mock_response(
            200,
            [
                {
                    "info": {"role": "assistant"},
                    "parts": [{"type": "step-start", "id": "s1"}],
                },
            ],
        )
        mock_get.return_value = in_progress

        assert wait_for_idle("sess-abc", timeout=10) is False

    @patch("pipeline.opencode_client.time.sleep")
    @patch("pipeline.opencode_client.requests.get")
    def test_tolerates_transient_polling_errors(
        self, mock_get: MagicMock, mock_sleep: MagicMock
    ) -> None:
        from pipeline.opencode_client import wait_for_idle

        finished = _mock_response(
            200,
            [
                {
                    "info": {"role": "assistant"},
                    "parts": [
                        {"type": "text", "text": "hello"},
                        {"type": "step-finish", "reason": "stop"},
                    ],
                },
            ],
        )

        # First poll raises a connection error, second succeeds with finish.
        mock_get.side_effect = [
            requests.RequestException("transient"),
            finished,
        ]

        assert wait_for_idle("sess-abc", timeout=10) is True

    @patch("pipeline.opencode_client.time.sleep")
    @patch("pipeline.opencode_client.requests.get")
    def test_only_treats_assistant_step_finish_as_done(
        self, mock_get: MagicMock, mock_sleep: MagicMock
    ) -> None:
        """A user message with step-finish must not be treated as session done.

        The session is only "done" when the *assistant's* message has finished.
        """
        from pipeline.opencode_client import wait_for_idle

        # User message with a stray step-finish (shouldn't happen, but defensive)
        # and an assistant message still in progress.
        in_progress = _mock_response(
            200,
            [
                {
                    "info": {"role": "user"},
                    "parts": [
                        {"type": "text", "text": "hi"},
                        {"type": "step-finish", "reason": "stop"},
                    ],
                },
                {
                    "info": {"role": "assistant"},
                    "parts": [{"type": "step-start", "id": "s1"}],
                },
            ],
        )
        finished = _mock_response(
            200,
            [
                {
                    "info": {"role": "user"},
                    "parts": [
                        {"type": "text", "text": "hi"},
                        {"type": "step-finish", "reason": "stop"},
                    ],
                },
                {
                    "info": {"role": "assistant"},
                    "parts": [
                        {"type": "text", "text": "hello"},
                        {"type": "step-finish", "reason": "stop"},
                    ],
                },
            ],
        )
        mock_get.side_effect = [in_progress, finished]

        # First poll must return False-equivalent (continue), second must finish.
        assert wait_for_idle("sess-abc", timeout=10) is True

    @patch("pipeline.opencode_client.time.sleep")
    @patch("pipeline.opencode_client.requests.get")
    def test_handles_no_assistant_message_yet(
        self, mock_get: MagicMock, mock_sleep: MagicMock
    ) -> None:
        from pipeline.opencode_client import wait_for_idle

        # First poll: no assistant message at all (only user)
        no_asst = _mock_response(
            200,
            [{"info": {"role": "user"}, "parts": [{"type": "text", "text": "hi"}]}],
        )
        finished = _mock_response(
            200,
            [
                {"info": {"role": "user"}, "parts": [{"type": "text", "text": "hi"}]},
                {
                    "info": {"role": "assistant"},
                    "parts": [
                        {"type": "text", "text": "hello"},
                        {"type": "step-finish", "reason": "stop"},
                    ],
                },
            ],
        )
        mock_get.side_effect = [no_asst, finished]

        assert wait_for_idle("sess-abc", timeout=10) is True
