import httpx
import pytest

from pipeline.podcast_download import DownloadError, download_enclosure


def _client(
    body: bytes, *, length: str | None = None, status: int = 200
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        headers = {"content-length": length} if length is not None else {}
        return httpx.Response(status, content=body, headers=headers)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_downloads_atomically(tmp_path) -> None:
    dest = tmp_path / "audio.mp3"
    n = download_enclosure("https://x/a.mp3", dest, client=_client(b"abc"))
    assert n == 3 and dest.read_bytes() == b"abc"
    assert not (tmp_path / "audio.mp3.tmp").exists()


def test_reuses_existing_file(tmp_path) -> None:
    dest = tmp_path / "audio.mp3"
    dest.write_bytes(b"old")

    def boom(request):  # must not be called
        raise AssertionError("re-downloaded")

    client = httpx.Client(transport=httpx.MockTransport(boom))
    assert download_enclosure("https://x/a.mp3", dest, client=client) == 3


def test_short_body_vs_content_length_refused(tmp_path) -> None:
    # MockTransport recomputes content-length from content, so simulate a
    # truncated stream by declaring a larger length via a raw stream.
    def handler(request):
        return httpx.Response(
            200, headers={"content-length": "10"}, stream=httpx.ByteStream(b"abc")
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(DownloadError, match="incomplete"):
        download_enclosure("https://x/a.mp3", tmp_path / "a.mp3", client=client)
    assert not (tmp_path / "a.mp3").exists()


def test_size_cap(tmp_path) -> None:
    with pytest.raises(DownloadError, match="exceeds"):
        download_enclosure(
            "https://x/a.mp3",
            tmp_path / "a.mp3",
            client=_client(b"x" * 11),
            max_bytes=10,
        )


def test_http_error(tmp_path) -> None:
    with pytest.raises(DownloadError, match="404"):
        download_enclosure(
            "https://x/a.mp3", tmp_path / "a.mp3", client=_client(b"", status=404)
        )


def test_requests_identity_encoding_even_on_injected_client(tmp_path) -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("accept-encoding"))
        return httpx.Response(200, content=b"abc")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    download_enclosure("https://x/a.mp3", tmp_path / "a.mp3", client=client)
    assert seen == ["identity"]


def test_creates_missing_parent_dir(tmp_path) -> None:
    dest = tmp_path / "nested" / "dir" / "a.mp3"
    assert download_enclosure("https://x/a.mp3", dest, client=_client(b"abc")) == 3
    assert dest.read_bytes() == b"abc"


def test_remote_protocol_error_is_incomplete(tmp_path) -> None:
    def handler(request):
        raise httpx.RemoteProtocolError("peer closed connection")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(DownloadError, match="incomplete download"):
        download_enclosure("https://x/a.mp3", tmp_path / "a.mp3", client=client)


def test_other_http_error_is_download_failed(tmp_path) -> None:
    def handler(request):
        raise httpx.ConnectError("boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(DownloadError, match="download failed: ConnectError: boom"):
        download_enclosure("https://x/a.mp3", tmp_path / "a.mp3", client=client)


def test_non_ascii_digit_content_length_is_ignored(tmp_path) -> None:
    # "\u00b2".isdigit() is True but int() raises; must not crash or be trusted.
    # httpx refuses non-ASCII str header values, so send raw bytes.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"abc", headers=[(b"content-length", "\u00b2".encode())]
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    dest = tmp_path / "a.mp3"
    assert download_enclosure("https://x/a.mp3", dest, client=client) == 3
    assert dest.read_bytes() == b"abc"
