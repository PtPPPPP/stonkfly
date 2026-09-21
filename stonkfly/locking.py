"""Single-worker directory lock.

Kept out of the CLI so any entry point that must not race a running worker --
the run loop and the ledger promotion -- can take the same lock on the same
file.
"""

import os


def acquire(path):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        handle = kernel32.CreateFileW(
            str(path),
            0x80000000 | 0x40000000,  # GENERIC_READ | GENERIC_WRITE
            0,  # No sharing: only one worker may own this file.
            None,
            4,  # OPEN_ALWAYS
            0x80,  # FILE_ATTRIBUTE_NORMAL
            None,
        )
        if handle == wintypes.HANDLE(-1).value:
            error = ctypes.get_last_error()
            if error in (32, 33):  # ERROR_SHARING_VIOLATION/LOCK_VIOLATION
                raise BlockingIOError(error, "Worker lock is already held")
            raise ctypes.WinError(error)
        return handle

    import fcntl

    lock = path.open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        lock.close()
        raise
    return lock


def release(lock):
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        if not kernel32.CloseHandle(lock):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        lock.close()
