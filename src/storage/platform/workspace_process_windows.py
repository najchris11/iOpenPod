"""Conservative liveness query for unreaped media-tool evidence on Windows."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any


def process_alive(pid: int) -> bool:
    factory = cast("Callable[..., Any]", vars(ctypes)["WinDLL"])
    last_error = cast("Callable[[], int]", vars(ctypes)["get_last_error"])
    kernel = factory("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return last_error() != 87
    try:
        code = wintypes.DWORD()
        return (
            not kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            or code.value == 259
        )
    finally:
        kernel.CloseHandle(handle)
