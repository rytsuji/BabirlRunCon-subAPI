# Disk monitor

```sh
python monitor_disk.py --config config.json
curl http://127.0.0.1:5401/monitor
```

`monitor_file` と同じ FastAPI / uvicorn を使用します。クエリ引数は不要です。

`config.json` の `disks` に監視対象を複数設定できます。

```json
{
  "host": "127.0.0.1",
  "port": 5401,
  "disks": [
    {"path": "/data", "limit": "90%"},
    {"path": "/backup", "limit": "2 TB"},
    {"path": "/archive", "limit": null}
  ]
}
```

指定ディレクトリが属するファイルシステムの使用量・総容量・使用率を返します。
ディレクトリ配下のファイルサイズの合計ではありません。同一ファイルシステム上のパスは同じ容量になります。
相対パスは config ファイルのディレクトリを基準にします。

上限は使用量に対して判定し、上限以上なら `error` です。`null`（または省略）は上限判定なしです。
容量指定は B / KB / MB / GB / TB / PB / EB（1000進）、KiB / MiB / GiB / TiB / PiB / EiB（1024進）と % に対応します。
表示は GB / TB などの1000進です。

レスポンスは `status` と `data`（`message`, `disk` 配列）を返します。
各ディスクの項目は `path` と `usage`（使用量 / 総容量と使用率）の2列です。
上限超過したパスは全体の `message` に表示します。
取得できないパスは `usage` を `Unavailable` として返し、他のパスの取得は継続します。
取得失敗したパスも全体の `message` に表示します。
全体の `status` は `fail` > `error` > `ok` の優先順です。

```json
{
  "status": "ok",
  "data": {
    "message": "Disk usage retrieved",
    "disk": [
      {"path": "/data", "usage": "500.00 GB / 1.00 TB (50.00%)"},
      {"path": "/backup", "usage": "800.00 GB / 2.00 TB (40.00%)"}
    ]
  }
}
```
