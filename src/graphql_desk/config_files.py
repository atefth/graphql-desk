"""Explicit configuration-file staging; importing performs no filesystem work."""

import base64
import errno
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast
from uuid import uuid4

MAX_CONFIG_BYTES = 1024 * 1024
MAX_RECEIPT_BYTES = 32 * 1024
_TEMPORARY = re.compile(
    r"\.graphql-desk-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.tmp\Z",
    re.IGNORECASE,
)
_HASH = re.compile(r"[0-9a-f]{64}\Z")
DirectoryAnchor = tuple[tuple[Path, tuple[int, ...]], ...]


@dataclass(frozen=True)
class ConfigSnapshot:
    data: bytes
    identity: os.stat_result
    dacl: bytes | None


def check_config_acl(descriptor: int) -> None:
    if sys.platform == "win32":
        return
    if sys.platform == "linux":
        try:
            os.getxattr(descriptor, "system.posix_acl_access")
        except OSError as error:
            if error.errno == errno.ENODATA:
                return
            raise
        raise OSError("Extended configuration ACLs are not supported")
    if sys.platform == "darwin":
        import ctypes

        native = ctypes.CDLL(None, use_errno=True)
        native.fpathconf.argtypes = [ctypes.c_int, ctypes.c_int]
        native.fpathconf.restype = ctypes.c_long
        # Opaque remote authorization cannot be established from the local ACL.
        ctypes.set_errno(0)
        opaque = native.fpathconf(descriptor, 14)
        if opaque != 0:
            raise OSError("Configuration authorization is opaque or unverified")
        ctypes.set_errno(0)
        capability = native.fpathconf(descriptor, 13)
        if capability == 0:
            return
        if capability != 1:
            raise OSError("Configuration ACL capability is unverified")
        native.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
        native.acl_get_fd_np.restype = ctypes.c_void_p
        native.acl_get_entry.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        native.acl_get_entry.restype = ctypes.c_int
        native.acl_free.argtypes = [ctypes.c_void_p]
        native.acl_free.restype = ctypes.c_int
        ctypes.set_errno(0)
        acl = native.acl_get_fd_np(descriptor, 0x100)
        if not acl:
            if ctypes.get_errno() == errno.ENOENT:
                return
            raise OSError("Configuration ACL could not be read")
        try:
            entry = ctypes.c_void_p()
            ctypes.set_errno(0)
            result = native.acl_get_entry(acl, 0, ctypes.byref(entry))
            if result == -1 and ctypes.get_errno() == errno.EINVAL:
                return
            raise OSError("Extended or unverified configuration ACL is unsupported")
        finally:
            ctypes.set_errno(0)
            if native.acl_free(acl) != 0:
                raise OSError("Configuration ACL handle could not be freed")
    raise OSError("Configuration ACL absence could not be verified")


def config_dacl(descriptor: int) -> bytes | None:
    """Capture owner and DACL in one self-relative security descriptor."""
    if sys.platform != "win32":
        return None
    import ctypes
    import msvcrt
    from ctypes import wintypes

    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.POINTER(ctypes.c_void_p)
    security.GetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        pointer,
        pointer,
        pointer,
        pointer,
        pointer,
    ]
    security.GetSecurityInfo.restype = wintypes.DWORD
    security.GetSecurityDescriptorLength.argtypes = [ctypes.c_void_p]
    security.GetSecurityDescriptorLength.restype = wintypes.DWORD
    security.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    security.GetSecurityDescriptorControl.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    result = ctypes.c_void_p()
    error = security.GetSecurityInfo(
        msvcrt.get_osfhandle(descriptor),
        1,
        5,
        None,
        None,
        None,
        None,
        ctypes.byref(result),
    )
    if error:
        raise ctypes.WinError(error)
    try:
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not security.GetSecurityDescriptorControl(
            result, ctypes.byref(control), ctypes.byref(revision)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not control.value & 0x8000:
            raise OSError("Configuration security is not self-relative")
        return ctypes.string_at(result, security.GetSecurityDescriptorLength(result))
    finally:
        kernel.LocalFree(result)


def _same_config_owner(original: bytes | None, staged: bytes | None) -> bool:
    if sys.platform != "win32":
        return True
    if original is None or staged is None:
        return False
    import ctypes
    from ctypes import wintypes

    security = ctypes.WinDLL("advapi32", use_last_error=True)
    security.GetSecurityDescriptorOwner.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    security.GetSecurityDescriptorOwner.restype = wintypes.BOOL
    security.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    security.EqualSid.restype = wintypes.BOOL
    source, destination = ctypes.c_void_p(), ctypes.c_void_p()
    defaulted = wintypes.BOOL()
    original_buffer = ctypes.create_string_buffer(original)
    staged_buffer = ctypes.create_string_buffer(staged)
    for descriptor, owner in (
        (original_buffer, source),
        (staged_buffer, destination),
    ):
        if not security.GetSecurityDescriptorOwner(
            descriptor, ctypes.byref(owner), ctypes.byref(defaulted)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    return bool(
        source.value and destination.value and security.EqualSid(source, destination)
    )


def apply_config_dacl(descriptor: int, dacl: bytes | None) -> None:
    if sys.platform != "win32":
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes

    if dacl is None:
        raise OSError("Original configuration security was not captured")
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.ReOpenFile.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel.ReOpenFile.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    security.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    security.GetSecurityDescriptorControl.restype = wintypes.BOOL
    security.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    security.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    security.SetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    security.SetSecurityInfo.restype = wintypes.DWORD
    original = ctypes.create_string_buffer(dacl)
    control, revision = wintypes.WORD(), wintypes.DWORD()
    present, defaulted, acl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
    if not security.GetSecurityDescriptorControl(
        original, ctypes.byref(control), ctypes.byref(revision)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if not security.GetSecurityDescriptorDacl(
        original, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if not present.value:
        raise OSError("Absent configuration DACL cannot be preserved")
    # Reopen the same file object with WRITE_DAC; Python's ordinary fd lacks it.
    handle = kernel.ReOpenFile(msvcrt.get_osfhandle(descriptor), 0x60000, 7, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        protection = 0x80000000 if control.value & 0x1000 else 0x20000000
        error = security.SetSecurityInfo(
            handle, 1, 4 | protection, None, None, acl, None
        )
        if error:
            raise ctypes.WinError(error)
    finally:
        kernel.CloseHandle(handle)


def _file_identity(identity: os.stat_result) -> tuple[int, ...]:
    # ctime may change during a same-object rename; it is not a file identity.
    return (
        identity.st_dev,
        identity.st_ino,
        identity.st_size,
        identity.st_mtime_ns,
        identity.st_mode,
        identity.st_uid,
        identity.st_gid,
    )


def same_output(path: Path, expected: ConfigSnapshot) -> bool:
    try:
        current = path.lstat()
        previous = _file_identity(expected.identity)
        if not stat.S_ISREG(current.st_mode) or _file_identity(current) != previous:
            return False
        with path.open("rb") as stream:
            if _file_identity(os.fstat(stream.fileno())) != previous:
                return False
            check_config_acl(stream.fileno())
            return (
                stream.read(len(expected.data) + 1) == expected.data
                and config_dacl(stream.fileno()) == expected.dacl
                and _file_identity(os.fstat(stream.fileno())) == previous
                and _file_identity(path.lstat()) == previous
            )
    except OSError:
        return False


def preserve_config_owner(
    descriptor: int, original: ConfigSnapshot, staged_dacl: bytes | None
) -> None:
    if not _same_config_owner(original.dacl, staged_dacl):
        raise OSError("Configuration owner cannot be preserved")
    if sys.platform != "win32":
        identity = original.identity
        os.fchown(descriptor, identity.st_uid, identity.st_gid)
        owner = os.fstat(descriptor)
        if (owner.st_uid, owner.st_gid) != (identity.st_uid, identity.st_gid):
            raise OSError("Configuration ownership was not preserved")
        os.fchmod(descriptor, stat.S_IMODE(identity.st_mode))


def _directory_identity(identity: os.stat_result) -> tuple[int, ...]:
    return (
        identity.st_dev,
        identity.st_ino,
        identity.st_mode,
        identity.st_uid,
        identity.st_gid,
    )


def _safe_directory(path: Path) -> tuple[int, ...]:
    identity = path.lstat()
    if not stat.S_ISDIR(identity.st_mode) or getattr(
        identity, "st_file_attributes", 0
    ) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        raise ValueError("Configuration directory is redirected or unavailable")
    return _directory_identity(identity)


def capture_config_directories(workspace: Path) -> DirectoryAnchor:
    """Anchor an explicitly supplied workspace without creating anything."""
    resolved = workspace.resolve(strict=True)
    if not workspace.is_absolute() or workspace != resolved:
        raise ValueError("Configuration workspace must be an absolute real directory")
    workspace = resolved
    return tuple(
        (path, _safe_directory(path))
        for path in reversed((workspace, *workspace.parents))
    )


def _check_directories(directories: DirectoryAnchor) -> None:
    for path, identity in directories:
        if _safe_directory(path) != identity:
            raise ValueError("Configuration directory changed")


def _capture_config(path: Path, *, writable: bool = False) -> ConfigSnapshot:
    identity = path.lstat()
    if not stat.S_ISREG(identity.st_mode) or identity.st_size > MAX_CONFIG_BYTES:
        raise ValueError("Configuration must be a bounded regular file")
    if writable and not identity.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError("Configuration is read-only")
    with path.open("r+b" if writable else "rb") as stream:
        if _file_identity(os.fstat(stream.fileno())) != _file_identity(identity):
            raise ValueError("Configuration changed while opening")
        check_config_acl(stream.fileno())
        data = stream.read(MAX_CONFIG_BYTES + 1)
        if len(data) > MAX_CONFIG_BYTES:
            raise ValueError("Configuration is too large")
        captured = ConfigSnapshot(data, identity, config_dacl(stream.fileno()))
    if not same_output(path, captured):
        raise ValueError("Configuration changed while reading")
    return captured


def _proof(snapshot: ConfigSnapshot) -> list[str]:
    # Native IDs are decimal strings, never lossy JSON numbers or Node inode IDs.
    return [
        *(str(value) for value in _file_identity(snapshot.identity)),
        hashlib.sha256(snapshot.data).hexdigest(),
        hashlib.sha256(snapshot.dacl or b"").hexdigest(),
    ]


def _matches_proof(path: Path, proof: list[str]) -> bool:
    try:
        return _proof(_capture_config(path)) == proof
    except (OSError, ValueError):
        return False


def _absent(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    return False


@dataclass(repr=False)
class ConfigFileStage:
    """One explicit staging capability; only the caller commits a replacement."""

    directories: DirectoryAnchor
    target: Path
    temporary: Path
    token: str = field(default_factory=lambda: uuid4().hex)
    original: list[str] | None = None
    staged: list[str] | None = None
    complete: bool = False
    revoked: bool = False
    attempted: bool = False
    failure_phase: str = field(default="create-empty", init=False)

    @classmethod
    def capture(
        cls, directories: DirectoryAnchor, file: str, temporary: Path
    ) -> "ConfigFileStage":
        _check_directories(directories)
        workspace = directories[-1][0]
        if file not in {"pyproject.toml", "pyrightconfig.json"} or (
            temporary.parent != workspace or not _TEMPORARY.fullmatch(temporary.name)
        ):
            raise ValueError("Configuration staging path is not permitted")
        return cls(directories, workspace / file, temporary)

    def write(self, text: str, expected_hash: str) -> None:
        if self.revoked or self.original is not None:
            raise ValueError("Configuration stage is no longer writable")
        data = text.encode("utf-8")
        if len(data) > MAX_CONFIG_BYTES or not _HASH.fullmatch(expected_hash):
            raise ValueError("Invalid configuration content or fingerprint")
        _check_directories(self.directories)
        original = _capture_config(self.target, writable=True)
        original.data.decode("utf-8")
        self.original = _proof(original)
        if self.original[-2] != expected_hash:
            raise ValueError("Configuration source changed")
        _check_directories(self.directories)
        self.attempted = True
        descriptor = os.open(self.temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            self.failure_phase = "read-empty-security"
            self.staged = _proof(
                ConfigSnapshot(b"", os.fstat(descriptor), config_dacl(descriptor))
            )
            check_config_acl(descriptor)
            self.failure_phase = "preserve-owner"
            preserve_config_owner(descriptor, original, config_dacl(descriptor))
            self.staged = _proof(
                ConfigSnapshot(b"", os.fstat(descriptor), config_dacl(descriptor))
            )
            self.failure_phase = "copy-dacl"
            apply_config_dacl(descriptor, original.dacl)
            self.failure_phase = "compare-dacl"
            check_config_acl(descriptor)
            if config_dacl(descriptor) != original.dacl:
                raise ValueError("Configuration authorization was not preserved")
            self.staged = _proof(
                ConfigSnapshot(b"", os.fstat(descriptor), config_dacl(descriptor))
            )
            self.failure_phase = "recheck-empty"
            _check_directories(self.directories)
            if not same_output(self.target, original) or not _matches_proof(
                self.temporary, self.staged
            ):
                raise ValueError("Configuration source or empty stage changed")
            self.failure_phase = "write-content"
            stream.write(data)
            stream.flush()
            self.staged = _proof(
                ConfigSnapshot(data, os.fstat(descriptor), config_dacl(descriptor))
            )
            os.fsync(descriptor)
        self.complete = True
        self.failure_phase = "verify-stage"
        self.verify()

    def verify(self) -> None:
        if self.revoked or not self.complete:
            raise ValueError("Configuration stage is revoked or incomplete")
        _check_directories(self.directories)
        if (
            self.original is None
            or self.staged is None
            or not (
                _matches_proof(self.target, self.original)
                and _matches_proof(self.temporary, self.staged)
            )
        ):
            raise ValueError("Configuration staging proof changed")
        _check_directories(self.directories)

    def verify_discard(self) -> None:
        """Verify cleanup authority; the caller owns its final synchronous unlink."""
        _check_directories(self.directories)
        if _absent(self.temporary):
            _check_directories(self.directories)
            return
        if self.staged is None or not _matches_proof(self.temporary, self.staged):
            raise ValueError("Unverified configuration temporary must be preserved")
        _check_directories(self.directories)

    def discard(self) -> None:
        """Confirm the caller removed the stage without deleting any file here."""
        _check_directories(self.directories)
        if not _absent(self.temporary):
            raise ValueError("Configuration temporary is still present; preserved")
        _check_directories(self.directories)

    def finish(self) -> None:
        _check_directories(self.directories)
        if (
            self.staged is None
            or not _absent(self.temporary)
            or not _matches_proof(self.target, self.staged)
        ):
            raise ValueError("Published configuration could not be verified")
        _check_directories(self.directories)

    def receipt(self) -> str:
        if not self.complete or self.original is None or self.staged is None:
            raise ValueError("Incomplete configuration stage requires manual recovery")
        record = {
            "version": 1,
            "directories": [
                [str(path), [str(value) for value in identity]]
                for path, identity in self.directories
            ],
            "file": self.target.name,
            "temporary": str(self.temporary),
            "token": self.token,
            "original": self.original,
            "staged": self.staged,
        }
        receipt = base64.b64encode(json.dumps(record, sort_keys=True).encode()).decode()
        if len(receipt) > MAX_RECEIPT_BYTES:
            raise ValueError("Configuration receipt is too large")
        return receipt

    @classmethod
    def resume(cls, directories: DirectoryAnchor, receipt: str) -> "ConfigFileStage":
        if len(receipt) > MAX_RECEIPT_BYTES:
            raise ValueError("Configuration receipt is too large")
        value: object = json.loads(base64.b64decode(receipt, validate=True))
        keys = {
            "version",
            "directories",
            "file",
            "temporary",
            "token",
            "original",
            "staged",
        }
        if not isinstance(value, dict):
            raise ValueError("Invalid configuration receipt")
        record = cast(dict[str, object], value)
        if set(record) != keys or (
            type(record["version"]) is not int or record["version"] != 1
        ):
            raise ValueError("Invalid configuration receipt")
        expected = [
            [str(path), [str(item) for item in identity]]
            for path, identity in directories
        ]
        if record["directories"] != expected:
            raise ValueError("Configuration receipt workspace changed")
        file, temporary, token = record["file"], record["temporary"], record["token"]
        if (
            not isinstance(file, str)
            or not isinstance(temporary, str)
            or not (isinstance(token, str) and re.fullmatch(r"[0-9a-f]{32}", token))
        ):
            raise ValueError("Invalid configuration receipt")
        restored = cls.capture(directories, file, Path(temporary))
        proofs: list[list[str]] = []
        for name in ("original", "staged"):
            proof = record[name]
            if not isinstance(proof, list):
                raise ValueError("Invalid configuration receipt proof")
            values = cast(list[object], proof)
            if len(values) != 9 or not all(isinstance(item, str) for item in values):
                raise ValueError("Invalid configuration receipt proof")
            selected = cast(list[str], proof)
            if (
                not all(re.fullmatch(r"-?[0-9]{1,40}", item) for item in selected[:7])
                or not all(_HASH.fullmatch(item) for item in selected[7:])
                or not 0 <= int(selected[2]) <= MAX_CONFIG_BYTES
            ):
                raise ValueError("Invalid configuration receipt proof")
            proofs.append(selected)
        restored.token = token
        restored.original, restored.staged = proofs
        restored.complete = restored.revoked = True
        if _absent(restored.temporary):
            if not (
                _matches_proof(restored.target, restored.original)
                or _matches_proof(restored.target, restored.staged)
            ):
                raise ValueError("Configuration recovery target changed")
        elif not _matches_proof(restored.temporary, restored.staged):
            raise ValueError("Configuration recovery stage changed")
        _check_directories(directories)
        return restored
