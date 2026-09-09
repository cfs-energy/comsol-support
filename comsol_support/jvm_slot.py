"""jvm_slot — advisory exclusive-COMSOL-JVM slot + leaked-JVM detection.

Why: two overlapping ~48 GB COMSOL
JVMs made a native SIGABRT permanently unattributable and contended for
license seats and scratch — the campaign adopted a one-JVM-at-a-time
rule (R-2) and hand-rolled slot chaining through a log file. Separately,
one leaked JVM from a timed-out probe survived ~2d19h at 3 GB RSS,
possibly holding a license seat, before anyone noticed.

This module provides both halves as harness plumbing:

- ``acquire_jvm_slot(purpose)`` — an advisory ``flock`` on a per-user
  lock file. Default behavior is WARN-AND-PROCEED on contention (the
  rule is advisory; breaking existing concurrent workflows would be
  worse than warning). Set ``COMSOL_AGENT_EXCLUSIVE=1`` to enforce:
  the acquire then waits for the slot up to
  ``COMSOL_AGENT_SLOT_TIMEOUT`` seconds (default 3600) and raises
  ``JvmSlotTimeout`` if it never frees. The holder's pid/purpose/start
  time are written into the lock file so a contender can say WHO holds
  the slot.
- ``scan_leaked_jvms()`` — a /proc sweep for pre-existing COMSOL JVMs
  (java processes whose cmdline mentions COMSOL plugins or our
  harness classes), reporting pid, age, RSS and a cmdline head so the
  caller can warn about orphans possibly holding license seats.

The slot lock works on POSIX (``flock``) and Windows (``msvcrt``
byte-range lock at a high offset, so the holder-info JSON at the start
of the file stays readable by contenders); elsewhere it degrades to a
no-op. The leaked-JVM scan reads /proc on Linux, queries
Win32_Process via PowerShell/CIM on Windows, and sweeps ``ps`` output
on macOS (no /proc there); elsewhere it returns []. Stdlib only.
"""

from __future__ import annotations

import getpass
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("comsol_support.jvm_slot")

#: Signature substrings that mark a java process as a COMSOL JVM.
_COMSOL_MARKERS = ("com.comsol", "comsol", "ModelExporter",
                   "SlotHarvester", "ModelChecker")

#: Lower-cased once — every sweep matches case-insensitively.
_MARKERS_LOWER = tuple(m.lower() for m in _COMSOL_MARKERS)

_ENFORCE_ENV = "COMSOL_AGENT_EXCLUSIVE"
_TIMEOUT_ENV = "COMSOL_AGENT_SLOT_TIMEOUT"
_POLL_S = 2.0


class JvmSlotTimeout(RuntimeError):
    """Enforced slot acquisition timed out while another JVM held it."""


#: Byte offset of the (1-byte) lock region. Far past any holder-info
#: JSON so that on Windows — where byte-range locks are mandatory — the
#: holder info at offset 0 stays readable by contenders.
_LOCK_OFFSET = 1 << 30


def _try_lock_nb(fh) -> bool:
    """Non-blocking exclusive lock attempt on an open file. True if won."""
    if os.name == "nt":
        import msvcrt
        try:
            fh.flush()
            os.lseek(fh.fileno(), _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fh) -> None:
    """Release a lock taken by :func:`_try_lock_nb`. Raises OSError on
    failure; callers treat release as best-effort."""
    if os.name == "nt":
        import msvcrt
        fh.flush()
        os.lseek(fh.fileno(), _LOCK_OFFSET, os.SEEK_SET)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fh, fcntl.LOCK_UN)


def default_lock_path() -> Path:
    """Per-user lock file in the system temp dir (machine-wide per user)."""
    user = "unknown"
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 — never fail over a username lookup
        pass
    return Path(tempfile.gettempdir()) / f"comsol-support-jvm-{user}.lock"


@dataclass
class JvmSlot:
    """Handle for an acquired (or contended-and-proceeded) slot.

    ``acquired`` is False when the slot was busy and we proceeded anyway
    (warn mode). ``holder`` carries the busy holder's recorded info in
    that case. Always call :meth:`release` (idempotent) in a finally.
    """
    acquired: bool
    lock_path: Path
    purpose: str = ""
    holder: dict | None = None
    waited_ms: int = 0
    _fh: object | None = field(default=None, repr=False)

    def as_event_payload(self) -> dict:
        """Payload for a telemetry `jvm_slot` event."""
        return {
            "acquired": self.acquired,
            "purpose": self.purpose,
            "lock_path": str(self.lock_path),
            "waited_ms": self.waited_ms,
            "holder": self.holder,
        }

    def release(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is None:
            return
        try:
            _unlock(fh)
        except Exception:  # noqa: BLE001
            pass
        try:
            fh.close()
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "JvmSlot":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _read_holder(lock_path: Path) -> dict | None:
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
        if raw:
            info = json.loads(raw)
            if isinstance(info, dict):
                return info
    except Exception:  # noqa: BLE001 — holder info is best-effort
        pass
    return None


def acquire_jvm_slot(
    purpose: str,
    *,
    lock_path: Path | None = None,
    enforce: bool | None = None,
    timeout_s: float | None = None,
) -> JvmSlot:
    """Acquire the advisory COMSOL-JVM slot.

    Args:
      purpose: short human-readable description recorded as holder info
        (e.g. ``"edit-mph model.mph"``).
      lock_path: override the per-user default (mainly for tests).
      enforce: override the ``COMSOL_AGENT_EXCLUSIVE`` env switch.
      timeout_s: override the ``COMSOL_AGENT_SLOT_TIMEOUT`` env value
        (seconds; only meaningful when enforcing).

    Returns a :class:`JvmSlot`; in warn mode the slot may have
    ``acquired=False`` (busy — we logged who holds it and proceeded).
    Raises :class:`JvmSlotTimeout` only in enforce mode.
    """
    lp = lock_path or default_lock_path()
    if enforce is None:
        enforce = os.environ.get(_ENFORCE_ENV, "").lower() in ("1", "true", "yes")
    if timeout_s is None:
        try:
            timeout_s = float(os.environ.get(_TIMEOUT_ENV, "3600"))
        except ValueError:
            timeout_s = 3600.0

    if os.name not in ("posix", "nt"):
        return JvmSlot(acquired=True, lock_path=lp, purpose=purpose)

    t0 = time.monotonic()
    try:
        # "a+" so opening never truncates a live holder's info.
        fh = open(lp, "a+", encoding="utf-8")
    except OSError as e:
        logger.warning("jvm_slot: lock file unavailable (%s): %s — "
                       "proceeding unlocked", lp, e)
        return JvmSlot(acquired=False, lock_path=lp, purpose=purpose)

    while True:
        if _try_lock_nb(fh):
            # Slot is ours: record holder info for future contenders.
            try:
                fh.seek(0)
                fh.truncate()
                fh.write(json.dumps({
                    "pid": os.getpid(),
                    "purpose": purpose,
                    "since": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }) + "\n")
                fh.flush()
            except Exception:  # noqa: BLE001
                pass
            return JvmSlot(
                acquired=True, lock_path=lp, purpose=purpose,
                waited_ms=int((time.monotonic() - t0) * 1000), _fh=fh)
        else:
            holder = _read_holder(lp)
            if not enforce:
                logger.warning(
                    "jvm_slot: another COMSOL JVM holds the slot (%s) — "
                    "proceeding anyway. Overlapping COMSOL JVMs contend "
                    "for license seats and make native crashes "
                    "unattributable (G-SIGABRT-NO-HSERR); set "
                    "%s=1 to enforce one JVM at a time.",
                    holder or "holder unknown", _ENFORCE_ENV)
                try:
                    fh.close()
                except Exception:  # noqa: BLE001
                    pass
                return JvmSlot(acquired=False, lock_path=lp,
                               purpose=purpose, holder=holder,
                               waited_ms=int((time.monotonic() - t0) * 1000))
            if time.monotonic() - t0 > timeout_s:
                try:
                    fh.close()
                except Exception:  # noqa: BLE001
                    pass
                raise JvmSlotTimeout(
                    f"COMSOL JVM slot still held after {timeout_s:.0f}s "
                    f"by {holder or 'unknown holder'} ({lp})")
            time.sleep(_POLL_S)


def _scan_leaked_jvms_windows(exclude: set[int]) -> list[dict]:
    """Windows sweep: query java.exe processes via PowerShell/CIM.

    Emits the same record shape as the /proc scan. Marker matching is
    case-insensitive here — Windows install paths spell it `COMSOL`,
    and the plugins/* classpath wildcard means lowercase jar names
    never appear on the command line. Never raises; any failure
    (PowerShell missing, timeout, malformed JSON) returns [].
    """
    ps_script = (
        "Get-CimInstance Win32_Process -Filter \"Name='java.exe'\" | "
        "ForEach-Object { [pscustomobject]@{ "
        "pid = $_.ProcessId; cmd = $_.CommandLine; "
        "rss = $_.WorkingSetSize; "
        "age_s = if ($_.CreationDate) { [math]::Round(((Get-Date) - "
        "$_.CreationDate).TotalSeconds, 1) } else { -1.0 } } } | "
        "ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-Command", ps_script],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
        )
        raw = (result.stdout or "").strip()
        if not raw:
            return []
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001 — the sweep must never raise; any
        # failure (PowerShell missing, timeout, malformed JSON, or a
        # test double patched over subprocess) degrades to "no leaks".
        return []
    if isinstance(parsed, dict):  # single process serializes bare
        parsed = [parsed]
    if not isinstance(parsed, list):
        return []

    out: list[dict] = []
    markers = _MARKERS_LOWER
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        try:
            pid = int(entry.get("pid"))
        except (TypeError, ValueError):
            continue
        if pid in exclude or pid == os.getpid():
            continue
        cmd = entry.get("cmd") or ""
        if not any(m in cmd.lower() for m in markers):
            continue
        try:
            rss = int(entry.get("rss"))
        except (TypeError, ValueError):
            rss = -1
        try:
            age_s = float(entry.get("age_s"))
        except (TypeError, ValueError):
            age_s = -1.0
        out.append({
            "pid": pid,
            "age_s": age_s,
            "rss_bytes": rss,
            "cmdline_head": cmd[:200],
        })
    return out


def _parse_etime(raw: str) -> float:
    """Parse a ps(1) ETIME value ([[dd-]hh:]mm:ss) to seconds; -1 on failure."""
    try:
        raw = raw.strip()
        days = 0
        if "-" in raw:
            d, raw = raw.split("-", 1)
            days = int(d)
        fields = [int(f) for f in raw.split(":")]
        while len(fields) < 3:
            fields.insert(0, 0)
        h, m, s = fields
        return float(days * 86400 + h * 3600 + m * 60 + s)
    except (ValueError, IndexError):
        return -1.0


def _scan_leaked_jvms_ps(exclude: set[int]) -> list[dict]:
    """macOS sweep: list java processes via ps(1).

    macOS has no /proc; ps is the stdlib-only equivalent. Emits the
    same record shape as the /proc scan. Marker matching is
    case-insensitive like the Windows sweep, and for the same reason:
    the install path spells it `COMSOL64`, and with the `plugins/*`
    classpath wildcard no lowercase `com.comsol` jar name ever appears
    on the command line (validated live — a case-sensitive first cut
    missed a real SlotHarvester JVM whose only marker was the
    uppercase install path). Never raises; any failure returns [].
    """
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,etime=,rss=,args="],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=15,
        )
        lines = (result.stdout or "").splitlines()
    except Exception:  # noqa: BLE001 — the sweep must never raise
        return []

    out: list[dict] = []
    markers = _MARKERS_LOWER
    for line in lines:
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid_s, etime_s, rss_s, cmd = parts
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        if pid in exclude or pid == os.getpid():
            continue
        exe = cmd.split(None, 1)[0]
        if "java" not in os.path.basename(exe).lower():
            continue
        if not any(m in cmd.lower() for m in markers):
            continue
        try:
            rss = int(rss_s) * 1024  # ps reports RSS in KiB
        except ValueError:
            rss = -1
        out.append({
            "pid": pid,
            "age_s": _parse_etime(etime_s),
            "rss_bytes": rss,
            "cmdline_head": cmd[:200],
        })
    return out


def scan_leaked_jvms(proc_root: str | Path = "/proc",
                     exclude_pids: set[int] | None = None) -> list[dict]:
    """Best-effort sweep for pre-existing COMSOL JVMs.

    Returns one dict per match: {pid, age_s, rss_bytes, cmdline_head}.
    A JVM already running when a new harness launch starts is either a
    concurrent run (contention) or a leak — the campaign's leaked probe
    JVM survived ~2d19h at 3 GB RSS. Never raises; an unsupported
    platform or unreadable source returns [].

    On Windows the default sweep goes through PowerShell/CIM; on macOS
    (no /proc) it goes through ps(1). Passing an explicit ``proc_root``
    always uses the /proc-style tree scan (that keeps the fake-/proc
    unit tests meaningful on any platform).
    """
    exclude = exclude_pids or set()
    if str(proc_root) == "/proc":
        if os.name == "nt":
            return _scan_leaked_jvms_windows(exclude)
        if sys.platform == "darwin":
            return _scan_leaked_jvms_ps(exclude)

    out: list[dict] = []
    root = Path(proc_root)
    now = time.time()
    try:
        entries = list(root.iterdir())
    except OSError:
        return out
    for d in entries:
        if not d.name.isdigit():
            continue
        pid = int(d.name)
        if pid in exclude or pid == os.getpid():
            continue
        try:
            argv = (d / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if not argv or not argv[0]:
            continue
        exe = argv[0].decode(errors="replace")
        if "java" not in os.path.basename(exe):
            continue
        joined = b" ".join(argv).decode(errors="replace")
        # Case-insensitive, matching the Windows/macOS sweeps: a Linux
        # install at /opt/COMSOL64/... spells the marker in caps, and
        # with the plugins/* classpath wildcard the uppercase install
        # path can be the only marker on the command line.
        if not any(m in joined.lower() for m in _MARKERS_LOWER):
            continue
        rss = -1
        try:
            for line in (d / "status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024
                    break
        except (OSError, ValueError, IndexError):
            pass
        age_s = -1.0
        try:
            age_s = max(0.0, now - d.stat().st_mtime)
        except OSError:
            pass
        out.append({
            "pid": pid,
            "age_s": round(age_s, 1),
            "rss_bytes": rss,
            "cmdline_head": joined[:200],
        })
    return out


def warn_leaked_jvms(log: logging.Logger | None = None) -> list[dict]:
    """Scan and log a warning naming any pre-existing COMSOL JVMs."""
    log = log or logger
    leaks = scan_leaked_jvms()
    if leaks:
        heads = "; ".join(
            f"pid {x['pid']} (age {x['age_s']:.0f}s, "
            f"rss {x['rss_bytes'] / 1e9:.1f} GB)" if x["rss_bytes"] > 0
            else f"pid {x['pid']} (age {x['age_s']:.0f}s)"
            for x in leaks[:5])
        log.warning(
            "jvm_slot: %d pre-existing COMSOL JVM(s) detected: %s — "
            "each may hold a license seat; a leaked probe JVM once "
            "survived days unnoticed. Kill "
            "orphans or expect seat contention.",
            len(leaks), heads)
    return leaks
