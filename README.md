# Media Janitor

Proactive media library quality monitor for Plex/Radarr/Sonarr. Automatically detects and replaces bad media files.

## Features

- **Web UI Dashboard** - Real-time monitoring, library browsing, reports, and settings management
- **Validates new imports** - Webhooks from Radarr/Sonarr trigger instant validation
- **Background scanning** - Gradually scans existing library at configurable rate
- **Auto-replaces bad files** - Deletes corrupt files, blocklists release, triggers re-download
- **Path mismatch detection** - Detects wrong files in folders and triggers replacement (not rename)
- **Duplicate detection** - Finds same content in multiple qualities with space savings report
- **Library reports** - Codec breakdown, HDR analysis, suspicious files, and more
- **Plex integration** - OAuth login, library refresh, watch-based prioritization, quality upgrade suggestions, playback issue detection, orphan file detection
- **Multi-platform notifications** - Discord, Slack, Telegram, Pushover, Gotify, Email
- **Dry run mode** - Test without making changes

## Web UI

Access the web interface at `http://your-server:9000`

- **Dashboard** - Scanner status, recent activity, quick stats (including wrong files count)
- **Library** - Browse movies and TV shows with quality info
- **Reports** - Path mismatches, duplicates, codec breakdown, HDR content, quality by instance
- **Logs** - Real-time log viewer with filtering
- **Settings** - Configure everything from the browser (General, Security, Connections, Actions, Notifications)

> **Note**: The web UI pages do not require an API key. Authentication is only required for API endpoints and webhooks (see [Authentication](#authentication) below).

## What It Detects

| Problem | Detection Method |
|---------|-----------------|
| Corrupt files | ffprobe fails to read metadata |
| Wrong duration | File claims 300h instead of 30m |
| Truncated files | ffmpeg decode test fails |
| Encoding errors | ffmpeg reports errors during sample decode |
| Low bitrate | Below minimum for resolution and codec (HEVC/AV1 use lower thresholds) |
| Path mismatches | Filename doesn't match expected title (triggers replacement) |
| Duplicates | Same content in multiple qualities |

### Path Mismatch Detection

When a file passes all validation checks but the filename doesn't match the expected title from Radarr/Sonarr, it's flagged as a "wrong file". This typically happens when a completely different movie ends up in a folder (e.g., "Titanic.mkv" in an "Avatar (2009)" folder).

**Important**: Wrong files trigger **replacement**, not rename. Renaming would be incorrect since the actual content is wrong. Media Janitor will:
1. Delete the wrong file
2. Blocklist the release (if enabled)
3. Trigger a search for the correct content

Wrong files are tracked separately and shown on the dashboard as "Wrong Files".

## Quick Start (Unraid)

### 1. Create Config Directory

```bash
mkdir -p /mnt/user/appdata/media-janitor/logs
mkdir -p /mnt/user/appdata/media-janitor/state
```

### 2. Create Config File

Create `/mnt/user/appdata/media-janitor/config.yaml`:

```yaml
radarr:
  - name: "radarr"
    url: "http://YOUR_SERVER_IP:7878"
    api_key: "YOUR_RADARR_API_KEY"
    path_mappings:
      - from_path: "/movies"           # Path inside Radarr container
        to_path: "/media/movies"       # Path inside media-janitor container

sonarr:
  - name: "sonarr"
    url: "http://YOUR_SERVER_IP:8989"
    api_key: "YOUR_SONARR_API_KEY"
    path_mappings:
      - from_path: "/tv"               # Path inside Sonarr container
        to_path: "/media/tv"           # Path inside media-janitor container

scanner:
  enabled: true
  files_per_hour: 300
  mode: "watch_only"

actions:
  auto_replace: true
  blocklist_bad_releases: true
  max_replacements_per_day: 10
  dry_run: false

logging:
  level: "INFO"
  file: "/data/logs/media-janitor.log"
```

### 3. Run the Container

```bash
docker run -d \
  --name media-janitor \
  -p 9000:9000 \
  -v /mnt/user/appdata/media-janitor/config.yaml:/data/config.yaml \
  -v /mnt/user/appdata/media-janitor/logs:/data/logs \
  -v /mnt/user/appdata/media-janitor/state:/data/state \
  -v /path/to/your/movies:/media/movies:ro \
  -v /path/to/your/tv:/media/tv:ro \
  -e TZ=America/New_York \
  --restart unless-stopped \
  ghcr.io/gnegelow-caylent/media-janitor:latest
```

> **Note**: For Unraid remote mounts, use `:ro,slave` option (e.g., `-v /mnt/remotes/nas/movies:/media/movies:ro,slave`)

> **Note**: The `:ro,slave` mount option is required for Unraid remote mounts to work properly.

### 4. Configure Webhooks

In **Radarr** → Settings → Connect → Add → Webhook:
- Name: `media-janitor`
- On Import: ✓
- On Upgrade: ✓
- URL: `http://YOUR_UNRAID_IP:9000/webhook/radarr?apikey=YOUR_API_KEY`

In **Sonarr** → Settings → Connect → Add → Webhook:
- Name: `media-janitor`
- On Import: ✓
- On Upgrade: ✓
- URL: `http://YOUR_UNRAID_IP:9000/webhook/sonarr?apikey=YOUR_API_KEY`

> **Note**: If you have an API key configured (see [Authentication](#authentication)), append `?apikey=YOUR_API_KEY` to the webhook URLs. Without an API key set, the URLs work without it.

### 5. Access Web UI

Open `http://YOUR_UNRAID_IP:9000` in your browser. No API key is required to browse the UI.

## Path Mappings

Radarr/Sonarr report paths as they see them inside their containers. Media Janitor needs to translate these to actual filesystem paths.

Example: If Radarr sees `/movies/Some Movie (2020)/movie.mkv` but the actual path on your host is `/mnt/user/media/Movies/Some Movie (2020)/movie.mkv`:

```yaml
path_mappings:
  - from_path: "/movies"
    to_path: "/media/movies"  # Maps to your container mount
```

Find your paths in Radarr/Sonarr → Settings → Media Management → Root Folders.

**Missing Mapping Warnings**: The dashboard will show a warning banner if files are found with paths that don't have corresponding mappings configured. This helps identify configuration issues early.

## Configuration Reference

### Radarr/Sonarr Instances

```yaml
radarr:
  - name: "radarr"           # Friendly name
    url: "http://YOUR_SERVER_IP:7878"
    api_key: "YOUR_API_KEY"
    path_mappings:
      - from_path: "/movies"
        to_path: "/media/movies"

sonarr:
  - name: "sonarr-anime"
    url: "http://YOUR_SERVER_IP:8989"
    api_key: "YOUR_API_KEY"
    path_mappings:
      - from_path: "/anime"
        to_path: "/media/anime"
```

> **Multiple Instances**: If running multiple Sonarr instances that share the same download client (NZBGet/SABnzbd), configure each instance to use a **different category** (e.g., `tv`, `tv-kids`). This prevents instances from seeing each other's downloads and causing "series mismatch" errors.

### Plex Integration

```yaml
plex:
  enabled: true
  url: "http://YOUR_SERVER_IP:32400"
  token: "YOUR_PLEX_TOKEN"  # Or login via Web UI
  refresh_on_replace: true  # Trigger library refresh after replacements
```

You can also authenticate via OAuth in the Web UI Settings → Connections → Plex.

### Scanner Settings

```yaml
scanner:
  enabled: true
  files_per_hour: 300        # Scan rate (validation reads ~2-5MB per file)
  concurrency: 10            # Parallel file validations
  mode: "watch_only"         # "watch_only" or "continuous"
  tv_refresh_schedule: "0 3 * * *"  # When to refresh TV library (cron format)
```

- **files_per_hour**: Recommended rates by storage type:
  - Network mounts: 500-1500
  - Local SSD: 3000-5000
  - NVMe: 5000+
- **watch_only**: Scan library once, then only validate new imports via webhooks
- **continuous**: Keep re-scanning library forever
- **tv_refresh_schedule**: TV episodes load on a schedule (default 3am) because large libraries can take time

### Validation Settings

```yaml
validation:
  check_duration_sanity: true
  max_duration_hours: 12

  check_bitrate: true
  min_bitrate_720p: 1500     # kbps (base threshold for H.264)
  min_bitrate_1080p: 3000    # kbps (base threshold for H.264)
  min_bitrate_4k: 8000       # kbps (base threshold for H.264)

  # Codec efficiency multipliers (modern codecs need less bitrate)
  codec_bitrate_multiplier_hevc: 0.6  # HEVC/H.265/x265 uses 60% of base
  codec_bitrate_multiplier_av1: 0.5   # AV1 uses 50% of base
  codec_bitrate_multiplier_vp9: 0.6   # VP9 uses 60% of base

  deep_scan_enabled: true
  deep_scan_mode: "partial"    # "partial" (start only) or "full" (start/middle/end)
  sample_duration_seconds: 10  # Seconds to decode per sample point
  decode_timeout_seconds: 60   # Timeout for each decode test

  full_decode_enabled: false  # Very slow, use sparingly
```

**Deep Scan Modes**:
- **partial**: Test start of file only (faster, recommended for network mounts)
- **full**: Test start, middle, and end of file (3x slower but more thorough)

**Recommended Settings by Storage Type**:

| Setting | Local SSD | Local HDD | Network Mount |
|---------|-----------|-----------|---------------|
| `sample_duration_seconds` | 30 | 15-20 | 5-10 |
| `decode_timeout_seconds` | 30 | 45-60 | 60-120 |
| `deep_scan_mode` | full | partial | partial |

**Codec-Aware Bitrate Thresholds**: Modern codecs like HEVC and AV1 achieve better quality at lower bitrates. A 1080p HEVC file at 1800kbps is roughly equivalent to H.264 at 3000kbps. The multipliers adjust the minimum thresholds accordingly:

| Codec | 720p Min | 1080p Min | 4K Min |
|-------|----------|-----------|--------|
| H.264/x264 | 1500 kbps | 3000 kbps | 8000 kbps |
| HEVC/x265 | 900 kbps | 1800 kbps | 4800 kbps |
| AV1 | 750 kbps | 1500 kbps | 4000 kbps |

**Bitrate Detection**: If ffprobe doesn't report bitrate (common with MKV files), Media Janitor calculates it from file size and duration.

### Action Settings

```yaml
actions:
  auto_replace: true           # Auto delete and re-download bad files
  auto_delete_duplicates: false # Automatically remove duplicate copies
  blocklist_bad_releases: true  # Prevent re-downloading same bad release
  max_replacements_per_day: 10  # Daily limit to control bandwidth
  dry_run: false               # Report only, no actual changes
```

**Rate Limiting**: The `max_replacements_per_day` counter:
- Persists across container restarts (stored in state file)
- Resets automatically at midnight via scheduled cron job
- Files that were rate-limited ("queued") are automatically processed immediately after the midnight reset
- Concurrent replacements are serialized to prevent exceeding the daily limit
- Resets when you clear state via "Restart Full Scan" in the UI
- Can be manually reset via the `/state/reset-replacements` endpoint

### Notifications

#### Discord

```yaml
notifications:
  discord:
    enabled: true
    webhook_url: "https://discord.com/api/webhooks/..."
```

#### Slack

```yaml
notifications:
  slack:
    enabled: true
    webhook_url: "https://hooks.slack.com/services/..."
```

#### Telegram

```yaml
notifications:
  telegram:
    enabled: true
    bot_token: "123456:ABC-DEF..."
    chat_id: "-1001234567890"
```

#### Pushover

```yaml
notifications:
  pushover:
    enabled: true
    user_key: "your_user_key"
    api_token: "your_api_token"
```

#### Gotify

```yaml
notifications:
  gotify:
    enabled: true
    server_url: "https://gotify.example.com"
    app_token: "your_app_token"
```

#### Email

```yaml
email:
  enabled: true
  smtp_host: "smtp.gmail.com"
  smtp_port: 587
  smtp_user: "you@gmail.com"
  smtp_password: "xxxx xxxx xxxx xxxx"  # Gmail App Password
  from_address: "you@gmail.com"
  to_address: "you@gmail.com"
  daily_summary_time: "08:00"
```

### UI Settings

```yaml
ui:
  theme: "dark"              # "dark" or "light"
  timezone: "America/New_York"
```

### Authentication

```yaml
webhook:
  enabled: true
  host: "0.0.0.0"
  port: 9000
  api_key: "your-secret-key-here"  # Leave empty to disable auth
```

When an API key is configured, all API endpoints and webhooks require authentication. The key can be provided via:

- **Header**: `X-Api-Key: your-key`
- **Bearer token**: `Authorization: Bearer your-key`
- **Query parameter**: `?apikey=your-key` (useful for webhook URLs in Radarr/Sonarr)

**Excluded from auth** (no key needed):
- Web UI pages (`/`, `/ui/*`)
- Plex auth flow (`/auth/*`)
- Static files (`/static/*`)
- Health check (`/health`)

You can generate and manage the API key from the Web UI at **Settings → General → Security**.

### Logging

```yaml
logging:
  level: "INFO"              # DEBUG, INFO, WARNING, ERROR
  file: "/data/logs/media-janitor.log"
```

## API Endpoints

### Web UI (no auth required)

| Endpoint | Description |
|----------|-------------|
| `/` | Dashboard |
| `/ui/library` | Browse library |
| `/ui/reports` | View reports |
| `/ui/logs` | Log viewer |
| `/ui/settings` | Configuration |

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
| `/report/library?source=movies` | GET | Library stats |
| `/report/library/email` | POST | Email library report |
| `/report/replaced` | GET | Replacement history |
| `/report/missing` | GET | Files not found on disk |
| `/report/missing/clear` | POST | Clear missing files list |
| `/report/mismatches?source=movies` | GET | Path mismatches |
| `/report/duplicates?source=movies` | GET | Duplicate content |
| `/report/codecs?source=movies` | GET | Codec and HDR breakdown |

**Source options**: `movies`, `tv`, `all`

### API (Settings)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/config` | GET | Get current config (secrets masked) |
| `/api/config` | POST | Update config |
| `/api/test-connection` | POST | Test Radarr/Sonarr connection |
| `/api/test-plex` | POST | Test Plex connection |
| `/api/test-notification` | POST | Test notification service |
| `/api/test-email` | POST | Test email configuration |

### Authentication

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/auth/plex/start` | GET | Start Plex OAuth flow |
| `/auth/plex/check` | GET | Check Plex auth status |
| `/auth/user` | GET | Get current authenticated user |
| `/auth/logout` | POST | Logout current user |

### Manual Actions

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/scan/trigger` | POST | Trigger a background scan batch |
| `/scan/refresh?source=movies` | POST | Refresh library list |
| `/state/reset-replacements` | POST | Reset only the daily replacement counter (preserves scan progress) |
| `/state/clear` | POST | Clear all state (forces full re-scan, resets daily counter, clears queue and cache) |

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
curl -H "X-Api-Key: YOUR_API_KEY" http://YOUR_SERVER:9000/status
```

View recent logs:
```bash
docker logs media-janitor --tail 50
```

Or use the Web UI at `/logs` for a better experience.

## How It Works

1. **Startup**: Loads movies from Radarr (fast), queues them for scanning
2. **Background scan**: Validates files with ffprobe at configured rate
3. **TV schedule**: Loads TV episodes at scheduled time (default 3am) using efficient bulk API
4. **Webhooks**: New imports are validated immediately (requires API key if auth is enabled)
5. **Auto-replace**: Bad files (including wrong files in folders) are deleted, blocklisted, and re-searched
6. **Rate limiting**: Replacements are capped per day; queued files are processed automatically after midnight reset
7. **Notifications**: Get alerted via Discord, Slack, Telegram, etc.
8. **Completion**: Once initial scan is done, only webhooks trigger validation

## Troubleshooting

### Scanner not processing files

Check status via Web UI dashboard or:
```bash
curl -H "X-Api-Key: YOUR_API_KEY" http://YOUR_SERVER:9000/status
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
curl -X POST http://YOUR_SERVER:9000/webhook/test \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: YOUR_API_KEY" \
  -d '{}'
```

If you get a `401 Unauthorized` error, make sure the API key is correct. For Radarr/Sonarr webhook URLs, append `?apikey=YOUR_API_KEY` to the URL.

Check Radarr/Sonarr webhook test button and logs.

### Notifications not working

Use the "Test" button in Settings → Notifications to verify each service is configured correctly.

### TV library taking a long time

This is normal for large libraries. The scanner uses an efficient bulk API that fetches all episode files in 1-2 API calls instead of per-series, but parsing thousands of files still takes time. Check the logs for progress.

### State lost after container update

Ensure the state volume is mounted correctly:
```yaml
volumes:
  - /path/to/appdata/state:/data/state
```

The state directory must persist outside the container. On Unraid, use `/mnt/user/appdata/media-janitor/state:/data/state`.

## Updating

```bash
docker pull ghcr.io/gnegelow-caylent/media-janitor:latest
docker stop media-janitor && docker rm media-janitor
# Run docker run command again
```

> **Note**: Scan progress, replacement history, and daily counters persist across updates when the `/data/state` volume is mounted correctly.

## Unraid Installation

### Option 1: Add Template Repository (Recommended)

Add this repo directly in Unraid without waiting for Community Applications approval:

1. Go to **Settings → Docker → Template Repositories**
2. Add: `https://github.com/gnegelow-caylent/media-janitor`
3. Click **Save**
4. Go to **Apps** tab and search "media-janitor"

### Option 2: Community Applications

An XML template has been submitted to Unraid Community Applications. Once approved, you can install directly from the Apps tab.

## Plex Integration Features

When Plex is configured, Media Janitor provides additional features:

| Feature | Description |
|---------|-------------|
| Library refresh | Automatically refresh Plex library after file replacements |
| Watch-based prioritization | Scan frequently-watched content first |
| Quality upgrade suggestions | Find watched content that could be upgraded (`/report/upgrades`) |
| Playback issue detection | Find files that users started but abandoned (`/report/playback-issues`) |
| Orphan detection | Find files in Plex not in Radarr/Sonarr or vice versa (`/report/orphans`) |

### Plex API Endpoints

| Endpoint | Description |
|----------|-------------|
| `/report/upgrades` | Quality upgrade suggestions for watched content |
| `/report/playback-issues` | Potential playback issues based on viewing patterns |
| `/report/orphans` | Files that exist in only Plex or only Radarr/Sonarr |

## Roadmap / Backlog

Future features under consideration:

- **NZBGet/SABnzbd integration** - Validate files before import, catch bad downloads earlier
- **Storage optimization** - Find unwatched content taking up space
- **Transcode stats** - Show which files always require transcoding

## License

MIT
