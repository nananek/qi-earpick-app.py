# QiPower (Maxevis QE-1 系) 通信プロトコル

QiPower 純正アプリ `com.molink.john.qipower` の APK を静的解析した結果。
本機は Bebird/BlackBee 系 SDK の OEM 派生で、ローカル Wi-Fi 上の通信は
すべて UDP の 3 チャンネル構成。

このドキュメントは観察された挙動の文書化であり、暗号化や保護機構を回避する
ものは含みません。

## 解析対象

- パッケージ: `com.molink.john.qipower`
- 中核 SDK: `com.blackbee.libbb.*` (BebirdTube クラスがプロトコル本体)
- ネイティブ実装: `lib/armeabi-v7a/libBBCameraLibs.so` (`BlackBeeCamera::*`)
- デバイス: Maxevis QE-1 系 Wi-Fi 耳かきカメラ
- アプリ DL 元: APKPure 経由 (`apkeep -a com.molink.john.qipower -d apk-pure`)

## ネットワーク構成

| 項目 | 値 |
|---|---|
| デバイス AP SSID | `Qipower-XXXXXX` (開放、暗号化なし) |
| デバイス IP | `192.168.5.1` (固定) |
| クライアントに割り当てられる IP | `192.168.5.0/24` の DHCP |
| Wi-Fi 制限 | **同時接続クライアントは 1 台のみ** (純正アプリと同時利用不可) |

## ポート構成 (すべて UDP)

| Port | 用途 | 送受信 |
|------|------|---------|
| 58080 | MJPEG ストリーム + ストリーム開始/停止コマンド | デバイス → クライアント主体 |
| 58090 | 制御 (バッテリ・LED・カメラ・AP管理など) + ブロードキャスト発見 | 双方向 |
| 58098 | IMU/角度センサーデータ | デバイス → クライアント主体 |

TCP は今回確認した範囲では**使用していない**（jadx 上の `tcp` 分岐は
代替 SDK 用で、QiPower の URL は `udp://...` のみ）。

## 接続シーケンス

純正アプリの動作シーケンス (`StreamSelf.startStream` → `BebirdTube.getInstance` →
`BebirdTube.Init(58090)`):

1. **(任意) デバイス発見ブロードキャスト** — ローカルネット上のブロードキャ
   ストアドレスに UDP 58090 で `66 39 01 01` を送信。デバイスは 500B
   超の JSON テキスト (`{...}`) を返してくる。  
   QiPower は 192.168.5.x 固定なので、IP が既知ならスキップ可能。
   - 純正アプリのスキャン対象は第 3 オクテットが `5` または `188` のサブネットのみ
2. クライアントが UDP ソケット 3 本を作成し、それぞれデバイスへ `connect()`:
   - `mSocketImgUDP` → `192.168.5.1:58080`  (画像/ストリーム)
   - `mSocketCmdUDP` → `192.168.5.1:58090`  (制御)
   - `mSocketSnsUDP` → `192.168.5.1:58098`  (センサー)
3. **ストリーム開始**: `mSocketImgUDP` から `20 36` を送る (2 バイト)
4. デバイスが MJPEG パケットを 58080 に送り続ける
5. **角度センサーを使う場合**: `mSocketSnsUDP` から `86 06 01` を送る → 24B パケット
   が継続して届く
6. 終了時: `mSocketImgUDP` ← `20 37` (映像停止) / `mSocketSnsUDP` ← `86 06 00`

## 制御コマンド一覧 (UDP 58090)

すべて `mSocketCmdUDP.send(payload)` の形式。応答が必要なものは
同ソケットに `recvfrom()` で返ってくる。バイト列はビッグエンディアン表記。

| コマンド | バイト列 | 引数 | 応答 | 説明 |
|--------|----------|------|------|------|
| START_OTA | `66 38` + filename | path文字列 | なし | OTA 更新の開始 (ファーム関連) |
| START_CONF | `66 39 01 01` | (発見時) / `00` (バインド時) | JSON | 設定セッション開始 / 発見 |
| GET_APLIST | `66 39 02` | なし | UTF-8 (Wi-Fi 一覧) | 周辺 AP のスキャン結果 |
| CONNECT_AP | `66 39 03 01` + ssid | ssid文字列 | なし | AP に接続 (z=true) |
| CONNECT_AP (no save) | `66 39 03 00` + ssid | | なし | AP 接続 (保存しない) |
| FORGET_AP | `66 39 04` + ssid | | なし | AP 設定削除 |
| GET_CONNECTED_AP | `66 39 05` | なし | UTF-8 | 接続中 AP の SSID |
| GET_SELF_AP | `66 39 06` | なし | UTF-8 | 自分の AP 情報 |
| GET_BAT_VOLTAGE | `66 3A` | なし | 4B (BE int32) | バッテリ残量 (詳細下記) |
| SET_CAM_EFFECT | `66 3B XX` | XX = effect id | なし | 画像エフェクト |
| SET_CAM_BRIGHTNESS | `66 3C XX` | XX = -2..100 (-2 で取得) | byte (XX==-2 のとき) | 明度 |
| START_AP | `66 3D` | なし | なし | デバイスを AP モードに |
| REBOOT | `66 3E` | なし | なし | デバイス再起動 |
| TRIGGER_LED | `66 3F BB CC` | BB,CC = LED 制御 | なし | ライトの on/off など |
| TRIGGER_TWEEZERS | `66 40 XX` | XX = 制御 (-2 で読み出し) | byte (XX==-2 のとき) | ピンセット系 |

`66` = ASCII `f`。`66 39 ..` は `f9..` プリフィックス（"f9 series"）。
複数バイト応答は基本可変長。

### バッテリ応答 (`66 3A` のリプライ)

4 バイト big-endian で返ってくる。Java 側 (`BebirdTube.GetBatteryVoltage`) はそのまま
int32 として保持する。観測 (2026-04-29):

| 時刻 | hex | high16 (status) | low16 (value) |
|------|-----|-----------------|---------------|
| 10:30 起動時 | `00 01 00 17` | 1 | 23 |
| 10:35 頃 | `00 01 00 09` | 1 | 9 |
| 直後 (電池切れ) | (応答なし) | — | — |

経過から **`value` (low 16bit) は残量と単調減少関係**。0 で停止することを観測。
1 単位がパーセントか電圧か未確定。`status` (high 16bit) は今回 1 のみ観測。
充電中 / 異常時に他の値を取る可能性あり (要追加観測)。

純正アプリの旧 MoLianConnect 経路の処理 (`StreamSelf.getBatteryVoltage`) では低 16bit
を signed short として扱うコードになっており、本経路 (Bebird) でもおおむね同様の解釈で
通用する。

### IMU/センサー (UDP 58098)

| コマンド | バイト列 | 説明 |
|---|---|---|
| TOGGLE_ANGLE on | `86 06 01` | センサー出力の有効化 |
| TOGGLE_ANGLE off | `86 06 00` | 無効化 |

データパケットは **24 バイト固定 = `short[12]`**。リトルエンディアン
解釈で 12 個の int16 が並ぶ (Java 側は `ByteBuffer.asShortBuffer()` で
バイト順をそのまま `short[12]` 化)。実フィールドの内訳は未確定だが、
角度+加速度+磁気の典型 6軸/9軸データの可能性が高い。最初の要素が
`getDegree()` (画面回転判定の角度) として使われる。

## ストリーム (UDP 58080)

### ストリーム開始/停止

| コマンド | バイト列 |
|---|---|
| START_VIDEO | `20 36` |
| STOP_VIDEO | `20 37` |

注: `20 36` は ASCII で `" 6"`、`20 37` は `" 7"`。意味はおそらく機械的な
「ID 0x20 に対する on(0x36)/off(0x37)」コマンド。

### MJPEG パケット形式

各 UDP データグラム = **4 バイトヘッダ + JPEG 断片**。
chunk のペイロード長はおおよそ均一で、最初の chunk のペイロード長を
基準に並べる実装。

```
+--------+--------+--------+--------+----------------+
| byte 0 | byte 1 | byte 2 | byte 3 |  jpeg payload  |
+--------+--------+--------+--------+----------------+
```

| Offset | Size | Field | 説明 |
|--------|------|-------|------|
| 0 | 1 | `frame_id` | フレーム ID。同一フレームの全 chunk で共通の uint8 |
| 1 | 1 | `last_flag` | `0`: 中間 chunk。`>=1`: **末尾 chunk** (この chunk のペイロードに EOI を含む)。`>1` のときは角度に +256 を加える (高位ビット) |
| 2 | 1 | `chunk_idx` | chunk 番号 (1 開始)。`chunk_idx == 1` が新フレームの開始 |
| 3 | 1 | `angle_low` | ジャイロ角度の下位 8bit。実角度 = `angle_low + (last_flag > 1 ? 256 : 0)`。中間 chunk では一定値が入っているが意味のある値は末尾 chunk のもの |
| 4 | n | payload | JPEG 断片 (`chunk_idx == 1` で SOI 開始、`last_flag != 0` の chunk で EOI 終端) |

実機観測 (480×480, ~16KB の典型フレーム): chunk 1〜11 は `last_flag=0` で payload 1468B 固定、chunk 12 が `last_flag=1` で payload 396B + EOI。

### フレーム再構成手順

1. 各 chunk を受信
2. `chunk_idx == 1` を見つけたら新フレーム開始: `frame_id` を覚え、`mFrameData` をクリア
3. 同フレーム内の `frame_id` が一致しない chunk が来たら **パケロス** (フレーム破棄)
4. 各 chunk の payload を `mFrameData[(chunk_idx - 1) * first_chunk_payload_size ..]` に書き込む
5. payload 内に EOI (`FF D9`) を見つけたらフレーム完成
6. `mFrameData[soi..eoi+2]` を `BitmapFactory.decodeByteArray` (= 普通の JPEG デコーダ) で復号

実装上の上限値: `FRAME_MAX_LENGTH = 524288` (512KB), `CHUNK_MAX_LENGTH = 4096`。

### 観察された動作パラメータ (2026-04-29 実機計測)

- 公称 20fps (純正アプリ報告)
- 解像度: **480×480** (baseline JPEG, JFIF 1.01, 3 components)
- 1フレームあたり ~16KB
- chunk ペイロード: 中間 chunk = 1468B 固定、末尾 chunk のみ可変 (端数)
  - 1468 = 1500 MTU − 20 IP − 8 UDP − 4 ヘッダ
- chunk 数: 1フレーム ~12 chunk

## 既知のレガシー経路（QiPower 個体では未使用）

純正アプリには `MoLianConnect`（旧 MoLink モデル用）のフォールバック経路があり、
こちらは:

- 固定 IP `192.168.10.123`
- ネイティブ JNI (`libBBCmd*`) で SETCMD/RETCMD 形式の独自パケットを直接送る
- ストリームは別形式

QiPower は `BebirdTube` 経路（本ドキュメントの内容）が一致する。`isBebirdConnect()`
の判定で `BebirdTube` 側が優先される。

## 解析時の参考

- `com/blackbee/libbb/BebirdTube.java` ─ プロトコル本体
- `com/blackbee/libbb/StreamSelf.java` ─ 上位ラッパー (発見ループ + 開始トリガ)
- `com/blackbee/libbb/NativeLibs.java` ─ MoLianConnect (legacy) 用 JNI
- `lib/armeabi-v7a/libBBCameraLibs.so` ─ ネイティブ実装、symbol `BlackBeeCamera::*`
