"""Encrypt integration secrets at rest.

Production on Windows uses DPAPI bound to the current user. Tests inject a
protector. Non-Windows production without an injected protector fails closed.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes


class ProtectorError(RuntimeError):
    pass


class TokenProtector:
    def protect(self, plaintext: bytes) -> bytes:
        raise NotImplementedError

    def unprotect(self, blob: bytes) -> bytes:
        raise NotImplementedError


class MemoryProtector(TokenProtector):
    """Test-only protector. Does not provide durable secrecy."""

    def protect(self, plaintext: bytes) -> bytes:
        if not isinstance(plaintext, (bytes, bytearray)):
            raise ProtectorError("invalid secret")
        return b"mem:" + bytes(plaintext)

    def unprotect(self, blob: bytes) -> bytes:
        if not isinstance(blob, (bytes, bytearray)) or not bytes(blob).startswith(b"mem:"):
            raise ProtectorError("invalid secret blob")
        return bytes(blob)[4:]


class DpapiProtector(TokenProtector):
    """Windows DPAPI encryption for the interactive user."""

    def protect(self, plaintext: bytes) -> bytes:
        return _dpapi_protect(bytes(plaintext))

    def unprotect(self, blob: bytes) -> bytes:
        return _dpapi_unprotect(bytes(blob))


def default_protector() -> TokenProtector:
    if sys.platform == "win32":
        return DpapiProtector()
    raise ProtectorError(
        "Encrypted token storage requires Windows DPAPI, or an injected protector for tests."
    )


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_protect(plaintext: bytes) -> bytes:
    if not plaintext:
        raise ProtectorError("empty secret")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(plaintext)
    blob_in = DATA_BLOB(len(plaintext), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise ProtectorError("DPAPI protect failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(blob: bytes) -> bytes:
    if not blob:
        raise ProtectorError("empty secret blob")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(blob)
    blob_in = DATA_BLOB(len(blob), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise ProtectorError("DPAPI unprotect failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)
