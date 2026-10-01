from __future__ import annotations

import json
import os
import time

from pipeline.tts import manifest


def test_write_manifest_path_and_content(tmp_path) -> None:
    path = manifest.write_manifest(
        tmp_path, feed_slug="fp-digest", episode_id="ep1", record={"a": 1}
    )
    assert path is not None
    assert path.parent == tmp_path / "fp-digest"
    assert path.name.startswith("ep1-") and path.name.endswith("Z.json")
    assert json.loads(path.read_text()) == {"a": 1}
    assert not list(path.parent.glob("*.tmp"))


def test_write_manifest_unwritable_returns_none_and_warns(tmp_path, caplog) -> None:
    blocker = tmp_path / "f"
    blocker.write_text("x")
    assert (
        manifest.write_manifest(blocker, feed_slug="s", episode_id="e", record={})
        is None
    )
    assert "manifest" in caplog.text.lower()


def test_prune_manifests_removes_only_old_json(tmp_path) -> None:
    old = manifest.write_manifest(tmp_path, feed_slug="s", episode_id="old", record={})
    new = manifest.write_manifest(tmp_path, feed_slug="s", episode_id="new", record={})
    stamp = time.time() - 90 * 86400
    os.utime(old, (stamp, stamp))
    manifest.prune_manifests(tmp_path, max_age_days=60)
    assert not old.exists()
    assert new.exists()


def test_prune_manifests_never_raises(tmp_path) -> None:
    manifest.prune_manifests(tmp_path / "absent")
    blocker = tmp_path / "f"
    blocker.write_text("x")
    manifest.prune_manifests(blocker)


def test_write_manifest_sanitizes_path_components(tmp_path) -> None:
    path = manifest.write_manifest(
        tmp_path / "m", feed_slug="../evil/feed", episode_id="a/b/../c", record={}
    )
    assert path is not None
    assert path.resolve().is_relative_to((tmp_path / "m").resolve())
    assert path.parent.parent == tmp_path / "m"
    assert "/" not in path.parent.name and not path.parent.name.startswith(".")
    assert path.name.startswith("a-b-..-c-")


def test_write_manifest_dotdot_ids_cannot_escape(tmp_path) -> None:
    path = manifest.write_manifest(
        tmp_path / "m", feed_slug="..", episode_id="..", record={}
    )
    assert path is not None
    assert path.resolve().is_relative_to((tmp_path / "m").resolve())
    assert not path.name.startswith(".")


def test_write_manifest_long_episode_id_is_truncated(tmp_path) -> None:
    path = manifest.write_manifest(
        tmp_path, feed_slug="fp", episode_id="x" * 300, record={"a": 1}
    )
    assert path is not None
    assert len(path.name) <= 200
    assert path.name.endswith("Z.json")
    assert json.loads(path.read_text()) == {"a": 1}


def _clip(tmp_path, feed: str, name: str, age_days: float):
    d = tmp_path / feed / manifest.OMISSION_AUDIO_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    clip = d / name
    clip.write_bytes(b"ID3")
    stamp = time.time() - age_days * 86400
    os.utime(clip, (stamp, stamp))
    return clip


def test_omission_audio_dir_is_under_the_feed_and_sanitized(tmp_path) -> None:
    assert (
        manifest.omission_audio_dir(tmp_path, "fp-digest")
        == tmp_path / "fp-digest" / "omission-audio"
    )
    evil = manifest.omission_audio_dir(tmp_path, "../evil/feed")
    assert evil.resolve().is_relative_to(tmp_path.resolve())
    assert evil.name == "omission-audio" and evil.parent.parent == tmp_path


def test_omission_clip_path_matches_the_manifest_naming(tmp_path) -> None:
    stamp = manifest.utc_stamp()
    path = manifest.omission_clip_path(tmp_path, "a/b", stamp, chunk=3, attempt=2)
    assert path.parent == tmp_path
    assert path.name == f"a-b-{stamp}-c0003-a2.mp3"
    long = manifest.omission_clip_path(tmp_path, "x" * 300, stamp, chunk=0, attempt=1)
    assert len(long.name) <= 200 and long.name.endswith("-c0000-a1.mp3")


def test_prune_manifests_removes_an_old_omission_clip_and_keeps_a_new_one(
    tmp_path,
) -> None:
    old = _clip(tmp_path, "fp-digest", "old-c0000-a1.mp3", age_days=90)
    new = _clip(tmp_path, "fp-digest", "new-c0000-a1.mp3", age_days=1)
    manifest.prune_manifests(tmp_path, max_age_days=60)
    assert not old.exists()
    assert new.exists()


def test_prune_leaves_other_files_in_the_clip_dir_alone(tmp_path) -> None:
    other = _clip(tmp_path, "fp-digest", "notes.txt", age_days=90)
    manifest.prune_manifests(tmp_path, max_age_days=60)
    assert other.exists()  # only *.mp3 clips are ours to remove


def test_prune_covers_manifests_and_clips_together(tmp_path) -> None:
    old_manifest = manifest.write_manifest(
        tmp_path, feed_slug="s", episode_id="old", record={}
    )
    stamp = time.time() - 90 * 86400
    os.utime(old_manifest, (stamp, stamp))
    old_clip = _clip(tmp_path, "s", "old-c0000-a1.mp3", age_days=90)
    manifest.prune_manifests(tmp_path, max_age_days=60)
    assert not old_manifest.exists() and not old_clip.exists()


def test_prune_removes_stale_encode_temp_files_in_the_clip_dir(tmp_path) -> None:
    # encode_mp3 writes ``<clip>.mp3.<random>.tmp`` and renames it on success; a
    # killed encode would otherwise leave it forever.
    old = _clip(tmp_path, "fp-digest", "ep-c0000-a1.mp3.abc123.tmp", age_days=90)
    new = _clip(tmp_path, "fp-digest", "ep-c0001-a1.mp3.def456.tmp", age_days=0.01)
    manifest.prune_manifests(tmp_path, max_age_days=60)
    assert not old.exists()
    assert new.exists()  # same age rule: a live encode's temp file is left alone
