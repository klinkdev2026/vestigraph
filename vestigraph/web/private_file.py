"""Publish local control credentials with private permissions from creation."""
import os
import uuid
from pathlib import Path


def _open_owner_only_windows(path):
    import ctypes as c
    from ctypes import wintypes as w
    import msvcrt

    kernel = c.WinDLL("kernel32", use_last_error=True)
    advapi = c.WinDLL("advapi32", use_last_error=True)

    def api(dll, name, result, *args):
        fn = getattr(dll, name)
        fn.restype, fn.argtypes = result, args
        return fn

    close = api(kernel, "CloseHandle", w.BOOL, w.HANDLE)
    free = api(kernel, "LocalFree", c.c_void_p, c.c_void_p)
    process = api(kernel, "GetCurrentProcess", w.HANDLE)
    open_token = api(advapi, "OpenProcessToken", w.BOOL, w.HANDLE, w.DWORD, c.POINTER(w.HANDLE))
    token_info = api(advapi, "GetTokenInformation", w.BOOL, w.HANDLE, c.c_int,
                     c.c_void_p, w.DWORD, c.POINTER(w.DWORD))
    sid_text = api(advapi, "ConvertSidToStringSidW", w.BOOL, c.c_void_p, c.POINTER(w.LPWSTR))
    descriptor = api(advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW", w.BOOL,
                     w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.POINTER(w.DWORD))

    class Attributes(c.Structure):
        _fields_ = [("length", w.DWORD), ("descriptor", c.c_void_p), ("inherit", w.BOOL)]

    create = api(kernel, "CreateFileW", w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD,
                 c.POINTER(Attributes), w.DWORD, w.DWORD, w.HANDLE)
    security = api(advapi, "GetSecurityInfo", w.DWORD, w.HANDLE, c.c_int, w.DWORD,
                   c.c_void_p, c.c_void_p, c.c_void_p, c.c_void_p, c.POINTER(c.c_void_p))
    describe = api(advapi, "ConvertSecurityDescriptorToStringSecurityDescriptorW", w.BOOL,
                   c.c_void_p, w.DWORD, w.DWORD, c.POINTER(w.LPWSTR), c.c_void_p)
    token, sd, sid = w.HANDLE(), c.c_void_p(), w.LPWSTR()
    try:
        if not open_token(process(), 8, c.byref(token)):  # TOKEN_QUERY
            raise c.WinError(c.get_last_error())
        size = w.DWORD()
        token_info(token, 1, None, 0, c.byref(size))  # TokenUser
        if not size.value:
            raise c.WinError(c.get_last_error())
        buffer = c.create_string_buffer(size.value)
        if not token_info(token, 1, buffer, size, c.byref(size)):
            raise c.WinError(c.get_last_error())
        if not sid_text(c.cast(buffer, c.POINTER(c.c_void_p))[0], c.byref(sid)):
            raise c.WinError(c.get_last_error())
        sddl = f"D:P(A;;FA;;;{sid.value})"  # protected DACL, current token user only
        if not descriptor(sddl, 1, c.byref(sd), None):
            raise c.WinError(c.get_last_error())
        attributes = Attributes(c.sizeof(Attributes), sd, False)
        handle = create(str(path), 0x40000000 | 0x20000, 0, c.byref(attributes), 1, 0x80, None)
        if handle == c.c_void_p(-1).value:  # CREATE_NEW, no sharing; never follow an existing file
            raise c.WinError(c.get_last_error())
        try:
            actual, actual_text = c.c_void_p(), w.LPWSTR()
            try:
                error = security(handle, 1, 4, None, None, None, None, c.byref(actual))
                if error:
                    raise c.WinError(error)
                if not describe(actual, 1, 4, c.byref(actual_text), None):
                    raise c.WinError(c.get_last_error())
                if actual_text.value != sddl:
                    raise PermissionError("Filesystem did not preserve the private control DACL")
            finally:
                if actual_text:
                    free(actual_text)
                if actual:
                    free(actual)
            fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
        except BaseException:
            close(handle)
            raise
        return fd
    finally:
        if sid:
            free(sid)
        if sd:
            free(sd)
        if token:
            close(token)


def write_owner_only(path: Path, text: str) -> bool:
    """Write through a new private inode; an old reader never receives the new secret."""
    path = Path(path)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = (_open_owner_only_windows(temporary) if os.name == "nt" else
              os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        with os.fdopen(fd, "wb") as stream:
            stream.write(text.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        return True
    except OSError:
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
