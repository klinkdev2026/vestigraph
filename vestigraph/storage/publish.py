"""Atomic no-replace publication, including local filesystems without hard links."""
import ctypes
import errno
import os
import sys


def publish_new(source, target):
    try:
        os.link(source, target)
        return
    except FileExistsError:
        raise
    except OSError as exc:
        # Windows rename is atomic and refuses an existing destination on NTFS/FAT/exFAT.
        if os.name == "nt":
            os.rename(source, target)
            return
        if exc.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV):
            raise
    libc = ctypes.CDLL(None, use_errno=True)
    src, dst = os.fsencode(source), os.fsencode(target)
    if sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        call = libc.renameat2
        call.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        call.restype = ctypes.c_int
        result = call(-100, src, -100, dst, 1)  # AT_FDCWD, RENAME_NOREPLACE
    elif sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        call = libc.renamex_np
        call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        call.restype = ctypes.c_int
        result = call(src, dst, 4)  # RENAME_EXCL
    else:
        raise OSError(errno.ENOTSUP, "Filesystem cannot atomically publish without replacing files")
    if result:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), os.fspath(target))
