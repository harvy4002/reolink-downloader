"""
Standalone Baichuan (port 9000) VOD download client.

Reolink's documented HTTPS ``cmd=Download`` API is bottlenecked by the camera's
TLS implementation (~800 KB/s). The native apps instead pull recordings over the
proprietary binary "Baichuan" protocol on TCP port 9000, which is several times
faster. This module implements just enough of that protocol to download a clip.

The full protocol is documented in ``PROTOCOL.md`` at the repo root. In short:
login (BC-obfuscated handshake → AES session key), then send a ``cmd 143`` request
keyed by channel + stream type + time range, and read back a stream of ``cmd 143``
media messages whose payloads de-frame into an H.264/H.265 Annex-B elementary
stream.

This is self-contained (only depends on pycryptodome) so it can later be lifted
into ``reolink_aio`` upstream without untangling it from this tool.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import struct
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import md5
from pathlib import Path

from Cryptodome.Cipher import AES

# --- protocol constants (see PROTOCOL.md) ---
MAGIC = bytes.fromhex("f0debc0a")
DEFAULT_PORT = 9000
AES_IV = b"0123456789abcdef"
XML_KEY = (0x1F, 0x2D, 0x3C, 0x4B, 0x5A, 0x69, 0x78, 0xFF)

# The camera closing the TCP connection is handled gracefully (see download()'s
# except clause below); these bound how long we'll wait for it to say anything
# at all, e.g. if it silently stops responding under load (several concurrent
# download sessions can apparently exceed what some NVRs handle cleanly).
CONNECT_TIMEOUT_SECONDS = 15.0
READ_TIMEOUT_SECONDS = 30.0

# A request/response correlation id we put in the download request; the camera
# echoes it on every media message. The value is arbitrary.
MEDIA_MESS_ID = 54

# BCMEDIA frame magics (little-endian u32)
_M_INFO_V1, _M_INFO_V2 = 0x31303031, 0x32303031
_M_IFRAME, _M_IFRAME_LAST = 0x63643030, 0x63643039
_M_PFRAME, _M_PFRAME_LAST = 0x63643130, 0x63643139
_M_AAC, _M_ADPCM = 0x62773530, 0x62773130

_ENCLEN_RE = re.compile(rb"<[eE]ncryptLen>(\d+)</[eE]ncryptLen>")
_NONCE_RE = re.compile(r"<nonce>([^<]+)</nonce>")

_LOGIN_XML = (
    '<?xml version="1.0" encoding="UTF-8" ?>\n'
    "<body>\n"
    '<LoginUser version="1.1">\n'
    "<userName>{user}</userName>\n"
    "<password>{password}</password>\n"
    "<userVer>1</userVer>\n"
    "</LoginUser>\n"
    '<LoginNet version="1.1">\n'
    "<type>LAN</type>\n"
    "<udpPort>0</udpPort>\n"
    "</LoginNet>\n"
    "</body>\n"
)

_DOWNLOAD_XML = (
    '<?xml version="1.0" encoding="UTF-8" ?>\n'
    "<body>\n"
    '<FileInfoList version="1.1">\n'
    "<FileInfo>\n"
    "<logicChnBitmap>{logic_bitmap}</logicChnBitmap>\n"
    "<channelId>{channel}</channelId>\n"
    "<supportSub>0</supportSub>\n"
    "<streamType>{stream_type}</streamType>\n"
    "<startTime>{start}</startTime>\n"
    "<endTime>{end}</endTime>\n"
    "</FileInfo>\n"
    "</FileInfoList>\n"
    "</body>\n"
)


class BaichuanError(Exception):
    """A Baichuan protocol or connection error."""


def _u32(b: bytes) -> int:
    return struct.unpack("<I", b)[0]


def _md5_modern(s: str) -> str:
    """The MD5 variant Baichuan uses for nonce/key/login hashes."""
    return md5(s.encode("utf8")).hexdigest()[:31].upper()


def _bc_crypt(buf: bytes, offset: int) -> bytes:
    """Keyless BC XOR cipher (symmetric); ``offset`` is the message's mess_id."""
    o = offset % 256
    return bytes((b ^ XML_KEY[(o + i) % 8] ^ o) & 0xFF for i, b in enumerate(buf))


def _aes(key: bytes):
    # A fresh CFB-128 cipher per segment, fixed IV (each segment restarts at IV).
    return AES.new(key=key, mode=AES.MODE_CFB, iv=AES_IV, segment_size=128)


def _reolink_time(dt: datetime) -> str:
    return (
        f"<year>{dt.year}</year><month>{dt.month}</month><day>{dt.day}</day>"
        f"<hour>{dt.hour}</hour><minute>{dt.minute}</minute><second>{dt.second}</second>"
    )


def sniff_codec(vbuf: bytes) -> str:
    """Detect h264 vs h265 from the Annex-B NAL stream.

    Reolink's BCMEDIA ``video_type`` field is unreliable (a TrackMix reports
    "H264" while sending HEVC), so read the bitstream itself.
    """
    i = checked = 0
    while i < len(vbuf) - 5 and checked < 16:
        if vbuf[i : i + 4] == b"\x00\x00\x00\x01" or vbuf[i : i + 3] == b"\x00\x00\x01":
            off = 4 if vbuf[i : i + 4] == b"\x00\x00\x00\x01" else 3
            b = vbuf[i + off]
            if (b >> 1) & 0x3F in (32, 33, 34):  # HEVC VPS/SPS/PPS
                return "h265"
            if b & 0x1F == 7:  # H.264 SPS
                return "h264"
            i += off + 1
            checked += 1
        else:
            i += 1
    return "h264"


def _pad8(n: int) -> int:
    return (8 - n % 8) % 8


def deframe_video(stream: bytes) -> tuple[bytearray, dict | None]:
    """Parse a BCMEDIA byte stream; return (annex_b_video, info_dict)."""
    vbuf = bytearray()
    info: dict | None = None
    p, n = 0, len(stream)
    while p + 8 <= n:
        magic = _u32(stream[p : p + 4])
        if magic in (_M_INFO_V1, _M_INFO_V2):
            hs = _u32(stream[p + 4 : p + 8])
            info = {
                "width": _u32(stream[p + 8 : p + 12]),
                "height": _u32(stream[p + 12 : p + 16]),
                "fps": stream[p + 17],
            }
            p += hs  # header_size is the total info-frame size, incl. magic
        elif _M_IFRAME <= magic <= _M_PFRAME_LAST:
            payload_size = _u32(stream[p + 8 : p + 12])
            add_hdr = _u32(stream[p + 12 : p + 16])
            start = p + 24 + add_hdr
            data = stream[start : start + payload_size]
            if len(data) < payload_size:
                break  # truncated; wait for more data
            vbuf += data
            p = start + payload_size + _pad8(payload_size)
        elif magic in (_M_AAC, _M_ADPCM):
            payload_size = struct.unpack("<H", stream[p + 4 : p + 6])[0]
            start = p + 8
            if start + payload_size > n:
                break
            p = start + payload_size + _pad8(payload_size)
        else:
            break  # unknown magic: stop (caller decides how much was consumed)
    return vbuf, info


@dataclass
class BaichuanDownloader:
    """Minimal async Baichuan client: connect → login → download one clip."""

    host: str
    username: str
    password: str
    port: int = DEFAULT_PORT
    debug: bool = False

    _reader: asyncio.StreamReader | None = field(default=None, repr=False)
    _writer: asyncio.StreamWriter | None = field(default=None, repr=False)
    _key: bytes | None = field(default=None, repr=False)

    def _dbg(self, msg: str) -> None:
        if self.debug:
            print(f"  [bc] {msg}")

    def _decrypt_control(self, body: bytes, mess_id: int, poff: int) -> str:
        """Best-effort decrypt of a control body for debugging (AES, else BC)."""
        if not body or self._key is None:
            return ""
        try:
            seg = body[:poff] if poff else body
            txt = _aes(self._key).decrypt(seg).decode("utf8", "replace")
            if "<" in txt:
                return " ".join(txt.split())
        except Exception:
            pass
        return " ".join(_bc_crypt(body, mess_id).decode("utf8", "replace").split())

    async def __aenter__(self) -> "BaichuanDownloader":
        await self.connect()
        await self.login()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def connect(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=CONNECT_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError as e:
            raise BaichuanError(
                f"could not connect to {self.host}:{self.port} within {CONNECT_TIMEOUT_SECONDS:.0f}s"
            ) from e

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = self._reader = None

    # -- framing --------------------------------------------------------------

    def _build(self, cmd_id: int, body: str, *, mess_id: int, message_class: str, aes: bool) -> bytes:
        body_bytes = body.encode("utf8")
        if aes:
            assert self._key is not None
            enc = _aes(self._key).encrypt(body_bytes) if body_bytes else b""
        else:
            enc = _bc_crypt(body_bytes, mess_id)
        header = MAGIC + struct.pack("<III", cmd_id, len(enc), mess_id)
        if message_class == "1465":  # 20-byte handshake header
            header += bytes.fromhex("12dc1465")
        elif message_class == "1464":  # 24-byte header, payload_offset = 0
            header += bytes.fromhex("00001464") + struct.pack("<I", 0)
        else:
            raise BaichuanError(f"unsupported message_class {message_class}")
        return header + enc

    async def _send(self, *args, **kwargs) -> None:
        assert self._writer is not None
        self._writer.write(self._build(*args, **kwargs))
        await self._writer.drain()

    async def _read_message(self) -> tuple[int, int, int, str, int, bytes]:
        """Read one full BC message, bounded by READ_TIMEOUT_SECONDS of camera
        silence. Without this, a stalled connection (e.g. the NVR not
        responding to one of several simultaneous download requests) hangs
        forever instead of raising something auto-recovery can retry."""
        try:
            return await asyncio.wait_for(self._read_message_raw(), timeout=READ_TIMEOUT_SECONDS)
        except asyncio.TimeoutError as e:
            raise BaichuanError(f"no response from camera for {READ_TIMEOUT_SECONDS:.0f}s") from e

    async def _read_message_raw(self) -> tuple[int, int, int, str, int, bytes]:
        """Read one full BC message: (cmd_id, mess_id, status, class, poff, body)."""
        assert self._reader is not None
        header = await self._reader.readexactly(20)
        if header[:4] != MAGIC:
            raise BaichuanError(f"bad magic {header[:4].hex()}")
        cmd_id, mess_len, mess_id = struct.unpack("<III", header[4:16])
        status = struct.unpack("<H", header[16:18])[0]
        mclass = header[18:20].hex()
        poff = 0
        if mclass in ("1464", "0000"):  # 24-byte header
            poff = _u32(await self._reader.readexactly(4))
        body = await self._reader.readexactly(mess_len) if mess_len else b""
        return cmd_id, mess_id, status, mclass, poff, body

    # -- login ----------------------------------------------------------------

    async def login(self) -> None:
        # 1. request the nonce (empty BC message)
        await self._send(1, "", mess_id=0, message_class="1465", aes=False)
        nonce = None
        for _ in range(8):
            cmd_id, mess_id, status, mclass, poff, body = await self._read_message()
            if cmd_id == 1 and body:
                m = _NONCE_RE.search(_bc_crypt(body, mess_id).decode("utf8", "replace"))
                if m:
                    nonce = m.group(1)
                    break
        if not nonce:
            raise BaichuanError("did not receive a login nonce")
        self._dbg(f"nonce={nonce}")

        self._key = _md5_modern(f"{nonce}-{self.password}")[:16].encode("utf8")

        # 2. send the login (still BC-encrypted)
        xml = _LOGIN_XML.format(
            user=_md5_modern(f"{self.username}{nonce}"),
            password=_md5_modern(f"{self.password}{nonce}"),
        )
        await self._send(1, xml, mess_id=0, message_class="1464", aes=False)

        # 3. await the login response (status 200 == success)
        for _ in range(8):
            cmd_id, mess_id, status, mclass, poff, body = await self._read_message()
            self._dbg(f"login recv cmd={cmd_id} status={status} class={mclass} len={len(body)}")
            if cmd_id == 1:
                if status not in (0, 200):
                    raise BaichuanError(f"login failed, status {status}")
                self._dbg("login OK")
                return
        raise BaichuanError("no login response")

    # -- download -------------------------------------------------------------

    async def download(
        self,
        out_path: Path,
        *,
        start: datetime,
        end: datetime,
        channel: int = 0,
        stream_type: str = "mainStream",
        logic_bitmap: int = 255,
        remux_mp4: bool = True,
        total_size: int | None = None,
        on_progress: Callable[[int, int | None], None] | None = None,
    ) -> Path:
        """Download the recording covering ``start``–``end`` to ``out_path``.

        Writes a raw ``.h264``/``.h265`` elementary stream, then remuxes to
        ``.mp4`` with ffmpeg when available (and ``remux_mp4``). Returns the path
        actually written.

        ``on_progress``, if given, is called as ``on_progress(bytes_so_far,
        total_size)`` after each media message is received (``total_size`` is
        whatever the caller passed in — usually the on-camera file size — and
        may be ``None`` if unknown). Exceptions raised by the callback are
        swallowed so a logging bug can never abort an in-progress download.
        """
        if self._key is None:
            raise BaichuanError("not logged in")

        req = _DOWNLOAD_XML.format(
            logic_bitmap=logic_bitmap,
            channel=channel,
            stream_type=stream_type,
            start=_reolink_time(start),
            end=_reolink_time(end),
        )
        self._dbg(f"download request: channel={channel} stream={stream_type} "
                  f"bitmap={logic_bitmap} {start} - {end}")
        await self._send(143, req, mess_id=MEDIA_MESS_ID, message_class="1464", aes=True)

        media = bytearray()
        nmsg = 0
        while True:
            try:
                cmd_id, mess_id, status, mclass, poff, body = await self._read_message()
            except (asyncio.IncompleteReadError, ConnectionError) as e:
                self._dbg(f"stream closed after {nmsg} media msgs ({type(e).__name__})")
                break  # camera closed the stream; decode what we have
            if cmd_id != 143:
                # Surface any error/control reply (e.g. status 400) for debugging.
                if self.debug and (status not in (0, 200) or body):
                    self._dbg(f"recv cmd={cmd_id} status={status} class={mclass} "
                              f"len={len(body)} :: {self._decrypt_control(body, mess_id, poff)[:200]}")
                continue
            if not body:
                self._dbg(f"end-of-stream marker (cmd143 len=0) after {nmsg} media msgs")
                break  # zero-length cmd 143 marks end of stream
            if poff == 0:
                # An AES-encrypted XML control message (status/info), not media.
                if self.debug:
                    self._dbg(f"control cmd143 len={len(body)} :: "
                              f"{self._decrypt_control(body, mess_id, 0)[:120]}")
                continue
            chunk = self._decrypt_media_body(body, poff)
            if nmsg == 0:
                self._dbg(f"first media chunk: poff={poff} len={len(body)} "
                          f"decoded_head={chunk[:8].hex()}")
            media += chunk
            nmsg += 1
            if on_progress is not None:
                try:
                    on_progress(len(media), total_size)
                except Exception:
                    pass
        self._dbg(f"collected {len(media)} media bytes from {nmsg} media messages")

        video, info = deframe_video(bytes(media))
        if not video:
            raise BaichuanError("no video frames decoded (wrong time range or password?)")

        codec = sniff_codec(bytes(video))
        self._dbg(f"decoded {codec} {info} video_bytes={len(video)}")
        raw_path = out_path.with_suffix(f".{codec}")
        raw_path.write_bytes(video)

        if remux_mp4 and shutil.which("ffmpeg"):
            mp4_path = out_path.with_suffix(".mp4")
            fps = info.get("fps") if info else None
            if codec == "h265":
                self._dbg("transcoding h265 -> h264 for broad player/thumbnail compatibility")
            if _remux_to_mp4(raw_path, mp4_path, codec=codec, fps=fps):
                raw_path.unlink(missing_ok=True)
                return mp4_path
        return raw_path

    def _decrypt_media_body(self, body: bytes, poff: int) -> bytes:
        """A media body is [AES Extension XML][payload]; only the first
        <encryptLen> bytes of the payload are AES-encrypted."""
        assert self._key is not None
        enc_len = 0
        if poff:
            ext = _aes(self._key).decrypt(body[:poff])
            m = _ENCLEN_RE.search(ext)
            enc_len = int(m.group(1)) if m else 0
        payload = body[poff:]
        enc_len = min(enc_len, len(payload))
        head = _aes(self._key).decrypt(payload[:enc_len]) if enc_len else b""
        return head + payload[enc_len:]


def _remux_to_mp4(raw_path: Path, mp4_path: Path, *, codec: str = "h264", fps: int | None = None) -> bool:
    """Write a raw elementary stream into MP4 with ffmpeg.

    A raw Annex-B stream has no container timing, so we pass the real frame rate
    (from the BCMEDIA InfoFrame) as the input rate.

    H.264 sources are remuxed losslessly (``-c copy``, no re-encode). H.265
    sources are transcoded to H.264 instead: web-based previews/thumbnails
    (e.g. Synology's Video Station/Photos) commonly can't decode HEVC, while
    H.264 plays everywhere, so the extra CPU time buys universal playback.
    """
    cmd = ["ffmpeg", "-y"]
    if fps:
        cmd += ["-r", str(fps)]
    cmd += ["-i", str(raw_path)]
    if codec == "h265":
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    else:
        cmd += ["-c", "copy"]
    cmd += ["-movflags", "+faststart", str(mp4_path)]
    try:
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result.returncode == 0 and mp4_path.exists()
    except Exception:
        return False
