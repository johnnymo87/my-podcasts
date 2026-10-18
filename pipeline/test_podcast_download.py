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
