"""Bounded synthetic Linux receipt IO, not cutover/recovery authority.

Constructing this object is IO-free. Explicit create() exclusively creates a new
0700 directory below an already-private owned parent. save(dict) publishes a new
0600 numbered JSON snapshot and returns literal True only after fsync.
No reopening/adoption, overwrite, CLI, timer/lease/service or production IO.
"""
import hashlib
import json
import math
import os
import re
import stat
import sys
import threading
import uuid


class ReceiptError(RuntimeError):
    """Receipt unavailable; caller must not infer durable success."""


def _identity(s):
    return s.st_dev, s.st_ino


def _fingerprint(s):
    return (_identity(s), s.st_uid, s.st_gid, s.st_mode, s.st_nlink,
            s.st_size, s.st_mtime_ns, s.st_ctime_ns)


class PrivateReceiptStore:
    """Single-process, bounded API-append-only store; IO/validation failures are terminal.

    max_bytes <= 65536 per document; max_saves <= 256 per store. Additional fixed
    bounds: depth 16, 4096 JSON nodes (including keys), signed 64-bit integers.
    parent is an absolute, symlink-free path to an euid-owned exact-0700 directory.
    name is one safe component. No IO occurs until create().
    """
    def __init__(self, parent, name, *, max_bytes=65536, max_saves=256):
        if (type(parent) is not str or not parent.startswith("/") or
                "\x00" in parent or any(p in (".", "..", "") for p in parent.split("/")[1:])):
            raise ValueError("parent must be an absolute canonical component path")
        if type(name) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", name) is None:
            raise ValueError("one bounded private directory name required")
        if type(max_bytes) is not int or not 1 <= max_bytes <= 65536:
            raise ValueError("max_bytes must be 1..65536")
        if type(max_saves) is not int or not 1 <= max_saves <= 256:
            raise ValueError("max_saves must be 1..256")
        self.parent, self.name = parent, name
        self.max_bytes, self.max_saves = max_bytes, max_saves
        self._state = "new"
        self._parent_fd = self._directory_fd = None
        self._parent_identity = self._directory_identity = None
        self._process = None
        self._files = []  # retained (name, fd, full stat fingerprint, content SHA-256)
        self._mutex = threading.Lock()

    @staticmethod
    def _process_identity():
        return (os.getpid(), os.getuid(), os.geteuid(), os.getgid(), os.getegid())

    @staticmethod
    def _platform():
        if (sys.platform != "linux" or not hasattr(os, "O_NOFOLLOW") or
                not hasattr(os, "O_DIRECTORY")):
            raise ReceiptError("requires Linux no-follow dirfd semantics; no fallback")

    @staticmethod
    def _dir_flags():
        return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC

    def _open_parent(self):
        fd = os.open("/", self._dir_flags())
        try:
            for part in self.parent.split("/")[1:]:
                new_fd = os.open(part, self._dir_flags(), dir_fd=fd)
                os.close(fd)
                fd = new_fd
            s = os.fstat(fd)
            if s.st_uid != self._process[2] or stat.S_IMODE(s.st_mode) != 0o700:
                raise ReceiptError("parent must be explicitly owned and exact 0700")
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _check_process(self):
        if self._process != self._process_identity():
            self._state = "failed"
            raise ReceiptError("creating process/credentials lost; no fork adoption")

    def create(self):
        """Explicit durable exclusive creation; never open an existing store."""
        self._platform()
        if not self._mutex.acquire(blocking=False):
            raise ReceiptError("concurrent receipt operation")
        try:
            if self._state != "new":
                raise ReceiptError("store is single-use")
            self._process = self._process_identity()
            self._state = "failed"
            try:
                self._parent_fd = self._open_parent()
                self._parent_identity = _identity(os.fstat(self._parent_fd))
                os.mkdir(self.name, 0o700, dir_fd=self._parent_fd)
                self._directory_fd = os.open(self.name, self._dir_flags(), dir_fd=self._parent_fd)
                self._directory_identity = _identity(os.fstat(self._directory_fd))
                self._check()
                os.fsync(self._directory_fd)
                os.fsync(self._parent_fd)
                self._check()
                self._state = "open"
                return self
            except BaseException as exc:
                self._release_fds()
                if not isinstance(exc, Exception):
                    raise
                raise ReceiptError("exclusive private store creation failed") from exc
        finally:
            self._mutex.release()

    def _check(self):
        self._check_process()
        fresh = self._open_parent()
        try:
            if _identity(os.fstat(fresh)) != self._parent_identity:
                raise ReceiptError("parent path identity changed")
        finally:
            os.close(fresh)
        ps = os.fstat(self._parent_fd)
        if (_identity(ps) != self._parent_identity or ps.st_uid != self._process[2] or
                stat.S_IMODE(ps.st_mode) != 0o700):
            raise ReceiptError("parent fd identity or privacy changed")
        ds = os.fstat(self._directory_fd)
        named = os.stat(self.name, dir_fd=self._parent_fd, follow_symlinks=False)
        if (not stat.S_ISDIR(named.st_mode) or _identity(named) != self._directory_identity or
                _identity(ds) != self._directory_identity or ds.st_uid != self._process[2] or
                stat.S_IMODE(ds.st_mode) != 0o700 or ds.st_nlink != 2):
            raise ReceiptError("private directory identity or privacy changed")
        expected = {name for name, _, _, _ in self._files}
        seen = set()
        with os.scandir(self._directory_fd) as entries:
            for entry in entries:
                if entry.name not in expected or entry.name in seen:
                    raise ReceiptError("foreign entry in private store")
                seen.add(entry.name)
        if seen != expected:
            raise ReceiptError("receipt disappeared")
        for name, fd, saved, digest in self._files:
            current = os.fstat(fd)
            named = os.stat(name, dir_fd=self._directory_fd, follow_symlinks=False)
            if (not stat.S_ISREG(named.st_mode) or _fingerprint(current) != saved or
                    _fingerprint(named) != saved or current.st_uid != self._process[2] or
                    stat.S_IMODE(current.st_mode) != 0o600 or current.st_nlink != 1):
                raise ReceiptError("receipt fd/path identity or content metadata changed")
            data = os.pread(fd, self.max_bytes + 1, 0)
            if (len(data) != saved[5] or len(data) > self.max_bytes or
                    hashlib.sha256(data).digest() != digest or _fingerprint(os.fstat(fd)) != saved):
                raise ReceiptError("receipt content identity changed")

    def _encode(self, receipt):
        if type(receipt) is not dict:
            raise ReceiptError("receipt must be a plain JSON object")
        budget = [4096, self.max_bytes]
        active = set()

        def visit(value, depth):
            budget[0] -= 1
            if budget[0] < 0 or depth > 16:
                raise ReceiptError("receipt node/depth bound exceeded")
            t = type(value)
            if t is str:
                budget[1] -= len(value)
                if budget[1] < 0:
                    raise ReceiptError("receipt string bound exceeded")
            elif t is int:
                if not -(1 << 63) <= value < (1 << 63):
                    raise ReceiptError("receipt integer bound exceeded")
            elif t is float:
                if not math.isfinite(value):
                    raise ReceiptError("nonfinite JSON number")
            elif value is None or t is bool:
                pass
            elif t in (dict, list):
                if id(value) in active or len(value) > budget[0]:
                    raise ReceiptError("cyclic or oversized container")
                active.add(id(value))
                snapshot = {} if t is dict else []
                if t is dict:
                    for key, item in value.items():
                        if type(key) is not str:
                            raise ReceiptError("JSON object keys must be plain strings")
                        frozen_key = visit(key, depth + 1)
                        snapshot[frozen_key] = visit(item, depth + 1)
                else:
                    for item in value:
                        snapshot.append(visit(item, depth + 1))
                active.remove(id(value))
                return snapshot
            else:
                raise ReceiptError("only plain JSON types supported")
            return value

        snapshot = visit(receipt, 0)
        data = bytearray()
        for chunk in json.JSONEncoder(ensure_ascii=True, allow_nan=False,
                                      separators=(",", ":")).iterencode(snapshot):
            encoded = chunk.encode("ascii")
            if len(data) + len(encoded) > self.max_bytes:
                raise ReceiptError("encoded receipt byte bound exceeded")
            data.extend(encoded)
        return bytes(data)

    def _operation(self, action):
        self._platform()
        if self._process is not None:
            self._check_process()  # check BEFORE inherited mutex; fork cannot deadlock
        if not self._mutex.acquire(blocking=False):
            raise ReceiptError("concurrent receipt operation")
        try:
            if self._state != "open":
                raise ReceiptError("receipt store is not open")
            try:
                self._check()
                return action()
            except BaseException as exc:
                self._state = "failed"
                if not isinstance(exc, Exception):
                    raise
                if isinstance(exc, ReceiptError):
                    raise
                raise ReceiptError("receipt IO failed; durability unknown") from exc
        finally:
            self._mutex.release()

    def save(self, receipt):
        """Publish without overwrite; IO/validation failure poisons the handle."""
        def write():
            if len(self._files) >= self.max_saves:
                raise ReceiptError("receipt store capacity exhausted")
            data = self._encode(receipt)
            number = len(self._files) + 1
            final = f"receipt-{number:06d}.json"
            temporary = ".pending-" + uuid.uuid4().hex
            fd = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                         0o600, dir_fd=self._directory_fd)
            retained = False
            pending_identity = None
            witness_fd = None
            try:
                # Capture identity through an independent descriptor before the
                # validation fstat. Cleanup must never derive ownership from the
                # mutable pending pathname: a hardlink/path replacement can make
                # that pathname identify a foreign descriptor after fd reuse.
                witness_fd = os.dup(fd)
                pending_identity = _identity(os.fstat(witness_fd))
                os.close(witness_fd)
                witness_fd = None
                s = os.fstat(fd)
                if (not stat.S_ISREG(s.st_mode) or s.st_uid != self._process[2] or
                        stat.S_IMODE(s.st_mode) != 0o600 or s.st_nlink != 1 or s.st_size != 0):
                    raise ReceiptError("new receipt is not exclusive/private")
                offset = 0
                while offset < len(data):
                    try:
                        count = os.write(fd, data[offset:])
                    except InterruptedError:
                        continue
                    if count <= 0:
                        raise ReceiptError("receipt write made no progress")
                    offset += count
                if os.pread(fd, self.max_bytes + 1, 0) != data:
                    raise ReceiptError("receipt readback did not match bounded JSON")
                os.fsync(fd)
                # link is atomic and no-clobber even if final appeared after preflight.
                os.link(temporary, final, src_dir_fd=self._directory_fd,
                        dst_dir_fd=self._directory_fd, follow_symlinks=False)
                os.unlink(temporary, dir_fd=self._directory_fd)
                os.fsync(fd)
                self._files.append((final, fd, _fingerprint(os.fstat(fd)), hashlib.sha256(data).digest()))
                retained = True
                os.fsync(self._directory_fd)
                self._check()
                return True
            finally:
                if witness_fd is not None:
                    try:
                        os.close(witness_fd)
                    except OSError:
                        pass  # best-effort close of our independent witness
                if not retained:
                    try:
                        # Do not use temporary's pathname as an ownership proof.
                        # If validation failed before the witness was captured, or
                        # fd was reassigned, preserve the descriptor and evidence.
                        if pending_identity is not None and _identity(os.fstat(fd)) == pending_identity:
                            os.close(fd)
                    except OSError:
                        pass  # best-effort, no retry; leave uncertain evidence
        return self._operation(write)

    def read_latest(self):
        """Bounded read through the continuous owned fd, not restart recovery."""
        def read():
            if not self._files:
                return None
            _, fd, saved, digest = self._files[-1]
            data = os.pread(fd, self.max_bytes + 1, 0)
            if (len(data) > self.max_bytes or len(data) != saved[5] or
                    hashlib.sha256(data).digest() != digest):
                raise ReceiptError("receipt read size changed")
            result = json.loads(data)
            self._encode(result)
            self._check()
            return result
        return self._operation(read)

    def _release_fds(self):
        fds = [(fd, saved[0]) for _, fd, saved, _ in self._files]
        fds += [(self._directory_fd, self._directory_identity),
                (self._parent_fd, self._parent_identity)]
        self._files = []
        self._directory_fd = self._parent_fd = None
        for fd, expected in fds:
            if fd is not None:
                try:
                    # A lost descriptor number may now belong to unrelated IO.
                    if expected is not None and _identity(os.fstat(fd)) != expected:
                        continue
                    os.close(fd)
                except OSError:
                    pass  # best-effort close, never retry a potentially reused fd

    def close(self):
        """Close only this process's descriptors; never unlink stored evidence."""
        if self._process is not None:
            self._check_process()
        if not self._mutex.acquire(blocking=False):
            raise ReceiptError("concurrent receipt operation")
        try:
            self._state = "closed"
            self._release_fds()
        finally:
            self._mutex.release()








