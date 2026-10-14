"""Generic cover generation excludes F1068 without silently dropping old records."""

from dataclasses import replace

import pytest
from tests.iPodDB.library.test_artwork_format_repair import (
    prepare_cover,
    retained_source,
)
from tests.iPodDB.library.test_write_artwork import BLUE, COVER, TARGET
from tests.iPodDB.library.test_writing import library

from device_registry import DEFAULT_DEVICE_REGISTRY
from iOpenPod.app.artwork_policy import application_cover_formats
from iPodDB.ArtworkDB.parser.parse_ArtworkDB import parse_ArtworkDB
from iPodDB.ArtworkDB.shared.chunk_defs.mhif import MhifHeader
from iPodDB.ArtworkDB.shared.chunk_defs.mhii import MhiiHeader
from iPodDB.ArtworkDB.shared.chunk_defs.mhod_payloads.container_mhod import (
    MhodContainerPayload,
)
from iPodDB.library import WriteResources


@pytest.mark.parametrize(
    "family,generation",
    [
        ("iPod Classic", "6th Gen"),
        ("iPod Classic", "6.5th Gen"),
        ("iPod Classic", "7th Gen"),
        ("iPod Nano", "3rd Gen"),
        ("iPod Nano", "4th Gen"),
    ],
)
def test_fresh_audio_cover_never_emits_f1068(family: str, generation: str) -> None:
    profile = next(
        p
        for p in DEFAULT_DEVICE_REGISTRY.profiles
        if p.family == family and p.generation == generation
    )
    source = library()
    target = replace(TARGET, cover_formats=application_cover_formats(profile))
    result = prepare_cover(
        source, target, WriteResources(artwork=(BLUE,), file_inventory=())
    )
    assert result.prepared is not None, result.issues
    assert result.prepared.artwork is not None
    document = parse_ArtworkDB(result.prepared.artwork)
    assert 1068 not in {
        s.chunk.header.format_id for s in document.find_chunks(MhifHeader)
    }
    assert all(
        child.payload.child.header.format_id != 1068
        for row in document.find_chunks(MhiiHeader)
        for child in row.chunk.children
        if isinstance(child.payload, MhodContainerPayload)
    )
    assert all("F1068_" not in f.relative_path for f in result.prepared.artwork_files)


def test_retained_f1068_remains_lossless_and_refresh_refuses_stale_poster() -> None:
    source, target, resources = retained_source((COVER, replace(COVER, format_id=1068)))
    target = replace(target, cover_formats=(COVER,))
    original = source.serialize()
    unchanged = source.prepare(
        source.analyze(source.begin_draft(source.snapshot), target)
    )
    assert unchanged.prepared is not None, unchanged.issues
    assert unchanged.prepared.artwork == original.artwork
    assert not unchanged.prepared.artwork_files
    refreshed = prepare_cover(source, target, resources)
    assert refreshed.prepared is None
    assert any(
        issue.code == "preparation.cannot_encode"
        and "unsupported representation stale" in issue.message
        for issue in refreshed.issues
    )
    assert source.serialize() == original
