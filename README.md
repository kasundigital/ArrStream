# ArrStream

ArrStream is a Docker-first media readiness service for Radarr and Sonarr libraries. It is designed to analyze media and optimize only files that are not ready for IPTV direct streaming, reducing the need for live transcoding.

> Current status: early MVP / foundation build. Authentication, first-run setup, Radarr/Sonarr connection testing, root-folder path verification, GPU detection/selection, dashboard shell and diagnostics API are implemented. Library scanning and FFmpeg conversion workers are the next milestone.

## Default port

- Web UI: `http://SERVER-IP:4321`
- Health API: `/api/v1/health`
- System API: `/api/v1/system`
- Issues API: `/api/v1/issues`
- OpenAPI: `/docs`

## Quick install with Docker Run

ArrStream is intended to be installed as a ready-made container. Once the public image is published, the normal install will be one `docker run` command.

### CPU / standard install

```bash
mkdir -p /opt/arrstream/config

docker run -d \
  --name arrstream \
  --restart unless-stopped \
  -p 4321:4321 \
  -e TZ=UTC \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  -v /opt/arrstream/config:/config \
  -v /data:/data \
  ghcr.io/kasundigital/arrstream:latest
```

Open:

```text
http://SERVER-IP:4321
```

Replace `/data` with the host path that contains the media paths used by Radarr/Sonarr.

Example:

```bash
-v /mnt/storage:/data
```

### NVIDIA GPU

The Docker host must have the NVIDIA driver and NVIDIA Container Toolkit installed. Expose the GPUs to ArrStream with `--gpus all`:

```bash
mkdir -p /opt/arrstream/config

docker run -d \
  --name arrstream \
  --restart unless-stopped \
  --gpus all \
  -p 4321:4321 \
  -e TZ=UTC \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  -v /opt/arrstream/config:/config \
  -v /data:/data \
  ghcr.io/kasundigital/arrstream:latest
```

ArrStream detects the GPUs exposed to the container. If multiple GPUs are available, the administrator can select one or more of them in **Settings → Hardware**.

### Intel / AMD GPU

Expose `/dev/dri` to the container:

```bash
mkdir -p /opt/arrstream/config

docker run -d \
  --name arrstream \
  --restart unless-stopped \
  --device /dev/dri:/dev/dri \
  -p 4321:4321 \
  -e TZ=UTC \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  -v /opt/arrstream/config:/config \
  -v /data:/data \
  ghcr.io/kasundigital/arrstream:latest
```

### Check status

```bash
docker ps --filter name=arrstream
docker logs -f arrstream
```

### Update ArrStream

```bash
docker pull ghcr.io/kasundigital/arrstream:latest
docker stop arrstream
docker rm arrstream
```

Then run the same `docker run` command again. The `/config` directory is persistent, so the setup and database remain available.

> The GHCR image still needs to be published before the one-command installation above can pull successfully. Until then, the repository can be built locally with the included Dockerfile.

## First-run wizard

The dashboard is unavailable until setup is completed.

1. Create the first administrator account.
2. Add and successfully test at least one Radarr or Sonarr instance.
3. ArrStream reads the instance root folders and checks that the same paths exist inside the ArrStream container.
4. Detect and select available GPU devices or choose CPU mode.
5. Select the IPTV direct-stream profile and automation settings.
6. Open the dashboard.

The first Radarr/Sonarr connection is mandatory. ArrStream does not allow normal dashboard use until at least one instance has connected successfully.

If a configured Arr instance later becomes unavailable, ArrStream reports an issue instead of forcing the administrator through first-run setup again.

## Radarr / Sonarr integration

ArrStream runs separately from Radarr and Sonarr. No Custom Script or webhook is required.

During setup the user provides:

```text
Radarr URL
Radarr API key

and/or

Sonarr URL
Sonarr API key
```

When the services share a Docker network, container DNS names can be used, for example:

```text
http://radarr:7878
http://sonarr:8989
```

ArrStream uses the APIs to discover library files and root folders, and periodically checks for new imports.

## Path mapping

Path mapping is a core ArrStream feature.

Use the same container-side path namespace across the downloader, Radarr, Sonarr and ArrStream whenever possible.

Recommended layout:

```text
/data/
├── downloads/
├── media/
│   ├── movies/
│   └── tv/
└── arrstream-work/
```

For example, if the real host storage is `/mnt/storage`, mount it into each application as `/data`:

```text
Radarr      /mnt/storage:/data
Sonarr      /mnt/storage:/data
Downloader  /mnt/storage:/data
ArrStream   /mnt/storage:/data
```

ArrStream then sees the same paths reported by Radarr/Sonarr:

```text
/data/media/movies
/data/media/tv
/data/downloads
```

During first-time setup ArrStream queries the current Radarr/Sonarr root folders and checks each path for:

- existence
- readability
- write access
- filesystem information
- hardlink capability where applicable

If Radarr reports `/movies` but ArrStream only has `/data/media/movies`, ArrStream reports a path mismatch and provides mapping guidance instead of starting conversions blindly.

## Hardware acceleration

ArrStream detects hardware devices exposed to its container.

Supported design targets:

- NVIDIA NVENC
- Intel Quick Sync / VAAPI
- AMD VAAPI
- CPU fallback with FFmpeg software encoders

If multiple GPUs are exposed, the administrator can select one or multiple GPUs. Conversion jobs are assigned to individual GPUs rather than trying to split one media file across several GPUs.

Planned hardware settings include:

- Automatic hardware selection
- Selected GPUs only
- CPU-only mode
- Per-GPU concurrent-job limits
- Automatic balancing
- CPU fallback
- GPU test before enabling an encoder

## IPTV direct-stream target

The initial recommended profile targets broadly compatible media:

- Container: MP4
- Video: H.264 / AVC
- Audio: AAC
- Pixel format: yuv420p
- Resolution: preserve source
- Frame rate: preserve source
- MP4 faststart: enabled

ArrStream will prefer the least expensive safe operation:

1. Skip if already compatible.
2. Remux when only the container is unsuitable.
3. Convert audio only when video can be copied.
4. Fully transcode video only when required.

The goal is to prepare the file once so IPTV/XUI servers can direct-stream it without repeatedly transcoding during playback.

## Processing model

ArrStream is a separate service and does not require Custom Scripts or webhooks in Radarr/Sonarr.

- Initial library scan discovers existing movie and episode files through the Radarr/Sonarr APIs.
- ArrStream stores file IDs and state locally in SQLite.
- Lightweight periodic polling discovers newly imported files.
- A periodic reconciliation scan catches missed changes.
- Existing libraries are scan-first; ArrStream will not blindly start thousands of transcodes on first installation.
- New imports can be analyzed and optimized automatically after Radarr/Sonarr imports them.

## ArrMedic integration

ArrStream exposes stable diagnostic APIs so ArrMedic can inspect it without accessing the ArrStream database directly.

Implemented foundation endpoints:

```text
GET /api/v1/health
GET /api/v1/system
GET /api/v1/issues
```

Planned endpoints include:

```text
GET /api/v1/paths
GET /api/v1/connections
GET /api/v1/jobs
GET /api/v1/history
GET /api/v1/readiness
GET /api/v1/stats/summary
```

This allows ArrMedic to identify issues such as path mismatches, inaccessible storage, unavailable GPU encoders, queue failures, conversion failures and unhealthy Arr connections.

## Dashboard goals

The main dashboard is designed to make the system status understandable at a glance.

It will show:

- total files discovered
- direct-stream-ready files
- files requiring optimization
- successful conversions
- failed conversions
- queued jobs
- current conversion progress
- animated progress bars
- current encoder / selected GPU
- conversion speed and ETA
- total input data processed
- total output data
- total disk space saved
- downloader activity / downloaded data where available
- Radarr/Sonarr connection health
- CPU, RAM and GPU health
- recent activity
- time/source/result/action/hardware filters

## Anonymous community statistics

A future optional telemetry feature can allow users to contribute anonymous aggregate statistics to the ArrStream website, such as:

- number of files processed
- bytes processed
- bytes saved
- successful/failed job counts
- conversion method counts
- general hardware type

This must be opt-in. ArrStream should never upload media names, episode/movie titles, file paths, API keys or other private library details.

## Development install

Until the public container image is published, developers can build directly from this repository:

```bash
git clone https://github.com/kasundigital/ArrStream.git
cd ArrStream
mkdir -p config

docker build -t kasundigital/arrstream:latest .

docker run -d \
  --name arrstream \
  --restart unless-stopped \
  -p 4321:4321 \
  -e SECRET_KEY="$(openssl rand -hex 32)" \
  -v "$(pwd)/config:/config" \
  -v /data:/data \
  kasundigital/arrstream:latest
```

## Security notes

Before exposing ArrStream outside a trusted network:

- set a strong unique `SECRET_KEY`
- put ArrStream behind HTTPS/reverse proxy
- do not expose Radarr/Sonarr API keys publicly
- restrict network access where possible
- keep `/config` persistent and protected

## License

A project license will be selected before the first public release.
