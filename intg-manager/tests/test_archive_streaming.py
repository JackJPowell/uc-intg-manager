"""Archives stay on disk during download/upload and are cleaned on every exit."""

import asyncio
import os
import sys
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from unfurled.api import CoreAPI

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sync_api import DownloadedArchive, GitHubClient, cleanup_stale_archives  # noqa: E402


@pytest.mark.parametrize("update", [False, True])
@pytest.mark.parametrize("upload_fails", [False, True])
def test_stream_download_and_multipart_upload(
    tmp_path, monkeypatch, update, upload_fails
):
    monkeypatch.setenv("UC_DATA_HOME", str(tmp_path))
    chunk = b"archive contents" * 4096
    count = 960  # 60 MiB, reproducing the reported archive size.
    received = []

    async def run():
        async def download(request):
            response = web.StreamResponse()
            await response.prepare(request)
            for _ in range(count):
                await response.write(chunk)
            await response.write_eof()
            return response

        async def install(request):
            assert request.headers["Authorization"] == "Bearer test-key"
            assert request.query.get("update") == ("true" if update else None)
            reader = await request.multipart()
            part = await reader.next()
            assert part.name == "file"
            assert part.filename == "release.tar.gz"
            assert part.headers["Content-Type"] == "application/x-gzip"
            total = 0
            while block := await part.read_chunk():
                total += len(block)
            received.append(total)
            if upload_fails:
                return web.json_response({"message": "install failed"}, status=500)
            return web.json_response({"installed": True})

        app = web.Application()
        app.router.add_get("/release", download)
        app.router.add_post("/api/intg/install", install)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        github = GitHubClient()

        async def release(*args, **kwargs):
            return {
                "assets": [
                    {
                        "name": "release.tar.gz",
                        "browser_download_url": f"{base_url}/release",
                    }
                ]
            }

        monkeypatch.setattr(github, "get_latest_release", release)

        async def no_full_read(*args, **kwargs):
            raise AssertionError("Release download must not buffer the full response")

        original_read = aiohttp.ClientResponse.read
        monkeypatch.setattr(aiohttp.ClientResponse, "read", no_full_read)
        try:
            archive = await github.download_release_asset("example", "demo")
            monkeypatch.setattr(aiohttp.ClientResponse, "read", original_read)
            assert archive.size == len(chunk) * count
            assert archive.path.parent.parent == tmp_path
            path = archive.path
            async with CoreAPI(f"{base_url}/api/", api_key="test-key") as api:
                try:
                    with archive:
                        assert await archive.install(api, update=update) == {
                            "installed": True
                        }
                except Exception:
                    if not upload_fails:
                        raise
                else:
                    assert not upload_fails
            assert not path.parent.exists()
            assert received == [len(chunk) * count]
        finally:
            await runner.cleanup()

    asyncio.run(run())
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("error", [OSError("disk full"), asyncio.CancelledError()])
def test_partial_download_cleanup(tmp_path, monkeypatch, error):
    monkeypatch.setenv("UC_DATA_HOME", str(tmp_path))

    async def chunks(size):
        assert size == 64 * 1024
        yield b"partial archive"
        raise error

    class Response:
        status = 200
        content = SimpleNamespace(iter_chunked=chunks)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Session(Response):
        def get(self, *args, **kwargs):
            return Response()

    async def release(*args, **kwargs):
        return {
            "assets": [{"name": "release.tar.gz", "browser_download_url": "unused"}]
        }

    client = GitHubClient()
    monkeypatch.setattr(client, "get_latest_release", release)
    monkeypatch.setattr(client, "_make_session", lambda *args: Session())
    with pytest.raises(type(error)):
        asyncio.run(client.download_release_asset("example", "demo"))
    assert list(tmp_path.iterdir()) == []


def test_storage_fallback_and_stale_cleanup(tmp_path, monkeypatch):
    monkeypatch.delenv("UC_DATA_HOME", raising=False)
    monkeypatch.setenv("UC_CONFIG_HOME", str(tmp_path))
    with DownloadedArchive("release.tar.gz") as archive:
        assert archive.path.parent.parent == tmp_path
    monkeypatch.delenv("UC_CONFIG_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    with DownloadedArchive("release.tar.gz") as archive:
        assert archive.path.parent.parent == tmp_path

    stale = tmp_path / "im-archive-interrupted"
    stale.mkdir()
    (stale / "archive.tar.gz").write_bytes(b"interrupted")
    unrelated = tmp_path / "manager.json"
    unrelated.write_text("{}")
    cleanup_stale_archives()
    assert not stale.exists()
    assert unrelated.exists()


def test_cancelled_upload_closes_file_and_removes_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("UC_DATA_HOME", str(tmp_path))

    async def run():
        started = asyncio.Event()
        handles = []
        original_open = type(tmp_path).open

        def track_open(path, *args, **kwargs):
            handle = original_open(path, *args, **kwargs)
            if args == ("rb",):
                handles.append(handle)
            return handle

        monkeypatch.setattr(type(tmp_path), "open", track_open)

        async def request(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        archive = DownloadedArchive("release.tar.gz")
        archive.path.write_bytes(b"archive")
        directory = archive.path.parent

        async def upload():
            with archive:
                await archive.install(SimpleNamespace(request=request))

        task = asyncio.create_task(upload())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert handles and all(handle.closed for handle in handles)
        assert not directory.exists()

    asyncio.run(run())
