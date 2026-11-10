import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import app.backup_engine as backup_engine
from app.backup_engine import BackupResult, sanitize_name, dir_size_bytes


def test_sanitize_name_keeps_safe_chars():
    assert sanitize_name("my-app_1.0") == "my-app_1.0"


def test_sanitize_name_strips_unsafe_chars():
    assert sanitize_name("my app/../weird:name") == "my_app_.._weird_name"


def test_dir_size_bytes(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello")  # 5 bytes
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("world!")  # 6 bytes
    assert dir_size_bytes(tmp_path) == 11


def _fake_client():
    """A MagicMock docker client wired so client.api.attach(container.id, ...) streams
    back whatever chunks were registered for that container via _fake_helper_container -
    mocking a raw attach-socket read (see iter_volume_tar_chunks, which reads this way
    instead of container.logs() to avoid routing bulk tar data through the json-file
    logging driver)."""
    client = MagicMock()
    client._chunk_registry = {}
    client.api.attach.side_effect = lambda container_id, **kwargs: iter(client._chunk_registry[container_id])
    return client


def _fake_helper_container(client, chunks, status_code=0):
    """Builds a MagicMock container as returned by client.containers.create(detach=True)
    and registers its stdout chunks with the client's fake attach socket."""
    container = MagicMock()
    container.id = f"helper-{len(client._chunk_registry)}"
    client._chunk_registry[container.id] = chunks
    container.wait.return_value = {"StatusCode": status_code}
    return container


def test_iter_volume_tar_chunks_yields_container_stdout(monkeypatch):
    client = _fake_client()
    container = _fake_helper_container(client, [b"chunk1", b"chunk2"])
    client.containers.create.return_value = container
    monkeypatch.setattr(backup_engine, "get_client", lambda: client)

    chunks = list(backup_engine.iter_volume_tar_chunks("some-volume"))

    assert chunks == [b"chunk1", b"chunk2"]
    # No bind mount at all - just the read-only volume, avoiding the host-path
    # resolution problem entirely (see module docstring on iter_volume_tar_chunks).
    run_kwargs = client.containers.create.call_args.kwargs
    assert run_kwargs["volumes"] == {"some-volume": {"bind": "/data", "mode": "ro"}}
    container.remove.assert_called_once_with(force=True)


def test_iter_volume_tar_chunks_stops_and_cleans_up_on_cancel(monkeypatch):
    # should_cancel/on_bytes are only checked once ~1MB has accumulated (see
    # iter_volume_tar_chunks) to avoid per-tiny-chunk lock overhead on a real
    # Docker attach stream - chunks here need to cross that threshold on
    # their own to exercise the mid-stream cancel path.
    big_chunk = b"x" * (1024 * 1024)
    client = _fake_client()
    container = _fake_helper_container(client, [big_chunk, big_chunk, big_chunk])
    client.containers.create.return_value = container
    monkeypatch.setattr(backup_engine, "get_client", lambda: client)

    seen = []
    # Cancel right after the first chunk is read.
    should_cancel = lambda: len(seen) >= 1  # noqa: E731

    with pytest.raises(backup_engine.BackupCancelled):
        for chunk in backup_engine.iter_volume_tar_chunks("some-volume", should_cancel=should_cancel):
            seen.append(chunk)

    assert seen == [big_chunk]
    container.remove.assert_called_once_with(force=True)


def test_iter_volume_tar_chunks_checks_cancel_at_end_even_for_small_transfer(monkeypatch):
    # A transfer that never crosses the ~1MB batching threshold must still be
    # cancellable via the final should_cancel() check after the stream ends.
    client = _fake_client()
    container = _fake_helper_container(client, [b"chunk1", b"chunk2", b"chunk3"])
    client.containers.create.return_value = container
    monkeypatch.setattr(backup_engine, "get_client", lambda: client)

    with pytest.raises(backup_engine.BackupCancelled):
        list(backup_engine.iter_volume_tar_chunks("some-volume", should_cancel=lambda: True))

    container.remove.assert_called_once_with(force=True)


def test_iter_volume_tar_chunks_reports_bytes_in_batches(monkeypatch):
    big_chunk = b"x" * (1024 * 1024)
    small_chunk = b"y" * 10
    client = _fake_client()
    container = _fake_helper_container(client, [big_chunk, small_chunk])
    client.containers.create.return_value = container
    monkeypatch.setattr(backup_engine, "get_client", lambda: client)

    reported = []
    list(backup_engine.iter_volume_tar_chunks("some-volume", on_bytes=reported.append))

    # The big chunk crosses the threshold on its own; the trailing small
    # chunk is flushed once the stream ends.
    assert reported == [len(big_chunk), len(small_chunk)]


def test_iter_volume_tar_chunks_raises_on_nonzero_exit(monkeypatch):
    client = _fake_client()
    container = _fake_helper_container(client, [b"partial"], status_code=1)
    client.containers.create.return_value = container
    monkeypatch.setattr(backup_engine, "get_client", lambda: client)

    with pytest.raises(RuntimeError):
        list(backup_engine.iter_volume_tar_chunks("some-volume"))
    container.remove.assert_called_once_with(force=True)


def test_backup_volume_to_file_writes_streamed_chunks(tmp_path: Path, monkeypatch):
    client = _fake_client()
    container = _fake_helper_container(client, [b"fake-", b"tar-data"])
    client.containers.create.return_value = container
    monkeypatch.setattr(backup_engine, "get_client", lambda: client)

    dest = tmp_path / "vol.tar.gz"
    backup_engine.backup_volume_to_file("some-volume", dest)

    assert dest.read_bytes() == b"fake-tar-data"


def test_stream_volume_to_target_uploads_without_local_file(tmp_path: Path, monkeypatch):
    client = _fake_client()
    container = _fake_helper_container(client, [b"fake-", b"tar-data"])
    client.containers.create.return_value = container
    monkeypatch.setattr(backup_engine, "get_client", lambda: client)

    dest = tmp_path / "remote" / "vol.tar.gz"
    backup_engine.stream_volume_to_target(
        "some-volume", "local_path", json.dumps({"path": str(tmp_path / "remote")}), "vol.tar.gz",
    )

    assert dest.read_bytes() == b"fake-tar-data"


def test_backup_container_removes_partial_dir_on_failure(tmp_path: Path, monkeypatch):
    container = MagicMock()
    container.name = "failing-app"
    container.attrs = {"Mounts": [], "NetworkSettings": {"Networks": {}}, "Config": {"Image": "x"}}
    container.image.save.side_effect = RuntimeError("docker daemon went away mid-save")

    client = MagicMock()
    client.containers.get.return_value = container
    monkeypatch.setattr(backup_engine, "get_client", lambda: client)

    result = backup_engine.backup_container("failing-app", dest_root=tmp_path)

    assert result.ok is False
    assert not result.path.exists()  # partial data must not linger and inflate disk usage


def test_backup_container_stops_and_restarts_running_container_when_opted_in(tmp_path: Path, monkeypatch):
    container = MagicMock()
    container.name = "postgres"
    container.attrs = {
        "Mounts": [{"Type": "volume", "Name": "pg-data"}],
        "NetworkSettings": {"Networks": {}},
        "Config": {"Image": "postgres:16"},
        "State": {"Status": "running"},
    }
    container.image.save.return_value = iter([b"image-bytes"])

    client = _fake_client()
    client.containers.get.return_value = container
    client.version.return_value = {"ApiVersion": "1.45"}
    client.containers.create.side_effect = lambda *a, **k: _fake_helper_container(client, [b"data"])
    monkeypatch.setattr(backup_engine, "get_client", lambda: client)

    result = backup_engine.backup_container("postgres", dest_root=tmp_path, stop_container=True)

    assert result.ok is True
    container.stop.assert_called_once()
    container.start.assert_called_once()


def test_backup_container_does_not_stop_already_stopped_container(tmp_path: Path, monkeypatch):
    container = MagicMock()
    container.name = "postgres"
    container.attrs = {
        "Mounts": [],
        "NetworkSettings": {"Networks": {}},
        "Config": {"Image": "postgres:16"},
        "State": {"Status": "exited"},
    }
    container.image.save.return_value = iter([b"image-bytes"])

    client = MagicMock()
    client.containers.get.return_value = container
    client.version.return_value = {"ApiVersion": "1.45"}
    monkeypatch.setattr(backup_engine, "get_client", lambda: client)

    result = backup_engine.backup_container("postgres", dest_root=tmp_path, stop_container=True)

    assert result.ok is True
    container.stop.assert_not_called()
    container.start.assert_not_called()


def test_backup_container_restarts_container_even_if_backup_fails_midway(tmp_path: Path, monkeypatch):
    container = MagicMock()
    container.name = "postgres"
    container.attrs = {
        "Mounts": [{"Type": "volume", "Name": "pg-data"}],
        "NetworkSettings": {"Networks": {}},
        "Config": {"Image": "postgres:16"},
        "State": {"Status": "running"},
    }
    container.image.save.return_value = iter([b"image-bytes"])

    client = MagicMock()
    client.containers.get.return_value = container
    client.version.return_value = {"ApiVersion": "1.45"}
    monkeypatch.setattr(backup_engine, "get_client", lambda: client)
    monkeypatch.setattr(backup_engine, "backup_volume_to_file",
                         lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full mid-volume")))

    result = backup_engine.backup_container("postgres", dest_root=tmp_path, stop_container=True)

    assert result.ok is False
    container.stop.assert_called_once()
    container.start.assert_called_once()  # never left down, even on failure


def test_should_backup_bind_mount_skips_docker_internals():
    assert backup_engine._should_backup_bind_mount("/var/run/docker.sock") is False
    assert backup_engine._should_backup_bind_mount("/proc") is False
    assert backup_engine._should_backup_bind_mount("/proc/1/ns") is False
    assert backup_engine._should_backup_bind_mount("/sys/fs/cgroup") is False
    assert backup_engine._should_backup_bind_mount("/some/other.sock") is False


def test_should_backup_bind_mount_allows_real_data_paths():
    assert backup_engine._should_backup_bind_mount("/mnt/bigdisk/nextcloud-data") is True
    assert backup_engine._should_backup_bind_mount("/srv/app-config") is True


def test_backup_container_archives_bind_mounts_and_skips_denylisted_ones(tmp_path: Path, monkeypatch):
    container = MagicMock()
    container.name = "nextcloud"
    container.attrs = {
        "Mounts": [
            {"Type": "volume", "Name": "nc-db"},
            {"Type": "bind", "Source": "/mnt/bigdisk/nextcloud-data", "Destination": "/var/www/html/data", "RW": True},
            {"Type": "bind", "Source": "/var/run/docker.sock", "Destination": "/var/run/docker.sock", "RW": True},
        ],
        "NetworkSettings": {"Networks": {}},
        "Config": {"Image": "nextcloud:latest"},
    }
    container.image.save.return_value = iter([b"image-bytes"])

    client = _fake_client()
    client.containers.get.return_value = container
    client.version.return_value = {"ApiVersion": "1.45"}

    def fake_helper_run(image, command=None, volumes=None, detach=None, **kwargs):
        # volumes has exactly one key: the volume name or bind source being archived.
        key = next(iter(volumes))
        content = f"data-for-{key}".encode()
        return _fake_helper_container(client, [content])

    client.containers.create.side_effect = fake_helper_run
    monkeypatch.setattr(backup_engine, "get_client", lambda: client)

    result = backup_engine.backup_container("nextcloud", dest_root=tmp_path)

    assert result.ok is True
    meta = json.loads((result.path / "meta.json").read_text())
    assert meta["bind_mounts"] == [{
        "source": "/mnt/bigdisk/nextcloud-data", "destination": "/var/www/html/data",
        "filename": "_var_www_html_data.tar.gz", "rw": True, "is_file": False,
    }]
    # The docker.sock bind mount must not show up anywhere - not archived, not in meta.
    assert (result.path / "binds" / "_var_www_html_data.tar.gz").read_bytes() == b"data-for-/mnt/bigdisk/nextcloud-data"
    assert not (result.path / "binds" / "var_run_docker.sock.tar.gz").exists()
    assert (result.path / "volumes" / "nc-db.tar.gz").exists()


def test_list_landscape_containers_filters_by_name_contains(monkeypatch):
    aio_apache = MagicMock()
    aio_apache.name = "nextcloud-aio-apache"
    aio_apache.labels = {}
    aio_db = MagicMock()
    aio_db.name = "nextcloud-aio-database"
    aio_db.labels = {}
    unrelated = MagicMock()
    unrelated.name = "portainer"
    unrelated.labels = {}

    client = MagicMock()
    client.containers.list.return_value = [aio_apache, aio_db, unrelated]
    monkeypatch.setattr(backup_engine, "get_client", lambda: client)

    result = backup_engine.list_landscape_containers(name_contains="nextcloud-aio")

    assert {c.name for c in result} == {"nextcloud-aio-apache", "nextcloud-aio-database"}


def test_list_landscape_containers_name_contains_is_case_insensitive(monkeypatch):
    container = MagicMock()
    container.name = "Nextcloud-AIO-Apache"
    client = MagicMock()
    client.containers.list.return_value = [container]
    monkeypatch.setattr(backup_engine, "get_client", lambda: client)

    result = backup_engine.list_landscape_containers(name_contains="nextcloud-aio")
    assert len(result) == 1


def test_backup_landscape_uses_name_contains_as_fallback_label(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(backup_engine, "list_landscape_containers",
                         lambda project_filter=None, name_contains=None, exclude_names=None: [])
    result = backup_engine.backup_landscape(dest_root=tmp_path, name_contains="nextcloud-aio")
    assert result.name == "nextcloud-aio"
    assert (tmp_path / "_landscapes" / "nextcloud-aio").exists()


def test_backup_container_stops_at_next_checkpoint_when_cancelled(tmp_path: Path, monkeypatch):
    container = MagicMock()
    container.name = "app"
    container.attrs = {
        "Mounts": [
            {"Type": "volume", "Name": "vol-a"},
            {"Type": "volume", "Name": "vol-b"},
        ],
        "NetworkSettings": {"Networks": {}},
        "Config": {"Image": "app:latest"},
    }
    container.image.save.return_value = iter([b"image-bytes"])

    client = _fake_client()
    client.containers.get.return_value = container
    client.version.return_value = {"ApiVersion": "1.45"}
    client.containers.create.side_effect = lambda *a, **k: _fake_helper_container(client, [b"data"])
    monkeypatch.setattr(backup_engine, "get_client", lambda: client)

    # Cancel once we reach the second volume - vol-a should already be archived.
    archived = []
    real_backup_volume_to_file = backup_engine.backup_volume_to_file

    def tracking_backup_volume_to_file(volume_name, dest, should_cancel=backup_engine._never_cancel, on_bytes=None):
        archived.append(volume_name)
        real_backup_volume_to_file(volume_name, dest, should_cancel=should_cancel)

    monkeypatch.setattr(backup_engine, "backup_volume_to_file", tracking_backup_volume_to_file)
    should_cancel = lambda: len(archived) >= 1  # noqa: E731

    result = backup_engine.backup_container("app", dest_root=tmp_path, should_cancel=should_cancel)

    assert result.cancelled is True
    assert result.ok is False
    assert not result.path.exists()  # cleaned up, same as any other failure
    assert archived == ["vol-a"]  # never got to vol-b


def test_backup_landscape_stops_after_a_cancelled_member(tmp_path: Path, monkeypatch):
    container_a = MagicMock(name="a")
    container_a.name = "app-a"
    container_b = MagicMock(name="b")
    container_b.name = "app-b"
    monkeypatch.setattr(backup_engine, "list_landscape_containers",
                         lambda project_filter=None, name_contains=None, exclude_names=None: [container_a, container_b])

    cancelled_result = BackupResult(ok=False, name="app-a", path=tmp_path / "app-a" / "v1",
                                     error="Backup abgebrochen", cancelled=True)
    calls = []

    def fake_backup_container(name, dest_root, stream_target=None, should_cancel=None, stop_container=None, on_bytes=None, on_upload_bytes=None, on_log=None):
        calls.append(name)
        return cancelled_result

    monkeypatch.setattr(backup_engine, "backup_container", fake_backup_container)

    result = backup_engine.backup_landscape(dest_root=tmp_path, should_cancel=lambda: False)

    assert calls == ["app-a"]  # stopped immediately after the cancelled member, never reached app-b
    assert result.cancelled is True
    assert result.ok is False


def test_backup_landscape_passes_stop_containers_to_each_member(tmp_path: Path, monkeypatch):
    container_a = MagicMock(name="a")
    container_a.name = "app-a"
    container_b = MagicMock(name="b")
    container_b.name = "app-b"
    monkeypatch.setattr(backup_engine, "list_landscape_containers",
                         lambda project_filter=None, name_contains=None, exclude_names=None: [container_a, container_b])

    captured = []

    def fake_backup_container(name, dest_root, stream_target=None, should_cancel=None, stop_container=None, on_bytes=None, on_upload_bytes=None, on_log=None):
        captured.append((name, stop_container))
        return BackupResult(ok=True, name=name, path=tmp_path / name / "v1", size_bytes=1)

    monkeypatch.setattr(backup_engine, "backup_container", fake_backup_container)

    backup_engine.backup_landscape(dest_root=tmp_path, stop_containers=True)

    assert captured == [("app-a", True), ("app-b", True)]


def test_backup_landscape_carries_member_results_for_tracking(tmp_path: Path, monkeypatch):
    container_a = MagicMock(name="a")
    container_a.name = "app-a"
    container_b = MagicMock(name="b")
    container_b.name = "app-b"
    monkeypatch.setattr(backup_engine, "list_landscape_containers",
                         lambda project_filter=None, name_contains=None, exclude_names=None: [container_a, container_b])

    canned_results = {
        "app-a": BackupResult(ok=True, name="app-a", path=tmp_path / "app-a" / "v1", size_bytes=123),
        "app-b": BackupResult(ok=False, name="app-b", path=tmp_path / "app-b" / "v1", error="boom"),
    }
    monkeypatch.setattr(backup_engine, "backup_container",
                         lambda name, dest_root, stream_target=None, should_cancel=None, stop_container=None, on_bytes=None, on_upload_bytes=None, on_log=None: canned_results[name])

    result = backup_engine.backup_landscape(dest_root=tmp_path)

    assert [m.name for m in result.member_results] == ["app-a", "app-b"]
    assert result.member_results[0].ok is True
    assert result.member_results[0].size_bytes == 123
    assert result.member_results[1].ok is False
    assert result.member_results[1].error == "boom"
    assert result.ok is False  # one member failed
    assert "app-b: boom" in result.error
