#!/usr/bin/env python3
"""QiPower カメラから JPEG フレームを 1 枚（または N 枚）取得する最小実装。

プロトコル詳細は ../docs/protocol.md を参照。

使い方:
    python3 grab_frame.py                       # 1枚取得して frame.jpg に保存
    python3 grab_frame.py -n 30                 # 30枚取得 (frame_0000.jpg ...)
    python3 grab_frame.py --ip 192.168.5.1 -n 10
    python3 grab_frame.py --debug               # 受信パケットを詳細表示

依存: 標準ライブラリのみ。
"""
from __future__ import annotations

import argparse
import socket
import sys
import time
from dataclasses import dataclass

DEFAULT_IP = "192.168.5.1"
PORT_IMG = 58080
PORT_CMD = 58090  # noqa: F841  (control port, kept for future commands)
PORT_SNS = 58098  # noqa: F841

CMD_START_VIDEO = bytes([0x20, 0x36])
CMD_STOP_VIDEO = bytes([0x20, 0x37])

SOI = b"\xff\xd8"
EOI = b"\xff\xd9"

FRAME_MAX = 524288


@dataclass
class Chunk:
    frame_id: int  # byte 0
    last_flag: int  # byte 1: 0 = 中間, >0 = 末尾 chunk (EOI 含む), >1 で angle に +256
    chunk_idx: int  # byte 2 (1-based)
    angle_low: int  # byte 3
    payload: bytes


def parse_chunk(data: bytes) -> Chunk | None:
    if len(data) < 5:
        return None
    return Chunk(
        frame_id=data[0],
        last_flag=data[1],
        chunk_idx=data[2],
        angle_low=data[3],
        payload=data[4:],
    )


def reassemble_one_frame(sock: socket.socket, debug: bool = False) -> tuple[bytes, int] | None:
    """1 フレーム分の chunk を集めて JPEG bytes と角度を返す。"""
    frame_id: int | None = None
    chunks: dict[int, bytes] = {}
    first_chunk_size: int | None = None
    angle: int = 0

    deadline = time.time() + 3.0  # 1フレーム待つ最大時間
    while time.time() < deadline:
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            return None

        c = parse_chunk(data)
        if c is None:
            continue

        if debug:
            print(
                f"  chunk frame_id={c.frame_id:3d} last={c.last_flag} "
                f"idx={c.chunk_idx:3d} angle_low={c.angle_low:3d} "
                f"payload={len(c.payload)}B"
            )

        # 新フレームの開始: chunk_idx == 1
        if c.chunk_idx == 1:
            frame_id = c.frame_id
            chunks = {1: c.payload}
            first_chunk_size = len(c.payload)
            continue

        if frame_id is None:
            # まだ最初の chunk を見ていない
            continue

        if c.frame_id != frame_id:
            # フレーム途中で別フレームの chunk が来た = 取りこぼし
            if debug:
                print(f"  ! drop frame {frame_id} (got chunk for {c.frame_id})")
            frame_id = None
            chunks = {}
            first_chunk_size = None
            continue

        chunks[c.chunk_idx] = c.payload

        # 末尾 chunk: last_flag > 0 = EOI 含み + 角度確定
        if c.last_flag > 0:
            angle = c.angle_low + (256 if c.last_flag > 1 else 0)
            break
    else:
        return None  # deadline

    if not chunks or 1 not in chunks:
        return None

    # 1..N の順に並べる
    max_idx = max(chunks)
    parts = []
    for i in range(1, max_idx + 1):
        if i in chunks:
            parts.append(chunks[i])
        else:
            # 欠損 chunk は first_chunk_size 分のゼロ埋めで詰める（暫定）
            parts.append(b"\x00" * (first_chunk_size or 0))
    raw = b"".join(parts)

    soi_at = raw.find(SOI)
    eoi_at = raw.find(EOI, soi_at + 2)
    if soi_at < 0 or eoi_at < 0:
        return None
    return raw[soi_at : eoi_at + 2], angle


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ip", default=DEFAULT_IP)
    p.add_argument("-n", "--count", type=int, default=1, help="保存するフレーム数")
    p.add_argument("-o", "--out", default="frame.jpg", help="出力ファイル名 / プレフィックス")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--no-stop", action="store_true", help="終了時に STOP_VIDEO を送らない")
    args = p.parse_args(argv)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    sock.settimeout(2.0)
    sock.connect((args.ip, PORT_IMG))

    print(f"# connect udp://{args.ip}:{PORT_IMG}, sending START_VIDEO")
    sock.send(CMD_START_VIDEO)

    saved = 0
    try:
        for i in range(args.count):
            result = reassemble_one_frame(sock, debug=args.debug)
            if result is None:
                print(f"frame {i}: timeout / no SOI-EOI", file=sys.stderr)
                continue
            jpeg, angle = result
            if args.count == 1:
                path = args.out
            else:
                base, dot, ext = args.out.rpartition(".")
                path = f"{base or 'frame'}_{i:04d}.{ext or 'jpg'}"
            with open(path, "wb") as f:
                f.write(jpeg)
            print(f"frame {i}: {len(jpeg)}B angle={angle} -> {path}")
            saved += 1
    finally:
        if not args.no_stop:
            try:
                sock.send(CMD_STOP_VIDEO)
            except OSError:
                pass
        sock.close()

    return 0 if saved > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
