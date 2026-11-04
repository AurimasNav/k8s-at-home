"""Hikvision stream encryption in recorded program streams, and undoing it.

With stream encryption on (Hik-Connect platform access) devices keep the program stream and
the NAL framing readable, but encrypt the start of some NAL units with AES-128-ECB. The key
is the device's verification code, filled up with zero bytes to 16. A unit is encrypted when
it begins in a video PES packet with the scrambling bits set; then the first 4096 bytes after
its header (1 byte H.264, 2 bytes H.265) are, in whole 16-byte blocks, so a short unit keeps
its last partial block in clear. Recordings keep this even after encryption is switched off.
"""

from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ENCRYPTED_PREFIX = 4096
BLOCK = 16
PACK_START = b"\x00\x00\x01\xba"
START_CODE = b"\x00\x00\x01"
PTS_WRAP = 1 << 33
_H264_PROFILES = frozenset({44, 66, 77, 83, 86, 88, 100, 110, 118, 122, 128, 144, 244})


def cipher_key(code: str) -> bytes:
    """AES key of a verification code."""
    return code.encode()[:BLOCK].ljust(BLOCK, b"\0")


def _plausible(codec: str, first: int, second: int | None) -> bool:
    """Whether bytes after a start code are a NAL header (ciphertext often is not)."""
    if first & 0x80:
        return False
    if codec == "h264":
        return 1 <= first & 0x1F <= 23
    if (first >> 1) & 0x3F > 40 or first & 0x01:
        return False
    return second is None or (second >> 3 == 0 and second & 0x07 != 0)


class ProgramStream:
    """Passes a program stream through, decrypting it when given a key.

    Data goes in with ``feed`` in pieces of any size and comes out in the same order; bytes
    are held back only until the packet (or encrypted block) they belong to is complete.
    Also tracks the playing time (from the video time stamps) of what came out so far, and,
    once ``key_frames`` is a list, collects where key frames start.
    """

    def __init__(self, key: bytes | None = None, codec: str | None = None) -> None:
        # ECB is what the devices use; this only reads their format.
        self._aes = Cipher(algorithms.AES(key), modes.ECB()) if key else None  # noqa: S305
        self.codec = codec
        self.key_fits: bool | None = None  # known once an encrypted parameter set was seen
        # (stream offset of the pack, playing time) of each key frame, when asked for.
        self.key_frames: list[tuple[int, float]] | None = None
        self._pack = 0  # stream offset of the latest pack header
        self._buffer = bytearray()
        self._base = 0  # stream offset of the first buffered byte
        self._next = 0  # stream offset of the next packet to parse
        self._first_pts: int | None = None
        self._pts = 0
        # The NAL unit being passed through.
        self._in_nal = False
        self._encrypted = False
        self._nal_type = 0
        self._skip = 0  # header bytes still to pass
        self._done = 0  # bytes of the encrypted prefix seen
        self._first_block = False
        self._pending: list[int] = []  # stream offsets of an incomplete block

    @property
    def seconds(self) -> float:
        """Playing time between the first and the latest video time stamp."""
        if self._first_pts is None:
            return 0.0
        return ((self._pts - self._first_pts) % PTS_WRAP) / 90000

    def feed(self, data: bytes) -> bytes:
        self._buffer += data
        self._parse()
        limit = min(self._pending[0], self._next) if self._pending else self._next
        count = limit - self._base
        out = bytes(self._buffer[:count])
        del self._buffer[:count]
        self._base = limit
        return out

    def flush(self) -> bytes:
        """Everything still held back, as it is (the stream ended)."""
        self._pending.clear()
        out = bytes(self._buffer)
        self._base += len(self._buffer)
        self._next = self._base
        self._buffer.clear()
        return out

    # ------------------------------------------------------------- packets

    def _parse(self) -> None:
        buffer = self._buffer
        while True:
            at = self._next - self._base
            if len(buffer) - at < 6:
                return
            if buffer[at : at + 3] != START_CODE or buffer[at + 3] < 0xB9:
                found = buffer.find(START_CODE, at + 1)
                while found >= 0 and found + 3 < len(buffer) and buffer[found + 3] < 0xB9:
                    found = buffer.find(START_CODE, found + 1)
                self._next = self._base + (found if found >= 0 else max(at, len(buffer) - 3))
                if found < 0 or found + 3 >= len(buffer):
                    return
                continue
            stream_id = buffer[at + 3]
            if stream_id == 0xB9:  # program end
                size = 4
            elif stream_id == 0xBA:
                if len(buffer) - at < 14:
                    return
                size = 14 + (buffer[at + 13] & 0x07) if buffer[at + 4] >> 6 == 1 else 12
            else:
                size = 6 + int.from_bytes(buffer[at + 4 : at + 6], "big")
            if len(buffer) - at < size:
                return
            if stream_id == 0xBA:
                self._pack = self._base + at
            if 0xE0 <= stream_id <= 0xEF and size > 9 and buffer[at + 6] >> 6 == 2:
                self._video(at, at + size)
            self._next += size

    def _video(self, at: int, end: int) -> None:
        buffer = self._buffer
        flags = buffer[at + 6]
        scrambled = bool(flags & 0x30)
        if scrambled and self._aes is not None:
            buffer[at + 6] = flags & 0xCF
        if buffer[at + 7] & 0x80 and at + 14 <= end:
            raw = buffer[at + 9 : at + 14]
            pts = (
                ((raw[0] >> 1) & 0x07) << 30
                | raw[1] << 22
                | (raw[2] >> 1) << 15
                | raw[3] << 7
                | raw[4] >> 1
            )
            if self._first_pts is None:
                self._first_pts = pts
            self._pts = pts
        position = min(at + 9 + buffer[at + 8], end)
        while position < end:
            found = self._next_unit(position, end)
            if found is None:
                self._body(position, end)
                return
            start, header = found
            self._body(position, start)
            self._open(header, scrambled)
            position = header

    def _next_unit(self, position: int, end: int) -> tuple[int, int] | None:
        """Start (with a leading zero byte) and header offset of the next NAL unit."""
        buffer = self._buffer
        search = position
        while (found := buffer.find(START_CODE, search, end)) >= 0:
            header = found + 3
            if header >= end:
                return None
            if self._in_encrypted_prefix():
                second = buffer[header + 1] if header + 1 < end else None
                if not _plausible(self.codec or "hevc", buffer[header], second):
                    search = found + 1
                    continue
            start = found - 1 if found > position and buffer[found - 1] == 0 else found
            return start, header
        return None

    def _in_encrypted_prefix(self) -> bool:
        return self._in_nal and self._encrypted and self._done < ENCRYPTED_PREFIX

    # ------------------------------------------------------------- NAL units

    def _open(self, header: int, scrambled: bool) -> None:
        first = self._buffer[header]
        if self.codec is None:
            self.codec = "hevc" if (first >> 1) & 0x3F in (32, 33, 34) else "h264"
        self._pending.clear()
        self._in_nal = True
        self._encrypted = scrambled and self._aes is not None
        self._nal_type = (first >> 1) & 0x3F if self.codec == "hevc" else first & 0x1F
        # Key frames start with their parameter sets (VPS, SPS).
        if self.key_frames is not None and self._nal_type == (32 if self.codec == "hevc" else 7):
            self.key_frames.append((self._pack, self.seconds))
        self._skip = 2 if self.codec == "hevc" else 1
        self._done = 0
        self._first_block = self._encrypted

    def _body(self, start: int, end: int) -> None:
        if not self._in_nal or start >= end:
            return
        if self._skip:
            taken = min(self._skip, end - start)
            self._skip -= taken
            start += taken
        if not self._encrypted or self._done >= ENCRYPTED_PREFIX:
            return
        end = min(end, start + ENCRYPTED_PREFIX - self._done)
        self._done += end - start
        while start < end:
            if not self._pending and end - start >= BLOCK:
                whole = (end - start) // BLOCK * BLOCK
                self._decrypt_run(start, start + whole)
                start += whole
                continue
            self._pending.append(self._base + start)
            start += 1
            if len(self._pending) == BLOCK:
                self._decrypt_scattered()

    def _decrypt_run(self, start: int, end: int) -> None:
        assert self._aes is not None
        plain = self._aes.decryptor().update(bytes(self._buffer[start:end]))
        self._buffer[start:end] = plain
        self._judge(plain[:BLOCK])

    def _decrypt_scattered(self) -> None:
        assert self._aes is not None
        offsets = [p - self._base for p in self._pending]
        plain = self._aes.decryptor().update(bytes(self._buffer[o] for o in offsets))
        for offset, value in zip(offsets, plain, strict=True):
            self._buffer[offset] = value
        self._pending.clear()
        self._judge(plain)

    def _judge(self, block: bytes) -> None:
        """Check the key on the first decrypted block of a parameter set."""
        if not self._first_block:
            return
        self._first_block = False
        if self.key_fits is not None:
            return
        if self.codec == "hevc" and self._nal_type == 32:
            self.key_fits = block[2:4] == b"\xff\xff"  # VPS reserved bits
        elif self.codec == "h264" and self._nal_type == 7:
            self.key_fits = block[0] in _H264_PROFILES
