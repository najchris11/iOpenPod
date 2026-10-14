"""Incremental preparations remain private and use ordinary transaction recovery."""

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from tests.storage.test_recovery import _session

from storage import (
    DevicePath,
    FileContent,
    FilePreconditionError,
    HostPath,
    RecoverableWriteError,
    StorageTransaction,
    TransactionProgress,
    TransactionState,
    TransactionWrite,
)
from storage._filesystem import allocated_size


def _content(data: bytes) -> FileContent:
    return FileContent(len(data), hashlib.sha256(data).hexdigest())


def test_incremental_staging_preserves_targets_and_adopts_one_journal(
    tmp_path: Path,
) -> None:
    _, _, session = _session(tmp_path)
    with session, session.transaction_staging() as staging:
        original = session.atomic_write(DevicePath("database"), b"original").fingerprint
        source = tmp_path / "encoded"
        source.write_bytes(b"encoded")
        staged = staging.stage(
            DevicePath("media"), HostPath(source), _content(b"encoded")
        )
        source.unlink()
        assert not session.exists(DevicePath("media"))
        assert session.read(DevicePath("database")) == b"original"
        plan = staging.bind(
            StorageTransaction(
                (
                    TransactionWrite(DevicePath("media"), staged, _content(b"encoded")),
                    TransactionWrite(
                        DevicePath("database"),
                        b"updated",
                        _content(b"updated"),
                        original,
                    ),
                )
            )
        )
        result = session.execute_transaction(plan)
        assert result.recovery.journal_path == staging.journal_path
        assert session.read(DevicePath("media")) == b"encoded"
        session.restore_transaction(session.inspect_transaction(staging.journal_path))
        assert not session.exists(DevicePath("media"))
        assert session.read(DevicePath("database")) == b"original"
        session.finalize_restored_transaction(staging.journal_path)


def test_stage_versions_survive_a_failed_preparation_attempt(tmp_path: Path) -> None:
    _, _, session = _session(tmp_path)
    with session, session.transaction_staging() as staging:
        source = tmp_path / "encoded"
        source.write_bytes(b"original output")
        first = staging.stage(
            DevicePath("media"), HostPath(source), _content(source.read_bytes())
        )
        source.write_bytes(b"rewritten output")
        retry = staging.stage(
            DevicePath("media"), HostPath(source), _content(source.read_bytes())
        )
        staging.release(retry)
        result = session.execute_transaction(
            staging.bind(
                StorageTransaction(
                    (
                        TransactionWrite(
                            DevicePath("media"), first, _content(b"original output")
                        ),
                    )
                )
            )
        )
        assert session.read(DevicePath("media")) == b"original output"
        session.finalize_committed_transaction(result.recovery.journal_path)


def test_staging_context_discards_preparations_without_changing_existing_media(
    tmp_path: Path,
) -> None:
    root, _, session = _session(tmp_path)
    with session:
        prior = session.atomic_write(DevicePath("media"), b"old").fingerprint
        source = tmp_path / "rewrite"
        source.write_bytes(b"new")
        with session.transaction_staging() as staging:
            staging.stage(
                DevicePath("media"), HostPath(source), _content(b"new"), expected=prior
            )
            assert session.read(DevicePath("media")) == b"old"
        assert session.read(DevicePath("media")) == b"old"
        assert not (root / str(staging.journal_path.parent)).exists()


def test_tampered_stage_cannot_publish_any_target(tmp_path: Path) -> None:
    root, _, session = _session(tmp_path)
    with session, session.transaction_staging() as staging:
        source = tmp_path / "encoded"
        source.write_bytes(b"encoded")
        staged = staging.stage(
            DevicePath("media"), HostPath(source), _content(b"encoded")
        )
        (root / str(staged.path)).write_bytes(b"changed")
        with pytest.raises(FilePreconditionError):
            session.execute_transaction(
                staging.bind(
                    StorageTransaction(
                        (
                            TransactionWrite(
                                DevicePath("media"), staged, _content(b"encoded")
                            ),
                        )
                    )
                )
            )
        assert not session.exists(DevicePath("media"))


def test_forged_stage_reference_is_rejected(tmp_path: Path) -> None:
    _, _, session = _session(tmp_path)
    with session, session.transaction_staging() as staging:
        source = tmp_path / "encoded"
        source.write_bytes(b"encoded")
        staged = staging.stage(
            DevicePath("media"), HostPath(source), _content(b"encoded")
        )
        with pytest.raises(FilePreconditionError):
            session.validate_transaction(
                staging.bind(
                    StorageTransaction(
                        (
                            TransactionWrite(
                                DevicePath("media"),
                                replace(staged),
                                _content(b"encoded"),
                            ),
                        )
                    )
                )
            )


def test_changed_preparation_journal_blocks_adoption_and_preserves_evidence(
    tmp_path: Path,
) -> None:
    root, _, session = _session(tmp_path)
    source = tmp_path / "encoded"
    source.write_bytes(b"encoded")
    with session:
        staging = session.transaction_staging()
        staged = staging.stage(
            DevicePath("media"), HostPath(source), _content(b"encoded")
        )
        journal = root / str(staging.journal_path)
        journal.write_bytes(journal.read_bytes() + b"\n")
        with pytest.raises(FilePreconditionError, match="journal changed"):
            session.validate_transaction(
                staging.bind(
                    StorageTransaction(
                        (
                            TransactionWrite(
                                DevicePath("media"), staged, _content(b"encoded")
                            ),
                        )
                    )
                )
            )
        with pytest.raises(RecoverableWriteError):
            staging.close()
        assert journal.exists()
        assert not session.exists(DevicePath("media"))


def test_foreign_connection_cannot_adopt_staged_content(tmp_path: Path) -> None:
    _, _, session = _session(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    _, _, foreign = _session(other)
    source = tmp_path / "encoded"
    source.write_bytes(b"encoded")
    with session, foreign, session.transaction_staging() as staging:
        staged = staging.stage(
            DevicePath("media"), HostPath(source), _content(b"encoded")
        )
        with pytest.raises(FilePreconditionError, match="connection"):
            foreign.validate_transaction(
                staging.bind(
                    StorageTransaction(
                        (
                            TransactionWrite(
                                DevicePath("media"), staged, _content(b"encoded")
                            ),
                        )
                    )
                )
            )
        assert not foreign.exists(DevicePath("media"))


def test_interruption_during_adoption_remains_recoverable(tmp_path: Path) -> None:
    _, _, session = _session(tmp_path)
    source = tmp_path / "encoded"
    source.write_bytes(b"encoded")
    with session, session.transaction_staging() as staging:
        staged = staging.stage(
            DevicePath("media"), HostPath(source), _content(b"encoded")
        )
        plan = staging.bind(
            StorageTransaction(
                (TransactionWrite(DevicePath("media"), staged, _content(b"encoded")),)
            )
        )

        def fail(event: TransactionProgress) -> None:
            if event.state is TransactionState.STAGING:
                raise RuntimeError("interrupted adoption")

        with pytest.raises(RecoverableWriteError) as failure:
            session.execute_transaction(plan, progress=fail)
        assert not failure.value.publication_started
        assert not session.exists(DevicePath("media"))
        session.restore_transaction(session.inspect_transaction(staging.journal_path))
        session.finalize_restored_transaction(staging.journal_path)


def test_adopted_capacity_does_not_count_allocated_media_twice(tmp_path: Path) -> None:
    _, _, session = _session(tmp_path)
    data = b"x" * (128 * 1024)
    source = tmp_path / "encoded"
    source.write_bytes(data)
    with session, session.transaction_staging() as staging:
        regular = StorageTransaction(
            (TransactionWrite(DevicePath("media"), HostPath(source), _content(data)),)
        )
        regular_size = session.validate_transaction(regular).required_bytes
        staged = staging.stage(DevicePath("media"), HostPath(source), _content(data))
        adopted = staging.bind(
            StorageTransaction(
                (TransactionWrite(DevicePath("media"), staged, _content(data)),)
            )
        )
        adopted_size = session.validate_transaction(adopted).required_bytes
        unit = session.mounted_volume.volume.capabilities.allocation_unit_size
        assert regular_size - adopted_size == allocated_size(len(data), unit)


def test_existing_file_rewrite_restores_after_adopted_publication_failure(
    tmp_path: Path,
) -> None:
    _, _, session = _session(tmp_path)
    source = tmp_path / "encoded"
    source.write_bytes(b"new media")
    with session, session.transaction_staging() as staging:
        before = session.atomic_write(DevicePath("media"), b"old media").fingerprint
        staged = staging.stage(
            DevicePath("media"),
            HostPath(source),
            _content(b"new media"),
            expected=before,
        )
        plan = staging.bind(
            StorageTransaction(
                (
                    TransactionWrite(
                        DevicePath("media"), staged, _content(b"new media"), before
                    ),
                )
            )
        )

        def fail(event: TransactionProgress) -> None:
            if event.state is TransactionState.PUBLISHING and event.completed:
                raise RuntimeError("interrupted publication")

        with pytest.raises(RecoverableWriteError) as failure:
            session.execute_transaction(plan, progress=fail)
        assert failure.value.publication_started
        session.restore_transaction(session.inspect_transaction(staging.journal_path))
        assert session.read(DevicePath("media")) == b"old media"
        session.finalize_restored_transaction(staging.journal_path)


def test_crashed_unadopted_staging_uses_existing_restart_recovery(
    tmp_path: Path,
) -> None:
    _, _, session = _session(tmp_path)
    source = tmp_path / "encoded"
    source.write_bytes(b"encoded")
    with session:
        staging = session.transaction_staging()
        staging.stage(DevicePath("media"), HostPath(source), _content(b"encoded"))
        # Simulate losing the application handle without its normal context exit.
        recovery = session.inspect_transaction(staging.journal_path)
        assert recovery.state is TransactionState.STAGING
        session.restore_transaction(recovery)
        session.finalize_restored_transaction(staging.journal_path)
        assert not session.exists(DevicePath("media"))


def test_disconnected_handle_cannot_stage_or_discard_its_journal(
    tmp_path: Path,
) -> None:
    root, platform, session = _session(tmp_path)
    source = tmp_path / "encoded"
    source.write_bytes(b"encoded")
    with session:
        staging = session.transaction_staging()
        staging.stage(DevicePath("media"), HostPath(source), _content(b"encoded"))
        platform.disconnect(root)
        with pytest.raises(RecoverableWriteError):
            staging.close()
        assert (root / str(staging.journal_path)).exists()
