"""Continuous POSIX flock capability. Explicit caller paths, no CLI or service IO.

No lock files are created/deleted/rewritten. FDs stay private and are never
unlocked or reacquired by valid(). Closing a lease is terminal. This is a
cooperative lock, NOT a gate for unrelated/manual noncooperating writers.
"""
import os
import stat
from pathlib import Path
try:
    import fcntl
except ImportError:
    fcntl = None

def _start_ticks():
    if os.name != "posix":
        return 0
    fields = Path("/proc/self/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
    if len(fields) <= 19 or not fields[19].isdigit() or int(fields[19]) <= 0:
        raise LeaseError("process start ticks unavailable")
    return int(fields[19])


class LeaseError(RuntimeError):
    """Sanitized admission refusal."""

def _open(path):
    """No-follow traversal including every absolute ancestor."""
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC)
    try:
        for component in path.parts[1:-1]:
            child=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=fd)
            os.close(fd);fd=child
        return os.open(path.name,os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK,dir_fd=fd)
    finally:os.close(fd)

def _identity(fd):
    st=os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1:raise ValueError('not single regular lock')
    return st.st_dev,st.st_ino

class MaintenanceLease:
    def __init__(self):
        self._fds=[]
        self._paths=[]
        self._closed=False
        self._lost=False
        self._pid=os.getpid()
        self._start_ticks=_start_ticks()
    @classmethod
    def acquire(cls,paths):
        if fcntl is None or os.name != "posix":
            raise LeaseError("POSIX flock required")
        lease=cls()
        try:
            paths=tuple(Path(p) for p in paths)
            if not paths or len(set(paths))!=len(paths):raise ValueError('exact nonempty unique paths required')
            if any(not p.is_absolute() or '..' in p.parts or p==Path('/') for p in paths):
                raise ValueError('absolute specific paths required')
            for path in paths:
                fd=_open(path)
                lease._fds.append(fd)
                identity=_identity(fd)
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                lease._paths.append((path,identity))
            if not lease.valid():raise ValueError('lock changed during admission')
            return lease
        except BaseException as error:
            lease.close()
            if isinstance(error,Exception):
                raise LeaseError('maintenance locks unavailable or unsafe') from None
            raise
    def identities(self):
        return tuple((str(path), identity[0], identity[1], self._pid, self._start_ticks)
                     for path, identity in self._paths)
    def valid(self):
        if self._closed or self._lost:return False
        if not self._fds or len(self._fds)!=len(self._paths):
            self._lost=True
            return False
        try:
            if self._pid!=os.getpid() or self._start_ticks!=_start_ticks():raise ValueError('forked or changed lease')
            for fd,(path,identity) in zip(self._fds,self._paths):
                current=_open(path)
                try:
                    if _identity(current)!=identity or _identity(fd)!=identity:
                        raise ValueError('lock identity changed')
                finally:os.close(current)
            return True
        except Exception:
            self._lost=True
            return False
    def close(self):
        if self._closed:return
        self._closed=True
        for fd in reversed(self._fds):
            try:os.close(fd)
            except OSError:pass
        self._fds=[]
    def __enter__(self):return self
    def __exit__(self,*args):self.close()