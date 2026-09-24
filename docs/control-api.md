# GUI control API

MDCx now exposes a small control API from the GUI process itself. The API only
starts the existing default directory scrape; it does not accept a number and
does not implement a second scraping pipeline.

The server is configured with these environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `MDCX_CONTROL_ENABLED` | `1` | Set to `0`, `false`, or `no` to disable the API. |
| `MDCX_CONTROL_HOST` | `127.0.0.1` | Bind address. Containers should use `0.0.0.0`. |
| `MDCX_CONTROL_PORT` | `8750` | HTTP port. |
| `MDCX_CONTROL_TOKEN` | empty | Optional Bearer token or `X-MDCX-Control-Token` value. |
| `MDCX_CONTROL_ONLY` | empty | When enabled, disables GUI auto-start and timed scrape triggers. |

When the API is published from a container, set a non-empty
`MDCX_CONTROL_TOKEN` and do not expose the port publicly without an
authentication boundary.

## Endpoints

`GET /health` returns a lightweight health response.

`POST /api/scrape/start` starts the same `FileMode.Default` scan used by the
GUI start action. The request body is ignored; no number is required or used.
It returns `202` when a new scrape is accepted. If a scrape is already in
progress, it returns immediately with `200` as an idempotent no-op; it does
not start a second scrape and does not wait for the existing scrape to finish.
The returned `status.state` remains `running`, so callers that need the actual
scrape completion must continue polling `/api/scrape/status`.

`GET /api/scrape/status` returns the current state and counters:

```json
{
  "state": "running",
  "run_id": "…",
  "file_mode": "Default",
  "total": 12,
  "done": 3,
  "success": 3,
  "failed": 0,
  "remaining": 9,
  "stop_requested": false
}
```

`POST /api/scrape/stop` requests the same stop path as the GUI, without a
confirmation dialog. It returns `202` when accepted and `409` when idle.

When `/api/scrape/start` is called while a scrape is already running, the
response has this shape:

```json
{
  "accepted": true,
  "idempotent": true,
  "reason": "already_running",
  "run_id": "…",
  "status": {
    "state": "running",
    "run_id": "…"
  }
}
```

Example with a token:

```bash
curl -X POST http://127.0.0.1:8750/api/scrape/start \
  -H 'Authorization: Bearer change-me'
curl http://127.0.0.1:8750/api/scrape/status \
  -H 'Authorization: Bearer change-me'
```
