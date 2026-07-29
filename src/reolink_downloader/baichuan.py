"""Standalone Baichuan (port 9000) VOD download client."""

from __future__ import annotations

import asyncio
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import md5
from pathlib import Path

from Cryptodome.Cipher import AES

# Protocol constants
MAGIC = bytes.fromhex("f0debc0a")
DEFAULT_PORT = 9000
AES_IV = b"0123456789abcdef"
XML_KEY = (0x1F, 0x2D, 0x3C, 0x4B, 0x5A, 0x69, 0x78, 0xFF)

CMD_LOGIN = 1
CMD_DOWNLOAD = 143
MEDIA_MESS_ID = 54

MSG_CLASS_HANDSHAKE = "1465"
MSG_CLASS_DATA = "1464"

# BCMEDIA frame magics (little-endian u32)
_M_INFO_V1, _M_INFO_V2 = 0x31303031, 0x32303031
_M_IFRAME, _M_IFRAME_LAST = 0x63643030, 0x63643039
_M_PFRAME, _M_PFRAME_LAST = 0x63643130, 0x63643139
_M_AAC, _M_ADPCM = 0x62773530, 0x62773130

_ENCLEN_RE = re.compile(rb"<[eE]ncryptLen>(\d+)</[eE]ncryptLen>")
_NONCE_RE = re.compile(r"<nonce>([^<]+)</nonce>")

_LOGIN_XML = (
    '<?xml version="1.0" encoding="UTF-8" ?>\n<body>\n'
    '<LoginUser version="1.1">\n<userName>{user}</userName>\n'
    "<password>{password}</password>\n<userVer>1</userVer>\n</LoginUser>\n"
    '<LoginNet version="1.1">\n<type>LAN</type>\n<udpPort>0</udpPort>\n</LoginNet>\n</body>\n'
)

_DOWNLOAD_XML = (
    '<?xml version="1.0" encoding="UTF-8" ?>\n<body>\n'
    '<FileInfoList version="1.1">\n<FileInfo>\n'
    "<logicChnBitmap>{logic_bitmap}</logicChnBitmap>\n<channelId>{channel}</channelId>\n"
    "<supportSub>0</supportSub>\n<streamType>{stream_type}</streamType>\n"
    "<startTime>{start}</startTime>\n<endTime>{end}</endTime>\n"
    "</FileInfo>\n</FileInfoList>\n</body>\n"
)


class BaichuanError(Exception):
    """A Baichuan protocol or connection error."""


@dataclass
class StreamInfo:
    """Parsed BCMEDIA stream dimensions and frame counters."""

    width: int = 0
    height: int = 0
    nominal_fps: int = 0
    video_frames: int = 0
    aac_frames: int = 0
    adpcm_frames: int = 0


def _u32(b: bytes) -> int:
    return struct.unpack("<I", b)[0]


def _pad8(n: int) -> int:
    return (8 - n % 8) % 8


def _md5_modern(s: str) -> str:
    return md5(s.encode("utf8")).hexdigest()[:31].upper()


def _bc_crypt(buf: bytes, offset: int) -> bytes:
    o = offset % 256
    return bytes((b ^ XML_KEY[(o + i) % 8] ^ o) & 0xFF for i, b in enumerate(buf))


def _aes(key: bytes):
    return AES.new(key=key, mode=AES.MODE_CFB, iv=AES_IV, segment_size=128)


def _reolink_time(dt: datetime) -> str:
    return (
        f"<year>{dt.year}</year><month>{dt.month}</month><day>{dt.day}</day>"
        f"<hour>{dt.hour}</hour><minute>{dt.minute}</minute><second>{dt.second}</second>"
    )


def sniff_codec(vbuf: bytes) -> str:
    """Detect H.264 vs H.265 from Annex-B NAL headers."""
    for match in re.finditer(rb"\x00\x00\x00?\x01(.)", vbuf[:100000]):
        nal = match.group(1)[0]
        if (nal >> 1) & 0x3F in (32, 33, 34):
            return "h265"
        if nal & 0x1F == 7:
            return "h264"
    return "h264"


def deframe_video(stream: bytes) -> tuple[bytearray, bytearray, StreamInfo]:
    """Parse BCMEDIA stream into raw video, audio, and stream metadata."""
    video_buf, audio_buf = bytearray(), bytearray()
    info = StreamInfo()
    offset, length = 0, len(stream)

    while offset + 8 <= length:
        magic = _u32(stream[offset : offset + 4])
        if magic in (_M_INFO_V1, _M_INFO_V2):
            header_size = _u32(stream[offset + 4 : offset + 8])
            info.width = _u32(stream[offset + 8 : offset + 12])
            info.height = _u32(stream[offset + 12 : offset + 16])
            info.nominal_fps = stream[offset + 17]
            offset += header_size
        elif _M_IFRAME <= magic <= _M_PFRAME_LAST:
            payload_size = _u32(stream[offset + 8 : offset + 12])
            start = offset + 24 + _u32(stream[offset + 12 : offset + 16])
            if start + payload_size > length:
                break
            video_buf += stream[start : start + payload_size]
            info.video_frames += 1
            offset = start + payload_size + _pad8(payload_size)
        elif magic in (_M_AAC, _M_ADPCM):
            payload_size = struct.unpack("<H", stream[offset + 4 : offset + 6])[0]
            start = offset + 8
            if start + payload_size > length:
                break
            audio_buf += stream[start : start + payload_size]
            if magic == _M_AAC:
                info.aac_frames += 1
            else:
                info.adpcm_frames += 1
            offset = start + payload_size + _pad8(payload_size)
        else:
            break

    return video_buf, audio_buf, info


def _get_media_duration(path: Path) -> float | None:
    """Query exact stream or format duration using ffprobe."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "stream=duration:format=duration",
        "-of", "default=noprintwrappers=1:nokey=1",
        str(path),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                val = line.strip()
                if val and val != "N/A":
                    return float(val)
    except Exception:
        pass
    return None


def _estimate_audio_duration(aac_frames: int, audio_bytes: bytes) -> float | None:
    """Calculate exact AAC audio duration from frame count and header sample rate."""
    if not aac_frames:
        return None
    sample_rate = 16000
    if len(audio_bytes) >= 7 and audio_bytes[0] == 0xFF and (audio_bytes[1] & 0xF0) == 0xF0:
        sr_lut = {
            0: 96000, 1: 88200, 2: 64000, 3: 48000, 4: 44100,
            5: 32000, 6: 24000, 7: 22050, 8: 16000, 9: 12000,
            10: 11025, 11: 8000, 12: 7350,
        }
        sample_rate = sr_lut.get((audio_bytes[2] >> 2) & 0x0F, 16000)
    return (aac_frames * 1024) / float(sample_rate)


def _remux_to_mp4(
    raw_path: Path,
    mp4_path: Path,
    *,
    codec: str = "h264",
    fps: float | None = None,
    audio_path: Path | None = None,
    debug: bool = False,
) -> bool:
    """Remux video and optional audio into MP4 with FFmpeg."""
    has_audio = bool(audio_path and audio_path.exists())
    input_format = "hevc" if codec in ("h265", "hevc") else "h264"

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-f", input_format]
    if fps and fps > 0:
        cmd += ["-r", f"{fps:.6f}"]
    cmd += ["-i", str(raw_path)]

    if has_audio:
        cmd += ["-i", str(audio_path)]

    cmd += ["-c:v", "copy"]
    if has_audio:
        cmd += ["-c:a", "copy", "-map", "0:v", "-map", "1:a"]

    if codec in ("h265", "hevc"):
        cmd += ["-tag:v", "hvc1"]

    cmd += ["-movflags", "+faststart", str(mp4_path)]

    if debug:
        print(f"  [bc] ffmpeg command: {' '.join(cmd)}")

    try:
        res = subprocess.run(
            cmd,
            stdout=None if debug else subprocess.DEVNULL,
            stderr=None if debug else subprocess.DEVNULL,
            timeout=300,
        )
        return res.returncode == 0 and mp4_path.exists()
    except Exception as e:
        if debug:
            print(f"  [bc] ffmpeg exception: {type(e).__name__}: {e}")
        return False


@dataclass
class BaichuanDownloader:
    """Async Baichuan client: connect -> login -> download."""

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

    def _decrypt_control(self, body: bytes, mess_id: int, offset: int = 0) -> str:
        if not body or not self._key:
            return ""
        try:
            raw = _aes(self._key).decrypt(body[:offset] if offset else body)
        except Exception:
            raw = _bc_crypt(body, mess_id)
        return " ".join(raw.decode("utf8", "replace").split())

    async def __aenter__(self) -> BaichuanDownloader:
        await self.connect()
        await self.login()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = self._reader = None

    def _build(self, cmd_id: int, body: str, *, mess_id: int, message_class: str, aes: bool) -> bytes:
        payload = body.encode("utf8")
        enc = _aes(self._key).encrypt(payload) if aes and self._key else _bc_crypt(payload, mess_id)
        header = MAGIC + struct.pack("<III", cmd_id, len(enc), mess_id)
        if message_class == MSG_CLASS_HANDSHAKE:
            header += bytes.fromhex("12dc1465")
        elif message_class == MSG_CLASS_DATA:
            header += bytes.fromhex("0000146400000000")
        else:
            raise BaichuanError(f"unsupported message_class {message_class}")
        return header + enc

    async def _send(self, *args, **kwargs) -> None:
        assert self._writer is not None
        self._writer.write(self._build(*args, **kwargs))
        await self._writer.drain()

    async def _read_message(self) -> tuple[int, int, int, str, int, bytes]:
        assert self._reader is not None
        header = await self._reader.readexactly(20)
        if header[:4] != MAGIC:
            raise BaichuanError(f"bad magic {header[:4].hex()}")

        cmd_id, mess_len, mess_id = struct.unpack("<III", header[4:16])
        status = struct.unpack("<H", header[16:18])[0]
        mclass = header[18:20].hex()
        offset = _u32(await self._reader.readexactly(4)) if mclass in (MSG_CLASS_DATA, "0000") else 0
        body = await self._reader.readexactly(mess_len) if mess_len else b""
        return cmd_id, mess_id, status, mclass, offset, body

    async def login(self) -> None:
        await self._send(CMD_LOGIN, "", mess_id=0, message_class=MSG_CLASS_HANDSHAKE, aes=False)
        nonce = None
        for _ in range(8):
            cmd_id, _, _, _, _, body = await self._read_message()
            if cmd_id == CMD_LOGIN and body:
                m = _NONCE_RE.search(_bc_crypt(body, 0).decode("utf8", "replace"))
                if m:
                    nonce = m.group(1)
                    break
        if not nonce:
            raise BaichuanError("did not receive a login nonce")

        self._dbg(f"nonce={nonce}")
        self._key = _md5_modern(f"{nonce}-{self.password}")[:16].encode("utf8")

        xml = _LOGIN_XML.format(
            user=_md5_modern(f"{self.username}{nonce}"),
            password=_md5_modern(f"{self.password}{nonce}"),
        )
        await self._send(CMD_LOGIN, xml, mess_id=0, message_class=MSG_CLASS_DATA, aes=False)

        for _ in range(8):
            cmd_id, _, status, _, _, _ = await self._read_message()
            if cmd_id == CMD_LOGIN:
                if status not in (0, 200):
                    raise BaichuanError(f"login failed, status {status}")
                self._dbg("login OK")
                return
        raise BaichuanError("no login response")

    def _decrypt_media_body(self, body: bytes, offset: int) -> bytes:
        assert self._key is not None
        enc_len = 0
        if offset:
            ext = _aes(self._key).decrypt(body[:offset])
            m = _ENCLEN_RE.search(ext)
            enc_len = int(m.group(1)) if m else 0
        payload = body[offset:]
        enc_len = min(enc_len, len(payload))
        head = _aes(self._key).decrypt(payload[:enc_len]) if enc_len else b""
        return head + payload[enc_len:]

    async def _fetch_media_stream(
        self,
        start: datetime,
        end: datetime,
        channel: int,
        stream_type: str,
        logic_bitmap: int,
    ) -> bytearray:
        req = _DOWNLOAD_XML.format(
            logic_bitmap=logic_bitmap,
            channel=channel,
            stream_type=stream_type,
            start=_reolink_time(start),
            end=_reolink_time(end),
        )
        self._dbg(f"download request: channel={channel} stream={stream_type} bitmap={logic_bitmap} {start} - {end}")
        await self._send(CMD_DOWNLOAD, req, mess_id=MEDIA_MESS_ID, message_class=MSG_CLASS_DATA, aes=True)

        media = bytearray()
        msg_count = 0

        while True:
            try:
                cmd_id, mess_id, status, _, offset, body = await self._read_message()
            except (asyncio.IncompleteReadError, ConnectionError) as e:
                self._dbg(f"stream closed after {msg_count} msgs ({type(e).__name__})")
                break

            if cmd_id != CMD_DOWNLOAD or not body:
                if not body and cmd_id == CMD_DOWNLOAD:
                    self._dbg(f"end-of-stream marker after {msg_count} media msgs")
                    break
                continue

            if offset == 0:
                continue

            chunk = self._decrypt_media_body(body, offset)
            if msg_count == 0:
                self._dbg(f"first media chunk: offset={offset} len={len(body)}")
            media += chunk
            msg_count += 1

        self._dbg(f"collected {len(media)} media bytes from {msg_count} messages")
        return media

    def _compute_effective_fps(
        self,
        info: StreamInfo,
        audio_path: Path | None,
        audio_bytes: bytes,
    ) -> float | None:
        if not info.video_frames:
            return float(info.nominal_fps) if info.nominal_fps else None

        audio_dur = _get_media_duration(audio_path) if audio_path and audio_path.exists() else None
        if not audio_dur or audio_dur <= 0:
            audio_dur = _estimate_audio_duration(info.aac_frames, audio_bytes)

        if audio_dur and audio_dur > 0:
            fps = info.video_frames / audio_dur
            self._dbg(f"calculated dynamic fps={fps:.6f} ({info.video_frames} frames / {audio_dur:.3f}s audio)")
            return fps

        return float(info.nominal_fps) if info.nominal_fps else None

    def _remux_clip(
        self,
        raw_path: Path,
        audio_path: Path | None,
        mp4_path: Path,
        codec: str,
        fps: float | None,
    ) -> Path:
        has_audio = bool(audio_path and audio_path.exists())

        if has_audio and _remux_to_mp4(raw_path, mp4_path, codec=codec, fps=fps, audio_path=audio_path, debug=self.debug):
            raw_path.unlink(missing_ok=True)
            audio_path.unlink(missing_ok=True)
            return mp4_path

        if has_audio:
            self._dbg("ffmpeg remux with audio failed, trying video-only")
            audio_path.unlink(missing_ok=True)

        if _remux_to_mp4(raw_path, mp4_path, codec=codec, fps=fps, audio_path=None, debug=self.debug):
            raw_path.unlink(missing_ok=True)
            return mp4_path

        return raw_path

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
    ) -> Path:
        """Download recording range to output path."""
        if not self._key:
            raise BaichuanError("not logged in")

        media = await self._fetch_media_stream(start, end, channel, stream_type, logic_bitmap)
        video, audio, info = deframe_video(bytes(media))
        if not video:
            raise BaichuanError("no video frames decoded (wrong time range or password?)")

        codec = sniff_codec(bytes(video))
        self._dbg(f"decoded {codec} {info} video_bytes={len(video)} audio_bytes={len(audio)}")

        raw_path = out_path.with_suffix(f".{codec}")
        raw_path.write_bytes(video)

        audio_path: Path | None = None
        if audio:
            audio_path = out_path.with_suffix(".aac")
            audio_path.write_bytes(audio)

        if not (remux_mp4 and shutil.which("ffmpeg")):
            return raw_path

        mp4_path = out_path.with_suffix(".mp4")
        fps = self._compute_effective_fps(info, audio_path, bytes(audio))
        return self._remux_clip(raw_path, audio_path, mp4_path, codec, fps)