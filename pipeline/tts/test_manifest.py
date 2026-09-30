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
