"""Incremental private content for a later complete, recoverable transaction."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Self

from storage._filesystem import (
    allocated_size,
    flush_parent_directory,
    resolve_device_path,
)
from storage._transaction_journal import journal_root, validate_paths
from storage._transactions import _journal, _journal_allocation, _persist, _read
from storage.errors import FilePreconditionError, RecoverableWriteError
from storage.paths import DevicePath
from storage.transactions import (
    FileContent,
    StagedTransactionFile,
    StorageTransaction,
    TransactionState,
    TransactionWrite,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

    from storage.models import FileFingerprint
    from storage.paths import HostPath
    from storage.session import FilesystemSession


class TransactionStaging:
    """Stage versions without changing targets; retain existing recovery format."""

    def __init__(self, session: FilesystemSession) -> None:
        self._session = session
        self._identity = uuid.uuid4().hex
        self.journal_path = DevicePath(
            f".iopenpod-recovery/{self._identity}/transaction.json"
        )
        self._fingerprint: FileFingerprint | None = None
        self._writes: dict[DevicePath, TransactionWrite] = {}
        self._files: dict[DevicePath, StagedTransactionFile] = {}
        self._consumed = False
        self._closed = False
        self._started = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def check(self, session: FilesystemSession) -> None:
        self._session._revalidate(write=True)
        session._revalidate()
        if (
            self._closed
            or self._consumed
            or self._session.mounted_volume != session.mounted_volume
        ):
            raise FilePreconditionError(
                "Transaction staging belongs to another or expired connection"
            )
        if self._fingerprint is not None:
            journal, fingerprint = _read(session, self.journal_path)
            if (
                fingerprint != self._fingerprint
                or journal.state is not TransactionState.STAGING
            ):
                raise FilePreconditionError("The private preparation journal changed")

    def allows(self, session: FilesystemSession, path: DevicePath) -> bool:
        if (
            path != self.journal_path
            or self._fingerprint is None
            or self._consumed
            or self._closed
        ):
            return False
        self.check(session)
        return True

    def stage(
        self,
        target: DevicePath,
        source: HostPath,
        content: FileContent,
        *,
        expected: FileFingerprint | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> StagedTransactionFile:
        session = self._session
        with session._writer_lease():
            self.check(session)
            previous = self._writes.get(target)
            if previous is not None and previous.expected != expected:
                raise FilePreconditionError(
                    "A preparation changed its original precondition"
                )
            write = TransactionWrite(target, source, content, expected)
            validate_paths(
                tuple(dict.fromkeys((*self._writes, target))),
                case_sensitive=session.mounted_volume.volume.capabilities.case_sensitive
                is True,
            )
            # Validate the new target and reserve prior originals without rereading
            # an ever-growing media set on every append.
            unit = session.mounted_volume.volume.capabilities.allocation_unit_size
            reserve = sum(
                allocated_size(w.expected.size, unit)
                for w in self._writes.values()
                if w.expected is not None and w.path != target
            )
            session.validate_transaction(
                StorageTransaction((write,), reserve_bytes=reserve)
            )
            self._writes[target] = write
            intent = StorageTransaction(
                tuple(self._writes.values()), journal_identity=self._identity
            )
            journal = _journal(session, intent)
            self._started = True
            self._fingerprint = _persist(
                session, self.journal_path, journal, self._fingerprint
            )
            path = journal_root(self.journal_path).joinpath(
                f"incoming-{uuid.uuid4().hex}.bin"
            )
            copied = session._copy_transaction_file(
                source,
                path,
                content,
                reserve_bytes=reserve + 2 * _journal_allocation(journal, unit),
                checkpoint=checkpoint,
            )
            staged = StagedTransactionFile(
                self, path, target, copied.fingerprint, expected
            )
            self._files[path] = staged
            self._writes[target] = replace(write, source=staged)
            return staged

    def verify(self, session: FilesystemSession, write: TransactionWrite) -> None:
        source = write.source
        if not isinstance(source, StagedTransactionFile):
            return
        if (
            source.owner is not self
            or self._files.get(source.path) is not source
            or source.target != write.path
            or source.expected != write.expected
            or FileContent.from_fingerprint(source.fingerprint) != write.content
        ):
            raise FilePreconditionError(
                "The staged source is not an owned matching version"
            )
        if session.fingerprint(source.path) != source.fingerprint:
            raise FilePreconditionError("A private media stage changed")

    def bind(self, plan: StorageTransaction) -> StorageTransaction:
        if self._fingerprint is None:
            return plan
        if plan.recovery_material is not None or (
            plan.journal_identity is not None
            and plan.journal_identity != self._identity
        ):
            raise FilePreconditionError("Preparation cannot adopt another transaction")
        return replace(plan, journal_identity=self._identity, staging=self)

    def claim(self, session: FilesystemSession) -> FileFingerprint:
        self.check(session)
        assert self._fingerprint is not None
        self._consumed = True
        return self._fingerprint

    def release(self, source: StagedTransactionFile) -> None:
        if self._consumed or self._closed:
            return
        session = self._session
        with session._writer_lease():
            self.check(session)
            if self._files.get(source.path) is not source:
                raise FilePreconditionError("This stage version is not owned")
            if session.fingerprint(source.path) != source.fingerprint:
                raise FilePreconditionError("A released stage changed")
            observation = session._revalidate(write=True)
            path = resolve_device_path(
                session._root,
                source.path,
                observation.volume.capabilities,
                require_leaf=True,
            )
            path.unlink()
            flush_parent_directory(path)
            del self._files[source.path]

    def close(self) -> None:
        if self._closed or self._consumed:
            return
        try:
            if self._started:
                self.check(self._session)
                if self._fingerprint is None:
                    if self._session.exists(self.journal_path):
                        raise FilePreconditionError(
                            "The preparation journal could not be verified"
                        )
                    if self._session.exists(journal_root(self.journal_path)):
                        self._session._remove_empty_transaction_namespace(
                            journal_root(self.journal_path)
                        )
                    return
                # This live, unchanged STAGING handle has never published a target.
                # Abandon only its private namespace, even if an external writer
                # changed a target since preparation. Never restore over that edit.
                with self._session._writer_lease():
                    self.check(self._session)
                    self._session._remove_transaction_namespace(
                        journal_root(self.journal_path),
                        self.journal_path,
                        expected_journal=self._fingerprint,
                    )
                self._session.flush()
        except Exception as error:
            raise RecoverableWriteError(
                f"Private preparations could not be cleaned: {error}",
                str(self.journal_path),
                False,
            ) from error
        finally:
            self._closed = True
