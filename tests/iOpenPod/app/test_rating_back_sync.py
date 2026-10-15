"""Ratings return only to the paired Host file after a verified Sync."""

import base64
import io
from pathlib import Path
from typing import Any, BinaryIO, cast

import pytest
from mutagen.id3 import POPM, TIT2
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4FreeForm

from iOpenPod.app.media.ratings import rewrite_rating_stream
from iOpenPod.app.media.tags import read_tag_values

FIXTURES = Path(__file__).parents[2] / "fixtures" / "media"


def _load_media(stream: BinaryIO | Path, suffix: str) -> Any:
    factory: Any = MP3 if suffix == "mp3" else MP4
    return factory(stream)


@pytest.mark.parametrize("suffix", ["mp3", "m4a"])
def test_rating_round_trip_preserves_other_tags_and_payload(suffix: str) -> None:
    data = base64.b64decode((FIXTURES / f"tone.{suffix}.b64").read_bytes())
    stream = io.BytesIO(data)
    if suffix == "mp3":
        audio = _load_media(stream, suffix)
        if audio.tags is None:
            audio.add_tags()
        audio.tags.add(cast("Any", TIT2)(encoding=3, text=["Keep title"]))
        audio.tags.add(cast("Any", POPM)(email="existing", rating=64, count=17))
        audio.save(stream)
    else:
        audio = _load_media(stream, suffix)
        audio["©nam"] = ["Keep title"]
        audio["----:com.apple.iTunes:PRIVATE"] = [cast("Any", MP4FreeForm)(b"retain")]
        audio.save(stream)
    stream.seek(0)
    before = stream.getvalue()
    rewrite_rating_stream(stream, f"track.{suffix}", 80)
    stream.seek(0)
    checked = _load_media(stream, suffix)
    tags = {tag.name: tag.value for tag in read_tag_values(checked.tags)}
    assert tags["rating"] == "80"
    assert tags["title"] == "Keep title"
    if suffix == "mp3":
        assert checked.tags.getall("POPM")[0].count == 17
        assert (
            before[before.index(b"\xff\xfb") :]
            == stream.getvalue()[stream.getvalue().index(b"\xff\xfb") :]
        )
    else:
        assert checked.tags["----:com.apple.iTunes:PRIVATE"] == [b"retain"]

        def payload(data: bytes) -> bytes:
            start = data.index(b"mdat") - 4
            size = int.from_bytes(data[start : start + 4], "big")
            return data[start + 8 : start + size]

        assert payload(before) == payload(stream.getvalue())
    written = stream.getvalue()
    rewrite_rating_stream(stream, f"track.{suffix}", 80)
    assert stream.getvalue() == written


@pytest.mark.parametrize("rating", [-1, 21, 101])
def test_invalid_rating_is_rejected(rating: int) -> None:
    with pytest.raises(ValueError):
        rewrite_rating_stream(io.BytesIO(b"unchanged"), "track.mp3", rating)


@pytest.mark.parametrize(
    "host,ipod,baseline_host,baseline_ipod,expected",
    [
        (60, 60, 60, 60, False),
        (60, 80, 60, 60, True),
        (60, 20, 60, 60, True),
        (60, 0, 60, 60, False),
        (100, 80, 60, 60, False),
        (60, 80, None, None, False),
        (60, 80, 60, 80, False),
    ],
)
def test_only_changed_nonzero_ipod_rating_with_unchanged_host_is_written(
    host: int,
    ipod: int,
    baseline_host: int | None,
    baseline_ipod: int | None,
    expected: bool,
) -> None:
    from iOpenPod.app.back_sync import rating_needs_back_sync

    assert rating_needs_back_sync(host, ipod, baseline_host, baseline_ipod) is expected


@pytest.mark.parametrize("suffix", ["mp3", "m4a"])
@pytest.mark.parametrize(
    "case",
    [
        "success",
        "excluded",
        "write_failure",
        "device_failure",
        "cancelled",
        "zero",
        "conflict",
        "legacy",
    ],
)
def test_sync_writes_changed_rating_to_paired_host_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str, case: str
) -> None:
    from dataclasses import replace
    from threading import Event

    from tests.iOpenPod.app.services.test_first_artwork_save import bare_device

    from iOpenPod.app.host_media_folders import create_host_media_folder
    from iOpenPod.app.host_media_library import HostMediaLibrary, HostMediaScanner
    from iOpenPod.app.library_sync_helper import (
        LIBRARY_SYNC_HELPER_PATH,
        IPodMediaCacheStats,
        IPodMediaLibrary,
        _decode_helper,  # pyright: ignore[reportPrivateUsage]
    )
    from iOpenPod.app.sync_execution import (
        SyncExecutionRequest,
        SyncExecutionStatus,
        SyncExecutor,
    )
    from iOpenPod.app.sync_plan import (
        SyncPlanAction,
        prepare_sync_plan,
        select_sync_plan,
    )
    from storage import AtomicHostFile

    monkeypatch.setattr(
        "iOpenPod.app.host_media_fingerprint.FpcalcFingerprinter.fingerprint",
        lambda *args, **kwargs: "1,2,3",
    )
    folder = tmp_path / "host"
    folder.mkdir()
    path = folder / f"track.{suffix}"
    stream = io.BytesIO(
        base64.b64decode((FIXTURES / f"tone.{suffix}.b64").read_bytes())
    )
    rewrite_rating_stream(stream, path.name, 60)
    path.write_bytes(stream.getvalue())
    scanner = HostMediaScanner(AtomicHostFile(tmp_path / "cache.json"))

    def scan() -> HostMediaLibrary:
        pending = scanner.scan(
            (create_host_media_folder(folder),), checkpoint=lambda: None
        )
        return scanner.complete(pending, frozenset(), checkpoint=lambda: None)

    device = bare_device(tmp_path, model_number="M9802")
    try:
        active = device.active
        ipod = IPodMediaLibrary((), (), (), IPodMediaCacheStats(), None, False)
        host = scan()
        executor = SyncExecutor(device.coordinator)
        plan = select_sync_plan(
            prepare_sync_plan(host, ipod, active.library),
            selected_host_paths=frozenset({str(path)}),
            selected_ipod_removals=frozenset(),
        )
        result = executor.execute(
            SyncExecutionRequest(plan, host, ipod, active, 1, 1),
            lambda _: None,
            Event(),
        )
        assert result.status is SyncExecutionStatus.SUCCESS, [
            (i.code, i.message, i.detail) for i in result.issues
        ]
        assert result.active is not None and result.helper is not None
        active, ipod = result.active, result.helper
        persisted = _decode_helper(
            (device.root / str(LIBRARY_SYNC_HELPER_PATH)).read_bytes()
        )
        baseline = next(
            record.sync
            for record in persisted.tracks
            if record.sync is not None and record.sync.host_path_hint == str(path)
        )
        assert baseline.host_rating == 60 and baseline.ipod_rating == 60
        track = next(
            t for t in active.library.tracks if t.metadata.location.endswith(suffix)
        )
        assert track.rating == 60
        if case == "legacy":
            ipod = replace(
                ipod,
                tracks=tuple(
                    replace(
                        record,
                        sync=replace(record.sync, host_rating=None, ipod_rating=None),
                    )
                    if record.sync is not None
                    else record
                    for record in ipod.tracks
                ),
            )
        changed = replace(track, rating=0 if case == "zero" else 80)
        review = device.prepare(
            replace(
                active.library,
                tracks=tuple(
                    changed if t.track_id == track.track_id else t
                    for t in active.library.tracks
                ),
            )
        )
        saved = device.coordinator.save_library(review, active, lambda _: None, Event())
        assert saved.active is not None
        active = saved.active
        if case in {"zero", "conflict", "legacy"}:
            with path.open("r+b") as writable:
                if case == "conflict":
                    rewrite_rating_stream(writable, path.name, 100)
                writable.seek(0)
                tagged = _load_media(writable, suffix)
                if suffix == "mp3":
                    tagged.tags.add(
                        cast("Any", TIT2)(encoding=3, text=["Updated Host title"])
                    )
                else:
                    tagged["©nam"] = ["Updated Host title"]
                tagged.save(writable)
        host = scan()
        plan = select_sync_plan(
            prepare_sync_plan(host, ipod, active.library),
            selected_host_paths=frozenset({str(path)}),
            selected_ipod_removals=frozenset(),
        )
        assert any(item.action is SyncPlanAction.UPDATE for item in plan.items)
        original = path.read_bytes()
        if case == "excluded":
            plan = replace(plan, items=())
        if case == "write_failure":

            def fail_write(*args: object, **kwargs: object) -> None:
                raise OSError("Host file is read-only")

            monkeypatch.setattr("iOpenPod.app.back_sync.replace_host_file", fail_write)
        if case == "device_failure":

            def fail_save(*args: object, **kwargs: object) -> None:
                raise OSError("Device publication failed")

            monkeypatch.setattr(device.coordinator, "save_library", fail_save)
        cancelled = Event()
        if case == "cancelled":
            cancelled.set()
        result = executor.execute(
            SyncExecutionRequest(plan, host, ipod, active, 1, 1),
            lambda _: None,
            cancelled,
        )
        if case in {"excluded", "write_failure", "device_failure", "cancelled"}:
            assert path.read_bytes() == original
            assert (
                result.status
                is {
                    "excluded": SyncExecutionStatus.SUCCESS,
                    "write_failure": SyncExecutionStatus.PARTIAL,
                    "device_failure": SyncExecutionStatus.FAILED,
                    "cancelled": SyncExecutionStatus.CANCELLED,
                }[case]
            ), result.issues
            if case == "write_failure":
                assert result.active is not None and result.helper is not None
                retry = prepare_sync_plan(scan(), result.helper, result.active.library)
                assert any(i.action is SyncPlanAction.UPDATE for i in retry.items)
            return
        assert result.status is SyncExecutionStatus.SUCCESS, result.issues
        assert result.active is not None and result.helper is not None
        audio = _load_media(path, suffix)
        assert {tag.name: tag.value for tag in read_tag_values(audio.tags)}[
            "rating"
        ] == (
            "100"
            if case == "conflict"
            else "60"
            if case in {"zero", "legacy"}
            else "80"
        )
        if case == "conflict":
            assert any(issue.code == "sync.rating_conflict" for issue in result.issues)
        after = path.read_bytes()
        plan = prepare_sync_plan(scan(), result.helper, result.active.library)
        assert all(item.action is not SyncPlanAction.UPDATE for item in plan.items)
        assert path.read_bytes() == after
    finally:
        device.coordinator.close()


def test_v14_mp4_cache_refreshes_previously_unread_rating(tmp_path: Path) -> None:
    import hashlib
    import json

    from iOpenPod.app.host_media_folders import create_host_media_folder
    from iOpenPod.app.host_media_library import HostMediaScanner
    from storage import AtomicHostFile

    stream = io.BytesIO(base64.b64decode((FIXTURES / "tone.m4a.b64").read_bytes()))
    rewrite_rating_stream(stream, "track.m4a", 80)
    (tmp_path / "track.m4a").write_bytes(stream.getvalue())
    cache = AtomicHostFile(tmp_path / "cache.json")
    folder = create_host_media_folder(tmp_path)
    HostMediaScanner(cache).scan((folder,), checkpoint=lambda: None)
    document = json.loads(cache.path.read_bytes())
    document["version"] = 14
    # Simulate the genuine old reader's cached tag projection, with no RATING.
    document["entries"][0]["metadata"]["tag_values"] = [
        tag
        for tag in document["entries"][0]["metadata"]["tag_values"]
        if tag["name"] != "rating"
    ]
    document["catalog_sha256"] = hashlib.sha256(
        json.dumps(
            document["entries"],
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    cache.replace_bytes(json.dumps(document).encode())
    scanner = HostMediaScanner(cache)
    pending = scanner.scan((folder,), checkpoint=lambda: None)
    assert pending.cache.inspected == 1
    library = scanner.complete(pending, frozenset(), checkpoint=lambda: None)
    assert library.snapshot.tracks[0].rating == 80


@pytest.mark.parametrize("version", [3, 4])
def test_rating_edit_retains_id3_version(version: int) -> None:
    stream = io.BytesIO(base64.b64decode((FIXTURES / "tone.mp3.b64").read_bytes()))
    audio = _load_media(stream, "mp3")
    audio.save(stream, v2_version=version)
    rewrite_rating_stream(stream, "track.mp3", 80)
    stream.seek(0)
    assert _load_media(stream, "mp3").tags.version[:2] == (2, version)


def test_native_rating_is_rechecked_before_editing() -> None:
    stream = io.BytesIO(base64.b64decode((FIXTURES / "tone.m4a.b64").read_bytes()))
    rewrite_rating_stream(stream, "track.m4a", 60)
    original = stream.getvalue()
    with pytest.raises(ValueError, match="Host rating changed"):
        rewrite_rating_stream(stream, "track.m4a", 80, expected_rating=20)
    assert stream.getvalue() == original
