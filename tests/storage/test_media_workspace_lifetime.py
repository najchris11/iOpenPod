"""Crash cleanup preserves active workspaces, tools, and unrelated Host paths."""

import os
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import pytest

from storage import _media_workspaces as lifecycle
from storage.media_processing import media_workspace


def test_active_workspace_is_not_reclaimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    with media_workspace(checkpoint=lambda: None) as workspace:
        path = Path(workspace.output_path(".bin"))
        path.write_bytes(b"prepared")
        assert lifecycle.cleanup_media_workspaces(tmp_path) == ()
        assert path.read_bytes() == b"prepared"
    assert not tuple(tmp_path.glob("iopenpod-media-*"))


def test_cleanup_skips_links_foreign_modes_and_unrecognized_content(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "important").write_bytes(b"retain")
    with suppress(OSError):
        (tmp_path / "iopenpod-media-alias").symlink_to(
            outside, target_is_directory=True
        )
    unknown = tmp_path / "iopenpod-media-unknown"
    unknown.mkdir(mode=0o700)
    (unknown / "unrelated").write_bytes(b"retain")
    shared = tmp_path / "iopenpod-media-shared"
    shared.mkdir(mode=0o755)
    (shared / "source").write_bytes(b"retain")
    lifecycle.cleanup_media_workspaces(tmp_path)
    assert (outside / "important").read_bytes() == b"retain"
    assert unknown.exists() and shared.exists()


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="Pre-fix compatibility uses Linux process observations",
)
def test_same_boot_legacy_cleanup_requires_process_quiescence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "iopenpod-media-legacy"
    legacy.mkdir(mode=0o700)
    (legacy / "source").write_bytes(b"previous crashed run")
    monkeypatch.setattr(lifecycle, "_legacy_quiescent", lambda _: False)
    assert lifecycle.cleanup_media_workspaces(tmp_path) == ()
    monkeypatch.setattr(lifecycle, "_legacy_quiescent", lambda _: True)
    assert lifecycle.cleanup_media_workspaces(tmp_path) == (legacy,)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process snapshot format")
@pytest.mark.parametrize("evidence", ["python", "pyside", "path", "missing"])
def test_legacy_process_guard_handles_closed_outputs_and_ambiguous_processes(
    tmp_path: Path, evidence: str
) -> None:
    proc = tmp_path / "proc"
    entry = proc / "999999"
    entry.mkdir(parents=True)
    (entry / "fd").mkdir()
    (entry / "exe").symlink_to("/bin/tool")
    (entry / "cmdline").write_bytes(b"tool\0")
    (entry / "maps").write_text("libc.so")
    candidate = tmp_path / "iopenpod-media-old"
    if evidence == "python":
        (entry / "maps").write_text("libpython3.12.so")
    elif evidence == "pyside":
        (entry / "maps").write_text("PySide6/QtCore.so")
    elif evidence == "path":
        (entry / "cmdline").write_text(f"tool\0{candidate}/processed.bin")
    else:
        (entry / "maps").unlink()
    assert not lifecycle._legacy_quiescent((candidate,), proc=proc)


def test_unreaped_child_evidence_is_conservative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / ".child-abc"
    marker.write_text("starting")
    assert lifecycle._children_in_use(tmp_path)
    marker.write_text("12345")
    monkeypatch.setattr(lifecycle, "_process_alive", lambda _: True)
    assert lifecycle._children_in_use(tmp_path)
    monkeypatch.setattr(lifecycle, "_process_alive", lambda _: False)
    assert not lifecycle._children_in_use(tmp_path)


def test_windows_markers_belong_to_process_objects_even_with_reused_pids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Process:
        pid = 12345

        def wait(self) -> int:
            return 0

    processes = iter((Process(), Process()))
    directory = tmp_path / "iopenpod-media-windows"
    directory.mkdir()
    monkeypatch.setattr(lifecycle, "os", SimpleNamespace(name="nt", sep=os.sep))
    monkeypatch.setattr(lifecycle, "_CHILDREN", {})

    def launch(args: list[str], **kwargs: object) -> Process:
        assert args[-1].startswith(str(directory))
        assert "pass_fds" not in kwargs
        assert (
            sum(
                marker.read_text() == "starting"
                for marker in directory.glob(".child-*")
            )
            == 1
        )
        return next(processes)

    monkeypatch.setattr(subprocess, "Popen", launch)
    with (directory / ".iopenpod-owner.lock").open("w+b") as lease:
        monkeypatch.setattr(lifecycle, "_LIVE", {directory: lease})
        first = lifecycle.spawn_media_process(
            ["tool", str(directory / "processed.bin")]
        )
        first_markers = set(directory.glob(".child-*"))
        second = lifecycle.spawn_media_process(
            ["tool", str(directory / "processed.bin")]
        )
        all_markers = set(directory.glob(".child-*"))
        assert len(first_markers) == 1 and len(all_markers) == 2
        assert all(marker.read_text() == "12345" for marker in all_markers)
        lifecycle.reap_media_process(first)
        assert set(directory.glob(".child-*")) == all_markers - first_markers
        lifecycle.reap_media_process(second)
        assert not tuple(directory.glob(".child-*"))
        assert not lifecycle._CHILDREN


def _wait_file(path: Path) -> None:
    deadline = time.monotonic() + 10
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"Child did not report readiness: {path}")
        time.sleep(0.02)


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX inherited lease after parent SIGKILL"
)
@pytest.mark.parametrize("kind", ["encoder", "probe"])
def test_orphaned_tool_keeps_workspace_leased_until_exit(
    tmp_path: Path, kind: str
) -> None:
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    ready, finish, directory_file = (
        tmp_path / name for name in ("ready", "finish", "directory")
    )
    tool = tmp_path / "tool"
    tool.write_text(
        f"#!{sys.executable}\n"
        "import os, time\nfrom pathlib import Path\n"
        "Path(os.environ['WORKSPACE_READY']).write_text(str(os.getpid()))\n"
        "while not Path(os.environ['WORKSPACE_FINISH']).exists(): time.sleep(.02)\n"
    )
    tool.chmod(0o700)
    script = """
import sys
from pathlib import Path
from storage import HostPath
from storage.media_processing import media_workspace, run_media_tool
from storage.media_probe import probe
with media_workspace(checkpoint=lambda: None) as workspace:
    path = workspace.output_path('.bin')
    Path(path).write_bytes(b'prepared')
    Path(sys.argv[2]).write_text(str(Path(path).parent))
    if sys.argv[3] == 'encoder':
        run_media_tool(HostPath(sys.argv[1]), (str(path),), checkpoint=lambda: None)
    else:
        probe(HostPath(sys.argv[1]), path, lambda: None, timeout_seconds=60, max_output_bytes=4096)
"""
    environment = {
        **os.environ,
        "TMPDIR": str(temporary),
        "WORKSPACE_READY": str(ready),
        "WORKSPACE_FINISH": str(finish),
    }
    parent = subprocess.Popen(
        [sys.executable, "-c", script, str(tool), str(directory_file), kind],
        env=environment,
    )
    try:
        _wait_file(ready)
        workspace = Path(directory_file.read_text())
        parent.kill()
        parent.wait(timeout=10)
        assert lifecycle.cleanup_media_workspaces(temporary) == ()
        assert workspace.exists()
        finish.touch()
        deadline = time.monotonic() + 10
        while workspace.exists() and time.monotonic() < deadline:
            lifecycle.cleanup_media_workspaces(temporary)
            time.sleep(0.02)
        assert not workspace.exists()
    finally:
        finish.touch()
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=10)
