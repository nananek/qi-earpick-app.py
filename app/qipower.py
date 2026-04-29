"""QiPower (BlackBee/Bebird OEM) UDP client.

Wire format and command list: ../docs/protocol.md
"""
from __future__ import annotations

import logging
import queue
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_IP = "192.168.5.1"
PORT_IMG = 58080
PORT_CMD = 58090
PORT_SNS = 58098

CMD_START_VIDEO = bytes([0x20, 0x36])
CMD_STOP_VIDEO = bytes([0x20, 0x37])

CMD_GET_BAT_VOLTAGE = bytes([0x66, 0x3A])
CMD_SET_CAM_BRIGHTNESS = bytes([0x66, 0x3C])
CMD_TRIGGER_LED = bytes([0x66, 0x3F])

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"

# Hard upper bound — frames are typically ~24KB, the device's own buffer is 512KB.
FRAME_MAX = 524288


@dataclass
class _Chunk:
    frame_id: int
    last_flag: int
    chunk_idx: int
    angle_low: int
    payload: bytes

    @classmethod
    def parse(cls, data: bytes) -> Optional["_Chunk"]:
        if len(data) < 5:
            return None
        return cls(data[0], data[1], data[2], data[3], data[4:])


class QiPowerClient:
    """Owns the device sockets, runs a background reader, fans out JPEG frames
    to subscriber queues, and exposes synchronous control commands."""

    def __init__(self, ip: str = DEFAULT_IP):
        self.ip = ip
        self._img_sock: Optional[socket.socket] = None
        self._cmd_sock: Optional[socket.socket] = None
        self._cmd_lock = threading.Lock()
        self._reconnect_lock = threading.Lock()
        self._latest_frame: Optional[bytes] = None
        self._latest_angle: int = 0
        self._latest_lock = threading.Lock()
        self._subscribers: list[queue.Queue[bytes]] = []
        self._sub_lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._frames_seen = 0
        self._frames_dropped = 0
        self._last_frame_at: float = 0.0
        # The device exposes no read-back for LED state, so we mirror what we
        # last commanded. None until the user (or the resync probe below) sets
        # a known value.
        self._led_state: Optional[bool] = None
        self._brightness: Optional[int] = None
        self._battery: Optional[dict] = None
        self._battery_at: float = 0.0

    def start(self) -> None:
        if self._running:
            return
        self._img_sock = self._make_image_socket()

        self._cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._cmd_sock.bind(("0.0.0.0", 0))
        self._cmd_sock.settimeout(1.5)
        self._cmd_sock.connect((self.ip, PORT_CMD))

        self._running = True
        self._thread = threading.Thread(target=self._reader_loop, name="qipower-reader", daemon=True)
        self._thread.start()
        try:
            self._img_sock.send(CMD_START_VIDEO)
        except OSError as e:
            log.warning("initial START_VIDEO failed: %s", e)
        threading.Thread(target=self._initial_probe, daemon=True).start()
        log.info("QiPower client started against %s", self.ip)

    def _initial_probe(self) -> None:
        # Seed UI with the device's current values so users don't see "—" or
        # a default-50 slider that doesn't match reality.
        try:
            self.get_brightness()
        except Exception as e:  # noqa: BLE001
            log.debug("initial brightness probe failed: %s", e)
        try:
            self.get_battery()
        except Exception as e:  # noqa: BLE001
            log.debug("initial battery probe failed: %s", e)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._img_sock is not None:
            try:
                self._img_sock.send(CMD_STOP_VIDEO)
            except OSError:
                pass
            try:
                self._img_sock.close()
            except OSError:
                pass
            self._img_sock = None
        if self._cmd_sock is not None:
            try:
                self._cmd_sock.close()
            except OSError:
                pass
            self._cmd_sock = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        log.info("QiPower client stopped")

    def reconnect(self) -> None:
        """Tear down image socket + reader thread and rebuild from scratch.

        Useful after a device reboot or 1-client takeover that left our socket
        in a stale state. Control socket and subscribers are preserved.
        """
        with self._reconnect_lock:
            log.info("reconnect: tearing down image socket")
            self._running = False
            old_sock = self._img_sock
            old_thread = self._thread
            self._img_sock = None
            self._thread = None
            if old_sock is not None:
                try:
                    old_sock.close()
                except OSError:
                    pass
            if old_thread is not None:
                old_thread.join(timeout=2.0)

            self._img_sock = self._make_image_socket()
            self._running = True
            self._thread = threading.Thread(target=self._reader_loop, name="qipower-reader", daemon=True)
            self._thread.start()
            try:
                self._img_sock.send(CMD_START_VIDEO)
            except OSError as e:
                log.warning("reconnect START_VIDEO failed: %s", e)
            threading.Thread(target=self._initial_probe, daemon=True).start()
            log.info("reconnect: image socket rebuilt")

    def _make_image_socket(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Bigger UDP receive buffer to absorb burst arrival of chunks. Default
        # on macOS is ~200KB which is < 1s of video; under any GIL hiccup we
        # would silently drop packets. 4MB ≈ 20s of headroom.
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        except OSError:
            pass
        s.bind(("0.0.0.0", 0))
        s.settimeout(2.0)
        s.connect((self.ip, PORT_IMG))
        return s

    def _reader_loop(self) -> None:
        sock = self._img_sock
        if sock is None:
            return
        frame_id: Optional[int] = None
        chunks: dict[int, bytes] = {}
        first_chunk_size: Optional[int] = None
        last_packet_at = time.time()
        # The device sometimes "remembers" a previous client's source port and
        # ignores the first START_VIDEO from us. Resend more aggressively while
        # we have not seen a single packet yet.
        bootstrap_until = last_packet_at + 8.0
        bootstrap_interval = 1.0
        next_bootstrap = last_packet_at + bootstrap_interval

        while self._running:
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                now = time.time()
                if self._frames_seen == 0 and now < bootstrap_until and now >= next_bootstrap:
                    log.info("bootstrap: re-sending START_VIDEO (no frames yet)")
                    try:
                        sock.send(CMD_START_VIDEO)
                    except OSError:
                        pass
                    next_bootstrap = now + bootstrap_interval
                elif now - last_packet_at > 5:
                    log.info("idle %.1fs: re-sending START_VIDEO", now - last_packet_at)
                    try:
                        sock.send(CMD_START_VIDEO)
                    except OSError:
                        pass
                    last_packet_at = now
                continue
            except OSError:
                break

            last_packet_at = time.time()
            c = _Chunk.parse(data)
            if c is None:
                continue

            if c.chunk_idx == 1:
                frame_id = c.frame_id
                chunks = {1: c.payload}
                first_chunk_size = len(c.payload)
                continue

            if frame_id is None or c.frame_id != frame_id:
                continue

            chunks[c.chunk_idx] = c.payload

            if c.last_flag > 0:
                jpeg = self._assemble(chunks, first_chunk_size or 0)
                if jpeg is not None:
                    angle = c.angle_low + (256 if c.last_flag > 1 else 0)
                    self._publish(jpeg, angle)
                else:
                    self._frames_dropped += 1
                frame_id = None
                chunks = {}
                first_chunk_size = None

    @staticmethod
    def _assemble(chunks: dict[int, bytes], first_size: int) -> Optional[bytes]:
        # Drop any frame with a hole — zero-padding mid-stream lets the JPEG
        # decoder produce partial garbage (visible as periodic color glitches).
        # Match the official client which discards incomplete frames.
        if not chunks or 1 not in chunks:
            log.debug("assemble: no chunk_idx=1 (have %s)", sorted(chunks.keys())[:5])
            return None
        max_idx = max(chunks)
        if len(chunks) != max_idx:
            missing = [i for i in range(1, max_idx + 1) if i not in chunks]
            log.debug("assemble: hole, max_idx=%d missing=%s", max_idx, missing)
            return None
        parts = [chunks[i] for i in range(1, max_idx + 1)]
        raw = b"".join(parts)
        soi = raw.find(SOI)
        eoi = raw.find(EOI, soi + 2) if soi >= 0 else -1
        if soi < 0 or eoi < 0:
            log.debug("assemble: no SOI/EOI (max_idx=%d, len=%d)", max_idx, len(raw))
            return None
        return raw[soi : eoi + 2]

    def _publish(self, jpeg: bytes, angle: int) -> None:
        with self._latest_lock:
            self._latest_frame = jpeg
            self._latest_angle = angle
        self._frames_seen += 1
        self._last_frame_at = time.time()
        with self._sub_lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(jpeg)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(jpeg)
                except queue.Full:
                    pass

    def subscribe(self, maxsize: int = 4) -> queue.Queue:
        q: queue.Queue[bytes] = queue.Queue(maxsize=maxsize)
        with self._sub_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._sub_lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def latest_frame(self) -> tuple[Optional[bytes], int]:
        with self._latest_lock:
            return self._latest_frame, self._latest_angle

    def stats(self) -> dict:
        now = time.time()
        idle = (now - self._last_frame_at) if self._last_frame_at else None
        return {
            "frames_seen": self._frames_seen,
            "frames_dropped": self._frames_dropped,
            "subscribers": len(self._subscribers),
            "running": self._running,
            "ip": self.ip,
            "idle_seconds": idle,
            "angle": self._latest_angle,
            "led": self._led_state,
            "brightness": self._brightness,
            "battery": self._battery,
        }

    def _cmd(self, payload: bytes, expect_reply: bool = False) -> Optional[bytes]:
        sock = self._cmd_sock
        if sock is None:
            return None
        with self._cmd_lock:
            try:
                sock.send(payload)
            except OSError as e:
                log.warning("cmd send failed: %s", e)
                return None
            if not expect_reply:
                return None
            try:
                return sock.recv(4096)
            except socket.timeout:
                return None

    def set_led(self, on: bool) -> None:
        # 0x66 0x3F 0x00 <state>  — byte 3 = state (1=on, 0=off)
        self._cmd(CMD_TRIGGER_LED + bytes([0x00, 1 if on else 0]))
        self._led_state = on

    def set_brightness(self, value: int) -> None:
        v = max(0, min(100, int(value)))
        self._cmd(CMD_SET_CAM_BRIGHTNESS + bytes([v]))
        self._brightness = v

    def get_brightness(self) -> Optional[int]:
        # 0x66 0x3C 0xFE — Java `SetCameraBrightness(-2)`: write the magic
        # value -2 and read the device's reply (current brightness, 1 byte).
        data = self._cmd(CMD_SET_CAM_BRIGHTNESS + bytes([0xFE]), expect_reply=True)
        if data and 0 <= data[0] <= 100:
            self._brightness = data[0]
            return data[0]
        return None

    def get_battery(self) -> Optional[dict]:
        data = self._cmd(CMD_GET_BAT_VOLTAGE, expect_reply=True)
        if data is None or len(data) < 4:
            return None
        raw = struct.unpack(">i", data[:4])[0]
        # Observed: 00 01 00 17 (BE) — high16=status, low16=level. Exact unit
        # unconfirmed; the level monotonically decreased toward 0 as the
        # battery drained, so we expose it as a 0..100 percent (clamped).
        level = raw & 0xFFFF
        info = {
            "raw": raw,
            "hex": data[:4].hex(),
            "status": (raw >> 16) & 0xFFFF,
            "level": level,
            "percent": max(0, min(100, level)),
        }
        self._battery = info
        self._battery_at = time.time()
        return info
