# Media Janitor

Proactive media library quality monitor for Plex/Radarr/Sonarr. Automatically detects and replaces bad media files.

## Features

- **Validates new imports** - Webhooks from Radarr/Sonarr trigger instant validation
- **Background scanning** - Gradually scans existing library at configurable rate
- **Auto-replaces bad files** - Deletes corrupt files, blocklists release, triggers re-download
- **Path mismatch detection** - Finds files in wrong folders (e.g., wrong movie in folder)
- **Library reports** - Top 50 largest/smallest files, quality breakdown
- **Daily email summaries** - What was scanned, flagged, and replaced
- **Low bandwidth mode** - Configurable scan rate, daily replacement limits

## What It Detects

| Problem | Detection Method |
|---------|-----------------|
| Corrupt files | ffprobe fails to read metadata |
| Wrong duration | File claims 300h instead of 30m |
| Truncated files | ffmpeg decode test fails |
| Encoding errors | ffmpeg reports errors during sample decode |
| Low bitrate | Below minimum for resolution (720p/1080p/4K) |
| Path mismatches | Filename doesn't match expected movie title |

## Quick Start (Unraid)

### 1. Build the Image

```bash
cd /tmp
git clone https://github.com/gnegelow-caylent/media-janitor.git
cd media-janitor
docker build -t ghcr.io/gnegelow-caylent/media-janitor:latest .
```

### 2. Create Config Directory

```bash
mkdir -p /mnt/user/appdata/media-janitor
```

### 3. Create Config File

Create `/mnt/user/appdata/media-janitor/config.yaml`:

```yaml
radarr:
  - name: "radarr"
    url: "http://192.168.1.3:7878"
    api_key: "YOUR_RADARR_API_KEY"
    path_mappings:
      - from_path: "/movies"
        to_path: "/mnt/remotes/192.168.1.5_video/Movies"

sonarr:
  - name: "sonarr-main"
    url: "http://192.168.1.3:8989"
    api_key: "YOUR_SONARR_API_KEY"
    path_mappings:
      - from_path: "/tv"
        to_path: "/mnt/remotes/192.168.1.2_video/TV"

scanner:
  enabled: true
  files_per_hour: 100
  mode: "watch_only"
  tv_refresh_schedule: "0 3 * * *"

actions:
  auto_replace: true
  blocklist_bad_releases: true
  max_replacements_per_day: 10

email:
  enabled: true
  smtp_host: "smtp.gmail.com"
  smtp_port: 587
  smtp_user: "you@gmail.com"
  smtp_password: "xxxx xxxx xxxx xxxx"  # Gmail App Password
  from_address: "you@gmail.com"
  to_address: "you@gmail.com"
  daily_summary_time: "08:00"

logging:
  level: "INFO"
  file: "/data/logs/media-janitor.log"
```

### 4. Run the Container

```bash
docker run -d \
  --name media-janitor \
  -p 9000:9000 \
  -v /mnt/user/appdata/media-janitor:/data \
  -v /mnt/remotes:/mnt/remotes:ro \
  ghcr.io/gnegelow-caylent/media-janitor:latest
```

### 5. Configure Webhooks

In **Radarr** → Settings → Connect → Add → Webhook:
- Name: `media-janitor`
- On Import: ✓
- On Upgrade: ✓
- URL: `http://192.168.1.3:9000/webhook/radarr`

In **Sonarr** (each instance) → Settings → Connect → Add → Webhook:
- Name: `media-janitor`
- On Import: ✓
- On Upgrade: ✓
- URL: `http://192.168.1.3:9000/webhook/sonarr`

## Path Mappings

Radarr/Sonarr report paths as they see them inside their containers. Media Janitor needs to translate these to actual filesystem paths.

Example: If Radarr sees `/movies/Some Movie (2020)/movie.mkv` but the actual path is `/mnt/remotes/192.168.1.5_video/Movies/Some Movie (2020)/movie.mkv`:

```yaml
path_mappings:
  - from_path: "/movies"
    to_path: "/mnt/remotes/192.168.1.5_video/Movies"
```

Find your paths in Radarr/Sonarr → Settings → Media Management → Root Folders.

## Configuration Reference

### Scanner Settings

```yaml
scanner:
  enabled: true
  files_per_hour: 100        # Scan rate (lower = less bandwidth)
  mode: "watch_only"         # "watch_only" or "continuous"
  tv_refresh_schedule: "0 3 * * *"  # When to load TV library (cron format)
```

- **watch_only**: Scan library once, then only validate new imports via webhooks
- **continuous**: Keep re-scanning library forever
- **tv_refresh_schedule**: TV episodes are slow to fetch, so they load on a schedule (default 3am). Movies load on startup.

### Validation Settings

```yaml
validation:
  check_duration_sanity: true
  max_duration_hours: 12

  check_bitrate: true
  min_bitrate_720p: 1500     # kbps
  min_bitrate_1080p: 3000    # kbps
  min_bitrate_4k: 8000       # kbps

  deep_scan_enabled: true
  sample_duration_seconds: 30

  full_decode_enabled: false  # Very slow, use sparingly
```

### Action Settings

```yaml
actions:
  auto_replace: true           # Auto delete and re-download bad files
  blocklist_bad_releases: true # Prevent re-downloading same bad release
  max_replacements_per_day: 10 # Daily limit to control bandwidth
```

## API Endpoints

### Health & Status

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check |
| `/status` | GET | Scanner status, queue size, counts |

### Webhooks

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/webhook/radarr` | POST | Radarr import notifications |
| `/webhook/sonarr` | POST | Sonarr import notifications |
| `/webhook/test` | POST | Test webhook connectivity |

### Reports

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/report/library?source=movies&format=json` | GET | Library stats (largest/smallest files) |
| `/report/library?source=movies&format=html` | GET | HTML version for browser |
| `/report/mismatches?source=movies` | GET | Files with wrong names/paths |
| `/report/library/email` | POST | Send library report via email |

**Source options**: `movies` (fast), `tv` (slow), `all` (slowest)

### Manual Actions

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/scan/trigger` | POST | Trigger a background scan batch |
| `/scan/refresh?source=movies` | POST | Refresh library list from Radarr/Sonarr |
| `/state/clear` | POST | Clear scan state (forces full re-scan) |

### Logs

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/logs?lines=100&level=all` | GET | Recent log entries |
| `/logs/errors?lines=50` | GET | Recent errors only |

## Gmail App Password Setup

Gmail requires an App Password for SMTP:

1. Go to https://myaccount.google.com/security
2. Enable **2-Step Verification** if not already
3. Go to https://myaccount.google.com/apppasswords
4. Generate a new App Password for "Mail"
5. Use the 16-character password in your config

## Monitoring

Check scanner progress:
```bash
curl http://192.168.1.3:9000/status
```

View recent logs:
```bash
docker logs media-janitor --tail 50
```

Get library report:
```bash
curl "http://192.168.1.3:9000/report/library?source=movies"
```

Find path mismatches:
```bash
curl "http://192.168.1.3:9000/report/mismatches?source=movies"
```

## How It Works

1. **Startup**: Loads movies from Radarr (fast), queues them for scanning
2. **Background scan**: Validates files with ffprobe at configured rate
3. **TV schedule**: Loads TV episodes at scheduled time (default 3am)
4. **Webhooks**: New imports are validated immediately
5. **Auto-replace**: Bad files are deleted, blocklisted, and re-searched
6. **Completion**: Once initial scan is done, only webhooks trigger validation

## Troubleshooting

### Scanner not processing files

Check if scanner is enabled and mode isn't blocking:
```bash
curl http://192.168.1.3:9000/status
```

If `initial_scan_done: true` with `mode: watch_only`, scanning has completed. Only webhooks will trigger validation.

### Files not found

Path mappings might be wrong. Check that translated paths exist:
```bash
docker exec media-janitor ls -la /mnt/remotes/
```

### Webhooks not working

Test the endpoint:
```bash
curl -X POST http://192.168.1.3:9000/webhook/test -H "Content-Type: application/json" -d '{}'
```

Check Radarr/Sonarr webhook test button and logs.

### Slow startup

First startup fetches your entire library. Movies are fast (~30s for 5000 movies). TV is slow (hours for large libraries) which is why it loads on a schedule instead.

### Email not sending

- Use Gmail App Password, not regular password
- Check spam folder
- Verify SMTP settings in config

## Updating

When GitHub Actions runner is working:
```bash
docker pull ghcr.io/gnegelow-caylent/media-janitor:latest
docker stop media-janitor && docker rm media-janitor
# Run docker run command again
```

Manual update:
```bash
cd /tmp/media-janitor
git pull
docker build -t ghcr.io/gnegelow-caylent/media-janitor:latest .
docker stop media-janitor && docker rm media-janitor
# Run docker run command again
```

## License

MIT
