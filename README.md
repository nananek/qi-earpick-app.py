# qi-earpick-app.py

**サードパーティ実装 (unofficial)**。QiPower (Maxevis QE-1 系) Wi-Fi 耳かきカメラの
プロトコルを再実装した Flask ビューア + 録画ツール。公式アプリ
`com.molink.john.qipower` 非依存。

プロトコル仕様: [docs/protocol.md](docs/protocol.md)
最小再現 (依存なし): [scripts/grab_frame.py](scripts/grab_frame.py)

## 使い方

```sh
# Wi-Fi を Qipower-XXXXXX に接続してから
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python run.py
# → http://127.0.0.1:5000 を開く
```

環境変数:

| 名前 | 既定 | 説明 |
|------|------|------|
| `QIPOWER_IP` | `192.168.5.1` | デバイス IP |
| `QIPOWER_REC_DIR` | `recordings` | 録画保存先 |
| `HOST` | `127.0.0.1` | Flask bind host |
| `PORT` | `5000` | Flask port |

## 機能

- ライブ MJPEG ビューア (`/stream`)
- スナップショット保存（ブラウザ DL）
- LED on/off, 明度スライダ
- 録画 (`recordings/<timestamp>.mjpeg` に JPEG を連結保存)
- 録画一覧・サムネ・ダウンロード

## 録画ファイル (.mjpeg)

JPEG フレームの単純連結ファイル。ffmpeg で MP4 にエンコードしたい場合:

```sh
# MJPEG をそのまま MP4 コンテナに（再エンコードなし、互換性高）
ffmpeg -framerate 20 -i recordings/20260429_103000.mjpeg -c:v copy out.mp4

# H.264 にトランスコード（ファイル小）
ffmpeg -framerate 20 -i recordings/20260429_103000.mjpeg -c:v libx264 -pix_fmt yuv420p out.mp4
```

## 制約

- デバイスの Wi-Fi AP は **同時接続クライアント 1 台** のみ受け付ける。純正アプリと
  同時には使えない。
- macOS Safari は `multipart/x-mixed-replace` の挙動が貧弱な場合がある。Chrome /
  Firefox 推奨。
- Flask の reloader は 2 プロセス起動するためデバイス側の 1-client 制限とぶつかる。
  `run.py` では reloader を無効化済み。

## ネットワーク的な注意

デフォルトで `127.0.0.1` にだけ bind しています。`HOST=0.0.0.0` で公開する場合は
**同一 LAN の誰でもカメラを操作・録画閲覧できる** ことに注意してください。認証は
ありません。自宅 LAN かつ信頼できる範囲でのみ晒すこと。

## License

MIT — see [LICENSE](LICENSE).
