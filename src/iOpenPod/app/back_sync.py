"""Post-commit rating updates to the Host files paired by the reviewed Sync."""

from __future__ import annotations

from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

import mutagen

from iOpenPod.app.display_text import source_text
from iOpenPod.app.library_sync_helper import SyncedTrack
from iOpenPod.app.media.ratings import rating_supported, rewrite_rating_stream
from iPodDB.library import IssueSeverity, WriteIssue
from storage import StorageError
from storage.host_input import LocalHostFile
from storage.host_replacement import replace_host_file

if TYPE_CHECKING:
    from collections.abc import Callable

    from iOpenPod.app.models.device import ActiveIPod
    from iOpenPod.app.sync_execution import SyncExecutionRequest
    from iOpenPod.app.sync_plan import SyncPlanItem


def rating_needs_back_sync(
    host: int, ipod: int, baseline_host: int | None, baseline_ipod: int | None
) -> bool:
    """Unknown baselines and zero iPod ratings never authorize Host changes."""
    return (
        baseline_host is not None
        and baseline_ipod is not None
        and host == baseline_host
        and ipod != baseline_ipod
        and ipod != host
        and ipod in (20, 40, 60, 80, 100)
    )


def back_sync_ratings(
    request: SyncExecutionRequest,
    active: ActiveIPod,
    completed: tuple[SyncPlanItem, ...],
    provenance: tuple[SyncedTrack, ...],
    *,
    validate_device: Callable[[], None] = lambda: None,
) -> tuple[tuple[SyncedTrack, ...], tuple[WriteIssue, ...]]:
    """Write ratings only for unchanged pairs and successfully completed items.

    The caller has already verified device publication. Failures retain previous
    rating baselines, so another reviewed Sync can retry without undoing the iPod.
    """
    from iOpenPod.app.sync_plan import (
        SyncPlanAction,
        SyncPlanMediaKind,
        host_path_identity,
    )

    hosts = {
        host_path_identity(t.metadata.location): t for t in request.host.snapshot.tracks
    }
    sources = {host_path_identity(str(s.path)): s for s in request.host.sources}
    scanned = {t.track_id: t for t in request.ipod.tracks}
    tracks = {t.track_id: t for t in active.library.tracks}
    paths = {t.metadata.location: t for t in active.library.tracks}
    updated = {(t.track_id, str(t.path)): t for t in provenance}
    linked = {
        host_path_identity(t.sync.host_path_hint): t
        for t in provenance
        if t.sync is not None
    }
    issues: list[WriteIssue] = []
    for item in request.plan.items:
        if (
            item.media_kind is not SyncPlanMediaKind.TRACK
            or item.host_path is None
            or not (item.action is SyncPlanAction.UNCHANGED or item in completed)
            or item.action in (SyncPlanAction.REMOVE, SyncPlanAction.ATTENTION)
        ):
            continue
        key = host_path_identity(item.host_path)
        new = linked.get(key)
        old = scanned.get(item.ipod_id) if item.ipod_id is not None else None
        prior = old.sync if old is not None else None
        if prior is not None and host_path_identity(prior.host_path_hint) != key:
            prior = None
        record = new or (
            SyncedTrack(
                old.path, old.acoustic_fingerprint, prior, track_id=old.track_id
            )
            if old is not None
            else None
        )
        if record is None or record.sync is None:
            continue
        track = (
            tracks.get(item.ipod_id)
            if item.ipod_id is not None
            else paths.get(str(record.path))
        )
        host = hosts.get(key)
        source = sources.get(key)
        if track is None or host is None or source is None:
            continue
        sync = replace(
            record.sync,
            host_rating=prior.host_rating if prior is not None else None,
            ipod_rating=prior.ipod_rating if prior is not None else None,
        )
        record = replace(record, sync=sync)
        record_key = (record.track_id, str(record.path))
        updated[record_key] = record
        if host.rating == sync.host_rating and track.rating == sync.ipod_rating:
            # Established unchanged ratings need no Host I/O or per-Track device
            # probes. The enclosing Sync Session validates the Library identity.
            continue
        validate_device()
        needs_write = rating_needs_back_sync(
            host.rating, track.rating, sync.host_rating, sync.ipod_rating
        )
        try:
            observation = LocalHostFile.observe(source.path)
            if (observation.size_bytes, observation.modified_ns) != (
                source.size_bytes,
                source.modified_ns,
            ):
                raise ValueError(
                    "The paired Host file changed after scanning; rescan before retrying."
                )
            rating = host.rating
            if needs_write:
                if not rating_supported(item.host_path):
                    raise ValueError(
                        "Rating Back Sync supports MP3 and MP4 media only."
                    )
                observation = replace_host_file(
                    observation,
                    partial(
                        rewrite_rating_stream,
                        file_name=item.host_path,
                        rating=track.rating,
                        expected_rating=host.rating,
                    ),
                    checkpoint=validate_device,
                )
                issues.append(
                    WriteIssue(
                        "sync.rating_written",
                        source_text(
                            "Updated Host rating from {old} to {new} stars.",
                            old=str(rating // 20),
                            new=str(track.rating // 20),
                        ),
                        severity=IssueSeverity.INFO,
                        artifact=item.host_path,
                    )
                )
                rating = track.rating
            elif (
                sync.host_rating is not None
                and sync.ipod_rating is not None
                and rating != sync.host_rating
                and track.rating != sync.ipod_rating
                and rating != track.rating
            ):
                issues.append(
                    WriteIssue(
                        "sync.rating_conflict",
                        "Both Host and iPod ratings changed. The Host rating was kept.",
                        severity=IssueSeverity.WARNING,
                        artifact=item.host_path,
                    )
                )
            updated[record_key] = replace(
                record,
                sync=replace(
                    sync,
                    host_rating=rating,
                    ipod_rating=track.rating,
                    host_size_bytes=observation.size_bytes,
                    host_modified_ns=observation.modified_ns,
                ),
            )
        except (OSError, ValueError, StorageError, mutagen.MutagenError) as error:
            validate_device()
            issues.append(
                WriteIssue(
                    "sync.rating_back_sync_failed",
                    "The iPod Sync completed, but its Host rating update could not finish. Rescan and retry Sync."
                    if needs_write
                    else "The paired Host file was unavailable; its rating baseline was kept. Rescan before the next Sync.",
                    detail=str(error),
                    artifact=item.host_path,
                    severity=IssueSeverity.ERROR
                    if needs_write
                    else IssueSeverity.WARNING,
                )
            )
    return tuple(updated.values()), tuple(issues)
