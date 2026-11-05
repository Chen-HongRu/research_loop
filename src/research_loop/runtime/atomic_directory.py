"""Exchange two complete directories without exposing an intermediate tree."""

import ctypes
import errno
import os
import sys


def exchange(left, right):
    library = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        operation = getattr(library, "renameatx_np", None)
        current_directory = -2
    elif sys.platform.startswith("linux"):
        operation = getattr(library, "renameat2", None)
        current_directory = -100
    else:
        operation = None
    if operation is None:
        raise OSError(errno.ENOTSUP, "Atomic directory exchange is unavailable")
    operation.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    operation.restype = ctypes.c_int
    # Both Darwin RENAME_SWAP and Linux RENAME_EXCHANGE use flag 2.
    if operation(
        current_directory, os.fsencode(left), current_directory, os.fsencode(right), 2
    ):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
