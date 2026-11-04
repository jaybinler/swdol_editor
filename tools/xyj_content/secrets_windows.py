"""Windows user-bound password protection; cleartext is never persisted or returned by an API."""

import ctypes
from ctypes import wintypes
import os

from .binary import ContentError


class _Blob(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data):
    buffer = ctypes.create_string_buffer(data, max(1, len(data)))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _transform(data: bytes, entropy: bytes, protect: bool) -> bytes:
    if os.name != 'nt':
        raise ContentError('记住密码仅支持 Windows；可取消记住密码后在当前会话使用。')
    source, source_buffer = _blob(data)
    extra, entropy_buffer = _blob(entropy)
    output = _Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    fn = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p,
                   ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(source), None, ctypes.byref(extra), None, None, 1, ctypes.byref(output)):
        raise ContentError('Windows 密码保护不可用或密码属于其他用户／机器，请重新输入密码。')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(ctypes.cast(output.data, ctypes.c_void_p))


def protect_password(password: str, binding: bytes) -> bytes:
    """Encrypt a password for the current Windows user and this exact connection identity."""
    return _transform(password.encode('utf-8'), binding, True)


def unprotect_password(ciphertext: bytes, binding: bytes) -> str:
    """Recover a password only in memory; a migrated encrypted file cannot silently become a new credential."""
    try:
        return _transform(ciphertext, binding, False).decode('utf-8')
    except UnicodeError as exc:
        raise ContentError('已保护密码无效，请重新输入。') from exc
