"""Where the admin's managed policy lives, and proof that a standard user cannot edit it.

The path comes from the OS (Windows' known-folder API, fixed POSIX paths), never from
environment variables: platformdirs honours WIN_PD_OVERRIDE_COMMON_APPDATA and
XDG_CONFIG_DIRS, which let any user point the "admin" policy at their own file.
"""

from __future__ import annotations

import sys
from pathlib import Path

from redactit.policy import PolicyError


def managed_policy_path() -> Path:
    if sys.platform == "win32":
        return _program_data() / "Redactit" / "policy.yaml"
    if sys.platform == "darwin":
        return Path("/Library/Application Support/Redactit/policy.yaml")
    return Path("/etc/redactit/policy.yaml")


def assert_admin_owned(path: Path) -> None:
    """Refuse a managed policy that the current (non-admin) user owns or could rewrite."""
    for p in (path, path.parent):
        if sys.platform == "win32":
            # A standard user can create folders under ProgramData and then owns them; an
            # installer running as admin leaves them owned by Administrators or SYSTEM.
            if _windows_owned_by_current_user(p):
                raise PolicyError(f"{p} is owned by the current user, so it cannot act as the admin policy")
        else:
            st = p.stat()
            if st.st_uid != 0 or st.st_mode & 0o022:
                raise PolicyError(f"{p} must be owned by root and not group- or world-writable")


def _program_data() -> Path:
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD), ("d", ctypes.c_ubyte * 8)]

    folderid_program_data = GUID(0x62AB5D82, 0xFDC1, 0x4DC3, (ctypes.c_ubyte * 8)(0xA9, 0xDD, 0x07, 0x0D, 0x1D, 0x49, 0x5D, 0x97))
    out = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(folderid_program_data), 0, None, ctypes.byref(out)):
        raise PolicyError("cannot locate the ProgramData folder")
    try:
        return Path(out.value)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)


def _windows_owned_by_current_user(path: Path) -> bool:
    import ctypes
    from ctypes import wintypes

    advapi, kernel = ctypes.WinDLL("advapi32"), ctypes.WinDLL("kernel32")
    advapi.GetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD] + [ctypes.c_void_p] * 5
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD)]
    advapi.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    owner, descriptor, token = ctypes.c_void_p(), ctypes.c_void_p(), wintypes.HANDLE()
    # SE_FILE_OBJECT = 1, OWNER_SECURITY_INFORMATION = 1
    if advapi.GetNamedSecurityInfoW(str(path), 1, 1, ctypes.byref(owner), None, None, None, ctypes.byref(descriptor)):
        raise PolicyError(f"cannot read the owner of {path}")
    try:
        advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token))  # TOKEN_QUERY
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # 1 = TokenUser
        buffer = ctypes.create_string_buffer(size.value)
        advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size))
        user_sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_USER.User.Sid
        return bool(advapi.EqualSid(owner, user_sid))
    finally:
        kernel.LocalFree(descriptor)
        kernel.CloseHandle(token)
