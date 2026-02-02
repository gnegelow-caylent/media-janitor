# Media Janitor

Proactive media library quality monitor for Plex/Radarr/Sonarr. Automatically detects and replaces bad media files.

## What It Does

- **Validates new imports** - Checks every file imported by Radarr/Sonarr via webhooks
- **Background scans** - Continuously validates your existing library over time
- **Auto-replaces bad files** - Deletes corrupt files and triggers re-downloads
- **Daily email reports** - Summary of what was scanned, found, and fixed

## What It Detects

| Problem | Detection Method |
|---------|-----------------|
| Corrupt files | ffprobe fails to read metadata |
| Wrong duration | File claims to be 300h when it should be 30m |
| Truncated files | ffmpeg decode test fails |
| Encoding errors | ffmpeg reports errors during decode |
| Low bitrate | Suspiciously low bitrate for resolution |

## Quick Start

### 1. Clone and Configure

```bash
git clone https://github.com/YOUR_USERNAME/media-janitor.git
cd media-janitor
cp config.example.yaml config.yaml
```

Edit `config.yaml` with your settings:

```yaml
radarr:
  - name: "radarr"
    url: "http://radarr:7878"  # Or your Radarr IP/hostname
    api_key: "your-api-key"    # Settings → General → API Key

sonarr:
  - name: "sonarr-main"
    url: "http://sonarr:8989"
    api_key: "your-api-key"
  - name: "sonarr-4k"
    url: "http://sonarr-4k:8990"
    api_key: "your-api-key"

email:
  enabled: true
  smtp_host: "smtp.gmail.com"
  smtp_port: 587
  smtp_user: "you@gmail.com"
  smtp_password: "your-app-password"  # Use Gmail App Password
  from_address: "you@gmail.com"
  to_address: "you@gmail.com"
```

### 2. Update docker-compose.yml

Edit the volume mounts to match your media paths:

```yaml
volumes:
  - ./config.yaml:/data/config.yaml:ro
  - ./logs:/data/logs
  # Your media paths - must match what Radarr/Sonarr see
  - /mnt/nas1/movies:/mnt/nas1/movies:ro
  - /mnt/nas1/tv:/mnt/nas1/tv:ro
  - /mnt/nas2/movies:/mnt/nas2/movies:ro
```

### 3. Run

```bash
docker-compose up -d
```

### 4. Configure Webhooks in Radarr/Sonarr

In each Radarr/Sonarr instance:

1. Go to **Settings → Connect**
2. Add a new **Webhook** connection
3. Set URL to: `http://media-janitor:9000/webhook/radarr` (or `/webhook/sonarr`)
4. Enable **On Import** and **On Upgrade**
5. Test and Save

## Configuration Reference

### Validation Settings

```yaml
validation:
  # Quick metadata checks
  check_duration_sanity: true
  max_duration_hours: 12        # Flag files longer than this

  # Bitrate checks
  check_bitrate: true
  min_bitrate_720p: 1500        # kbps
  min_bitrate_1080p: 3000
  min_bitrate_4k: 8000

  # Deep validation (slower but catches more)
  deep_scan_enabled: true
  sample_duration_seconds: 30   # Test this many seconds at start/middle/end

  # Full decode (very slow, most thorough)
  full_decode_enabled: false    # Only if you have CPU to spare
```

### Scanner Settings

```yaml
scanner:
  enabled: true
  files_per_hour: 100           # Throttle scanning
  # schedule: "0 2 * * *"       # Optional: only run at 2 AM
```

### Action Settings

```yaml
actions:
  auto_replace: true            # Auto delete and re-download bad files
  blocklist_bad_releases: true  # Prevent re-downloading same bad release
  max_replacements_per_day: 10  # Bandwidth control
```

## Unraid Setup

1. In Unraid, go to **Docker** → **Add Container**
2. Set repository to the built image or use docker-compose via Portainer
3. Add volume mappings for your media shares
4. Ensure the container can reach Radarr/Sonarr (same custom network)

### Bandwidth Limiting on Unraid

You can limit bandwidth in several ways:

1. **In Media Janitor**: Set `max_replacements_per_day` in config
2. **In your download client**: Set speed limits in qBittorrent/SABnzbd
3. **In Docker**: Add CPU/memory limits in container settings

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check |
| `POST /webhook/radarr` | Radarr webhook receiver |
| `POST /webhook/sonarr` | Sonarr webhook receiver |

## Logs

Logs are written to `/data/logs/media-janitor.log` (or `./logs/` on host).

View logs:
```bash
docker logs media-janitor
# or
tail -f logs/media-janitor.log
```

## Troubleshooting

### Files not being validated

- Check the container can access the media files (volume mounts)
- Ensure file paths in Radarr/Sonarr match paths inside the container
- Check logs for errors

### Webhooks not working

- Test with: `curl -X POST http://localhost:9000/webhook/test -d '{}'`
- Check Radarr/Sonarr webhook test button
- Ensure containers are on same Docker network

### Email not sending

- Use Gmail App Password (not regular password)
- Check SMTP settings
- Test with less secure mail servers first

## License

MIT
