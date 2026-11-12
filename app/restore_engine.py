"""
Restores a container backup produced by backup_engine.backup_container onto
any Docker host (same machine or a different OS entirely), by:
  1. loading the saved image,
  2. recreating any missing custom networks,
  3. recreating any missing named volumes and restoring their data,
  4. recreating the container from the saved inspect config.

Note: this covers the common subset of container configuration (env, command,
entrypoint, labels, ports, binds/volumes, restart policy, network attachments,
capabilities, privileged mode). Highly exotic configurations may need manual
adjustment after restore.
"""
from __future__ import annotations

import json
import logging
import tarfile as _tarfile
import tempfile
from pathlib import Path
from typing import Callable, Optional

from app import encryption, restic_engine, storage_sync

logger = logging.getLogger("dbm.restore")
from app.backup_engine import ProgressCallback, StreamTarget, _noop_progress, restore_volume_from_file, restore_volume_from_tar, sanitize_name
from app.config import BASE_DIR, BACKUPS_DIR, DOCKER_HELPER_IMAGE, container_path_to_host
from app.docker_client import get_client

_BIND_REMAP_ROOT = BASE_DIR / "bind_mounts"


def _remap_bind_mount(
    mount: dict,
    bind_mounts_meta: list,
    original_container_name: str,
    is_file: Optional[bool] = None,
) -> str:
    """Redirect a bind-mount source to a stable path under _BIND_REMAP_ROOT.

    DBM's /data is mounted from a real host directory, so paths under it are
    always accessible to dockerd — regardless of NAS filesystem restrictions or
    whether this is a cross-machine restore.

    For DIRECTORY mounts:
        mount["Source"]  → remap_dir  (host path of the directory)
        bm["source"]     → remap_dir  (restore step extracts contents here)

    For FILE mounts:
        mount["Source"]  → remap_dir/data  (the actual file after extraction)
        bm["source"]     → remap_dir       (restore step extracts to this dir;
                                            entry "data" → remap_dir/data)

    The backup pipeline creates file archives with `tar cf - /data` where
    /data is the bind-mounted FILE, producing a tar entry named "data".
    Extraction to remap_dir therefore creates remap_dir/data.

    is_file: use the value stored in backup metadata when available; when None
    fall back to a suffix heuristic (handles old backups without the field).

    Returns the original source path (before the redirect).
    """
    src = mount["Source"]
    dst = mount.get("Destination", "")
    safe_src = sanitize_name(src) or "bind"
    remap_dir = _BIND_REMAP_ROOT / sanitize_name(original_container_name) / safe_src
    remap_dir.mkdir(parents=True, exist_ok=True)

    if is_file is None:
        # Fallback heuristic for old backups without the is_file field.
        # A suffix on source OR destination strongly suggests a single-file mount.
        is_file = bool(Path(src).suffix or Path(dst).suffix)

    restore_target = container_path_to_host(remap_dir)
    if is_file:
        new_mount_src = container_path_to_host(remap_dir / "data")
        label = "Datei-Bind-Mount"
    else:
        new_mount_src = restore_target
        label = "Verzeichnis-Bind-Mount"

    mount["Source"] = new_mount_src
    for bm in bind_mounts_meta:
        if bm.get("source") == src:
            bm["source"] = restore_target

    logger.info("%s '%s' → '%s'", label, src, new_mount_src)
    return src  # original source, useful to callers


def _build_create_kwargs(container_json: dict, new_name: Optional[str], image_ref: str,
                         volume_name_map: Optional[dict] = None) -> dict:
    config = container_json.get("Config", {})
    host_config = container_json.get("HostConfig", {})

    ports = {}
    for cport, bindings in (host_config.get("PortBindings") or {}).items():
        if not bindings:
            ports[cport] = None
            continue
        mapped = []
        for b in bindings:
            host_ip = b.get("HostIp") or ""
            host_port = b.get("HostPort")
            if host_ip:
                mapped.append((host_ip, host_port))
            else:
                mapped.append(host_port)
        ports[cport] = mapped if len(mapped) > 1 else mapped[0]

    volumes = {}
    for mount in container_json.get("Mounts", []):
        if mount.get("Type") == "volume":
            src = (volume_name_map or {}).get(mount["Name"], mount["Name"])
            volumes[src] = {"bind": mount["Destination"], "mode": "rw" if mount.get("RW", True) else "ro"}
        elif mount.get("Type") == "bind":
            volumes[mount["Source"]] = {"bind": mount["Destination"], "mode": "rw" if mount.get("RW", True) else "ro"}

    restart_policy = host_config.get("RestartPolicy")
    if restart_policy and not restart_policy.get("Name"):
        restart_policy = None

    kwargs = dict(
        image=image_ref,
        name=new_name or container_json.get("Name", "").lstrip("/"),
        command=config.get("Cmd"),
        entrypoint=config.get("Entrypoint"),
        environment=config.get("Env") or [],
        labels=config.get("Labels") or {},
        working_dir=config.get("WorkingDir") or None,
        hostname=config.get("Hostname") or None,
        user=config.get("User") or None,
        ports=ports or None,
        volumes=volumes or None,
        restart_policy=restart_policy,
        privileged=host_config.get("Privileged", False),
        cap_add=host_config.get("CapAdd") or None,
        cap_drop=host_config.get("CapDrop") or None,
        detach=True,
    )
    if host_config.get("NetworkMode") not in (None, "default"):
        kwargs["network_mode"] = host_config.get("NetworkMode")

    return {k: v for k, v in kwargs.items() if v is not None}


def restore_container(backup_dir: Path, new_name: Optional[str] = None, start: bool = True,
                       on_progress: ProgressCallback = _noop_progress,
                       stream_target: Optional[StreamTarget] = None,
                       rename_volumes: bool = True,
                       volume_base_dir: Optional[str] = None,
                       overwrite: bool = False):
    backup_dir = Path(backup_dir)
    relative_key = storage_sync._relative_key(backup_dir)

    if not backup_dir.exists():
        if not stream_target:
            raise RuntimeError(
                "Dieses Backup existiert lokal nicht (z. B. nach einem Katalog-Import von einem "
                "Speicherziel) und es wurde kein Speicherziel zum Nachladen angegeben - "
                "Wiederherstellung nicht möglich."
            )
        on_progress(0, "Lade Backup vom Speicherziel herunter", 1)
        target_type, target_config_json, _target_id = stream_target
        storage_sync.download_full_backup_from_target(target_type, target_config_json, relative_key, backup_dir)

    if encryption.is_backup_encrypted(backup_dir):
        on_progress(0, "Decrypting backup", 1)
        with encryption.decrypt_directory_to_temp(backup_dir) as tmp_dir:
            return _restore_from_plaintext_dir(Path(tmp_dir), new_name, start, on_progress,
                                                stream_target, relative_key, rename_volumes,
                                                volume_base_dir, overwrite)
    return _restore_from_plaintext_dir(backup_dir, new_name, start, on_progress, stream_target,
                                        relative_key, rename_volumes, volume_base_dir, overwrite)


def _restore_from_plaintext_dir(backup_dir: Path, new_name: Optional[str], start: bool,
                                 on_progress: ProgressCallback,
                                 stream_target: Optional[StreamTarget], relative_key: str,
                                 rename_volumes: bool = True,
                                 volume_base_dir: Optional[str] = None,
                                 overwrite: bool = False):
    client = get_client()

    container_json = json.loads((backup_dir / "container.json").read_text())
    networks_json = {}
    networks_path = backup_dir / "networks.json"
    if networks_path.exists():
        networks_json = json.loads(networks_path.read_text())

    meta = json.loads((backup_dir / "meta.json").read_text()) if (backup_dir / "meta.json").exists() else {}
    streamed_target_id = meta.get("streamed_target_id")
    bind_mounts_meta = meta.get("bind_mounts", [])

    # Remove the existing container now that we have the plaintext container name.
    # (The overwrite check in restore_container can't do this for encrypted backups
    # because container.json is not readable before decryption.)
    if overwrite:
        target_name = new_name or container_json.get("Name", "").lstrip("/")
        if target_name:
            try:
                existing = client.containers.get(target_name)
                existing.remove(force=True)
                logger.info("Vorhandener Container '%s' wurde entfernt (overwrite=True)", target_name)
            except Exception as _rm_exc:
                if "No such container" not in str(_rm_exc) and "404" not in str(_rm_exc):
                    logger.warning("Konnte vorhandenen Container '%s' nicht entfernen: %s", target_name, _rm_exc)

    original_container_name = meta.get("container_name") or container_json.get("Name", "").lstrip("/")
    effective_name = new_name or original_container_name

    def _map_vol(vol_name: str) -> str:
        mapped = vol_name
        if rename_volumes and new_name and original_container_name and vol_name.startswith(original_container_name):
            mapped = effective_name + vol_name[len(original_container_name):]
        if volume_base_dir:
            return str(Path(volume_base_dir) / mapped)
        return mapped

    all_named_vols = [m["Name"] for m in container_json.get("Mounts", []) if m.get("Type") == "volume"]
    volume_name_map = {v: _map_vol(v) for v in all_named_vols}

    # Build a lookup: original_source → is_file, from backup metadata.
    # is_file is stored since backup_engine v1.4.5; older backups don't have it
    # (None = unknown, _remap_bind_mount will fall back to a suffix heuristic).
    bind_is_file: dict[str, Optional[bool]] = {
        bm["source"]: bm.get("is_file")
        for bm in bind_mounts_meta
    }

    # Remap bind mounts whose source paths don't exist on this machine.
    # We track which mounts were remapped here so the retry block below can
    # skip them (they already point to _BIND_REMAP_ROOT, which dockerd can
    # always reach — they cannot be the cause of a subsequent start failure).
    remapped_mount_ids: set[int] = set()
    for mount in container_json.get("Mounts", []):
        if mount.get("Type") != "bind":
            continue
        src = mount["Source"]
        if Path(src).exists():
            # Accessible inside DBM — same machine or the path is mounted in.
            # Keep the original source; the retry block handles the rare case
            # where the path exists in DBM but dockerd still can't use it.
            continue
        # Path not found in DBM → cross-machine restore or not mounted.
        # Do NOT attempt mkdir: inside a container overlay mkdir always
        # succeeds in the overlay layer, never on the real host, masking
        # the problem and sending data to the wrong place.
        _remap_bind_mount(mount, bind_mounts_meta, original_container_name,
                          is_file=bind_is_file.get(src))
        remapped_mount_ids.add(id(mount))

    if streamed_target_id is not None:
        if not stream_target:
            raise RuntimeError(
                "Dieses Backup wurde direkt zu einem Speicherziel gestreamt, aber das Ziel ist nicht "
                "mehr verfügbar (gelöscht oder deaktiviert) - Wiederherstellung nicht möglich."
            )
        volume_names = meta.get("volumes", [])
        staging_root = BACKUPS_DIR / ".tmp"
        staging_root.mkdir(parents=True, exist_ok=True)
        stage_dir_ctx = tempfile.TemporaryDirectory(dir=staging_root)
        stage_dir = Path(stage_dir_ctx.name)

        if meta.get("backup_engine") == "restic":
            target_type, target_config_json, _target_id = stream_target
            target_config = json.loads(target_config_json)
            container_name = meta.get("container_name", "")
            snapshot_ids: dict = meta.get("restic_snapshot_ids", {})
            password = meta.get("restic_password") or restic_engine.get_password()
            repo_url, r_env, smb_conf_path = restic_engine.repo_url_and_env(
                target_type, target_config, container_name
            )
            try:
                volume_files = []
                for vol_name in volume_names:
                    sid = snapshot_ids.get(vol_name)
                    if not sid:
                        raise RuntimeError(f"Kein Restic-Snapshot für Volume '{vol_name}' gefunden")
                    dest = stage_dir / f"{sanitize_name(vol_name)}.tar"
                    restic_engine.dump_snapshot_to_file(repo_url, r_env, password, sid, dest)
                    volume_files.append((vol_name, dest, "tar"))
                bind_files_restic = []
                for bind in bind_mounts_meta:
                    bind_key = f"bind_{sanitize_name(bind['destination'])}"
                    sid = snapshot_ids.get(bind_key)
                    if not sid:
                        raise RuntimeError(f"Kein Restic-Snapshot für Bind-Mount '{bind['destination']}' gefunden")
                    dest = stage_dir / f"{sanitize_name(bind['destination'])}.tar"
                    restic_engine.dump_snapshot_to_file(repo_url, r_env, password, sid, dest)
                    bind_files_restic.append((bind["source"], dest))
            finally:
                if smb_conf_path:
                    try:
                        restic_engine.rclone_backends.discard_conf(smb_conf_path)
                    except Exception:
                        pass
            _restic_restore = True
        else:
            target_type, target_config_json, _target_id = stream_target
            volume_files = []
            for vol_name in volume_names:
                dest = stage_dir / f"{sanitize_name(vol_name)}.tar.gz"
                storage_sync.download_from_target(
                    target_type, target_config_json, f"{relative_key}/volumes/{sanitize_name(vol_name)}.tar.gz", dest,
                )
                volume_files.append(dest)
            bind_files = []
            for bind in bind_mounts_meta:
                dest = stage_dir / bind["filename"]
                storage_sync.download_from_target(
                    target_type, target_config_json, f"{relative_key}/binds/{bind['filename']}", dest,
                )
                bind_files.append((bind["source"], dest))
            _restic_restore = False
    else:
        stage_dir_ctx = None
        _restic_restore = False
        volume_files = sorted((backup_dir / "volumes").glob("*.tar.gz")) if (backup_dir / "volumes").exists() else []
        binds_dir = backup_dir / "binds"
        bind_files = [(bind["source"], binds_dir / bind["filename"]) for bind in bind_mounts_meta
                      if (binds_dir / bind["filename"]).exists()]

    try:
        if _restic_restore:
            all_vol_entries = volume_files
            all_bind_entries = bind_files_restic
        else:
            all_vol_entries = [(f.name[:-len(".tar.gz")], f, "tar.gz") for f in volume_files]
            all_bind_entries = bind_files

        total_steps = 3 + len(all_vol_entries) + len(all_bind_entries)
        step = 1

        on_progress(step, "Loading image", total_steps)
        local_image_tar = backup_dir / "image.tar"
        if not local_image_tar.exists() and streamed_target_id is not None and stream_target:
            target_type, target_config_json, _target_id = stream_target
            dl_dest = stage_dir / "image.tar"
            storage_sync.download_from_target(
                target_type, target_config_json, f"{relative_key}/image.tar", dl_dest,
            )
            local_image_tar = dl_dest
        with open(local_image_tar, "rb") as f:
            loaded = client.images.load(f)
        image_ref = loaded[0].tags[0] if loaded and loaded[0].tags else loaded[0].id

        step += 1
        on_progress(step, "Recreating networks", total_steps)
        existing_networks = {n.name for n in client.networks.list()}
        for net_name, net_attrs in networks_json.items():
            if net_name in existing_networks:
                continue
            driver = net_attrs.get("Driver", "bridge")
            client.networks.create(net_name, driver=driver)

        for vol_name, vol_file, fmt in all_vol_entries:
            step += 1
            mapped = _map_vol(vol_name)
            on_progress(step, f"Restoring volume {mapped}", total_steps)
            if volume_base_dir:
                Path(mapped).mkdir(parents=True, exist_ok=True)
            else:
                existing_volumes = {v.name for v in client.volumes.list()}
                if mapped not in existing_volumes:
                    client.volumes.create(name=mapped)
            if fmt == "tar":
                restore_volume_from_tar(mapped, vol_file)
            else:
                restore_volume_from_file(mapped, vol_file)

        for source, bind_file in all_bind_entries:
            step += 1
            on_progress(step, f"Restoring bind mount {source}", total_steps)
            if _restic_restore:
                restore_volume_from_tar(source, bind_file)
            else:
                restore_volume_from_file(source, bind_file)
    finally:
        if stage_dir_ctx is not None:
            stage_dir_ctx.cleanup()

    step += 1
    on_progress(step, "Creating container", total_steps)
    create_kwargs = _build_create_kwargs(container_json, new_name, image_ref, volume_name_map)

    try:
        container = client.containers.create(**create_kwargs)
    except Exception as exc:
        msg = str(exc)
        if "Conflict" in msg or "already in use" in msg:
            cname = create_kwargs.get("name", "")
            raise RuntimeError(
                f"Container '{cname}' existiert bereits auf diesem System. "
                f"Aktiviere die Option 'Vorhandenen Container überschreiben' und starte den Restore erneut."
            ) from exc
        raise

    for net_name in networks_json.keys():
        try:
            client.networks.get(net_name).connect(container)
        except Exception:  # noqa: BLE001
            pass

    if start:
        try:
            container.start()
        except Exception as exc:
            # dockerd creates bind-mount source paths on the HOST, which may fail
            # even when the path is accessible inside the DBM container (e.g. the
            # Synology /volume1 filesystem is read-only for the Docker daemon).
            # Redirect only the mounts that were NOT already remapped by the early
            # remap block above (those already point to _BIND_REMAP_ROOT and cannot
            # have caused this failure).
            msg = str(exc)
            if "creating mount source path" not in msg and "read-only file system" not in msg:
                raise
            logger.info(
                "container.start() fehlgeschlagen (Bind-Mount-Pfad für dockerd nicht erreichbar) "
                "— leite verbleibende Bind-Mounts nach /data/bind_mounts um und versuche erneut"
            )
            container.remove(force=True)

            for mount in container_json.get("Mounts", []):
                if mount.get("Type") != "bind":
                    continue
                if id(mount) in remapped_mount_ids:
                    # Already redirected to _BIND_REMAP_ROOT — cannot be the
                    # cause of this failure, skip.
                    continue

                old_src = mount["Source"]
                is_file = bind_is_file.get(old_src)
                safe_src = sanitize_name(old_src) or "bind"
                remap_dir = _BIND_REMAP_ROOT / sanitize_name(original_container_name) / safe_src
                remap_dir.mkdir(parents=True, exist_ok=True)

                # Resolve is_file if not in metadata
                if is_file is None:
                    is_file = bool(Path(old_src).suffix or Path(mount.get("Destination", "")).suffix)

                # Best-effort: copy existing data from the old path into remap_dir
                # before redirecting the mount, so the container keeps its data.
                old_path = Path(old_src)
                if old_path.exists():
                    tar_tmp = remap_dir / "_mig.tar"
                    try:
                        with _tarfile.open(str(tar_tmp), "w") as tf:
                            # arcname="data" for files matches the backup pipeline's
                            # `tar cf - /data` convention (entry "data").
                            tf.add(str(old_path), arcname="data" if is_file else ".")
                        tar_host = container_path_to_host(tar_tmp)
                        client.containers.run(
                            DOCKER_HELPER_IMAGE,
                            command="tar xf /mig.tar -C /dst",
                            volumes={
                                tar_host: {"bind": "/mig.tar", "mode": "ro"},
                                container_path_to_host(remap_dir): {"bind": "/dst", "mode": "rw"},
                            },
                            remove=True,
                        )
                    except Exception as mig_exc:
                        logger.warning("Datenmigration für '%s' fehlgeschlagen: %s", old_src, mig_exc)
                    finally:
                        try:
                            tar_tmp.unlink(missing_ok=True)
                        except Exception:
                            pass

                _remap_bind_mount(mount, bind_mounts_meta, original_container_name, is_file=is_file)
                remapped_mount_ids.add(id(mount))

            retry_kwargs = _build_create_kwargs(container_json, new_name, image_ref, volume_name_map)
            container = client.containers.create(**retry_kwargs)
            for net_name in networks_json.keys():
                try:
                    client.networks.get(net_name).connect(container)
                except Exception:  # noqa: BLE001
                    pass
            container.start()

    return container
