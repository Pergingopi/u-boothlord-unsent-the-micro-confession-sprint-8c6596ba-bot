#!/usr/bin/env python3
"""Unsent: The Micro-Confession Sprint.

Writes one two-part micro-confession to $MAKEFUN_OUTPUT_DIR/entry.txt:
a "To:" line naming who the message was meant for, then the unsent
message itself, one line, exactly as it was typed, with no explanation
after it.

Gate (kit RULES.txt): entry.txt must be UTF-8 text of 1 to 260
characters, not only white space; the message line is at most
200 characters. This entry is 166 characters total including the
trailing newline (165 without it), 30 words; the message line is
142 characters. The gate is enforced twice: once on the in-memory
entry before any write, and once on the bytes actually read back
from disk, so a bad entry can never reach the output file.

Write path is atomic: the entry goes to a uniquely named temporary
file in the output directory, is fsynced, then renamed over
entry.txt, and the directory is fsynced. A failed run removes its
own temporary file; stale temporary files left by an earlier
crashed run (older than one hour) are swept at startup. Output
files are mode 0644.

Python 3.13 standard library only. Deterministic: the same bytes
every run.
"""

import os
import sys
import tempfile
import time

ENTRY = (
    "To: my brother's widow\n"
    "I kept his last voicemail for three years and I still cannot "
    "decide whether deleting it would be letting him go or finally "
    "letting you grieve.\n"
)

MAX_TOTAL_CHARS = 260
MAX_MESSAGE_CHARS = 200
TMP_PREFIX = ".entry-"
TMP_SUFFIX = ".tmp"
STALE_TMP_SECONDS = 3600


class GateError(ValueError):
    """The entry (or what landed on disk) fails the competition gate."""


# Characters that would break the "one line, exactly as typed" shape
# even though they are not ASCII control characters: carriage return,
# DEL, and the Unicode line/paragraph separators.
FORBIDDEN_CHARS = {"\r": "U+000D", "\x7f": "U+007F",
                   "\u2028": "U+2028", "\u2029": "U+2029"}


def validate(text: str) -> None:
    """Check the entry against the competition gate before writing.

    Raises GateError on the first failure, naming the rule that
    failed so a bad entry is diagnosable without guessing.
    """
    if not isinstance(text, str):
        raise GateError("entry is not text")
    if not text or not text.strip():
        raise GateError("entry is empty or only white space")
    if len(text) > MAX_TOTAL_CHARS:
        raise GateError(
            "entry is %d characters, over the %d limit"
            % (len(text), MAX_TOTAL_CHARS)
        )
    # Exactly two content lines plus one trailing newline: no blank
    # line at the end, no missing newline, no third line.
    lines = text.split("\n")
    if len(lines) != 3 or lines[2] != "":
        raise GateError(
            "entry must be exactly two lines plus one trailing newline"
        )
    to_line, message = lines[0], lines[1]

    # No hidden line breaks or control characters anywhere.
    for ch in text:
        if ord(ch) < 0x20 and ch != "\n":
            raise GateError(
                "entry contains a control character U+%04X" % ord(ch)
            )
        if ch in FORBIDDEN_CHARS:
            raise GateError(
                "entry contains %s, which would break the one-line shape"
                % FORBIDDEN_CHARS[ch]
            )

    # The To: line must name someone: "To:" alone, or a recipient
    # that is only spaces, does not count.
    if not to_line.startswith("To: "):
        raise GateError("first line must start with 'To: '")
    if not to_line[4:].strip():
        raise GateError("first line must name a recipient after 'To: '")
    if to_line != to_line.rstrip():
        raise GateError("first line has trailing white space")

    # The message must be a real line, not blank, and within its cap.
    if not message.strip():
        raise GateError("message line is empty or only white space")
    if len(message) > MAX_MESSAGE_CHARS:
        raise GateError(
            "message line is %d characters, over the %d limit"
            % (len(message), MAX_MESSAGE_CHARS)
        )
    if message != message.rstrip():
        raise GateError("message line has trailing white space")


def resolve_output_dir() -> str:
    """Resolve the output directory from the environment.

    An unset, empty, or white-space-only MAKEFUN_OUTPUT_DIR falls
    back to /output. A value that exists but is not a directory is
    an error, reported plainly instead of failing later inside the
    write.
    """
    raw = os.environ.get("MAKEFUN_OUTPUT_DIR")
    out_dir = (raw or "").strip() or "/output"
    out_dir = os.path.expanduser(out_dir)
    if os.path.lexists(out_dir) and not os.path.isdir(out_dir):
        raise GateError(
            "MAKEFUN_OUTPUT_DIR %r exists and is not a directory" % out_dir
        )
    return out_dir


def cleanup_stale_tmp(out_dir: str) -> None:
    """Remove our own stale temporary files from earlier crashed runs.

    Only files this entry creates (prefix .entry-, suffix .tmp) and
    only ones older than STALE_TMP_SECONDS, so a concurrent run's
    fresh temporary file is never touched. Best effort: any failure
    here must not stop the entry itself.
    """
    now = time.time()
    try:
        names = os.listdir(out_dir)
    except OSError:
        return
    for name in names:
        if not (name.startswith(TMP_PREFIX) and name.endswith(TMP_SUFFIX)):
            continue
        path = os.path.join(out_dir, name)
        try:
            st = os.lstat(path)
            if st.st_mtime < now - STALE_TMP_SECONDS:
                os.unlink(path)
        except OSError:
            continue


def atomic_write(out_dir: str, data: bytes) -> str:
    """Write data to entry.txt atomically, mode 0644, and return its path."""
    fd, tmp_path = tempfile.mkstemp(
        dir=out_dir, prefix=TMP_PREFIX, suffix=TMP_SUFFIX
    )
    try:
        os.chmod(tmp_path, 0o644)
        try:
            fh = os.fdopen(fd, "wb")
        except BaseException:
            os.close(fd)
            raise
        with fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_path, 0o644)
        target = os.path.join(out_dir, "entry.txt")
        os.replace(tmp_path, target)
        os.chmod(target, 0o644)
        dir_fd = os.open(out_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return target
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def main() -> int:
    try:
        validate(ENTRY)
        out_dir = resolve_output_dir()
        os.makedirs(out_dir, exist_ok=True)
        cleanup_stale_tmp(out_dir)

        data = ENTRY.encode("utf-8")
        target = atomic_write(out_dir, data)

        # Read back and re-validate what actually landed on disk.
        with open(target, "rb") as fh:
            on_disk = fh.read()
        if on_disk != data:
            raise GateError(
                "read-back mismatch: entry.txt differs from the entry"
            )
        validate(on_disk.decode("utf-8"))
    except (GateError, OSError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print("wrote %s (%d bytes)" % (target, len(data)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
