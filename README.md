# ArrStream

ArrStream is a Docker-first media readiness service for Radarr and Sonarr libraries. It is designed to analyze media and optimize only files that are not ready for IPTV direct streaming, reducing the need for live transcoding.

> Current status: early MVP / foundation build. Authentication, first-run setup, Radarr/Sonarr connection testing, root-folder path verification, GPU detection/selection, dashboard shell and diagnostics API are implemented. Library scanning and FFmpeg conversion workers are the next milestone.

## Default port

- Web UI: `http://SERVER-IP:4321`
- Health API: `/api/v1/health`
- System API: `/api/v1/system`
- Issues API: `/api/v1/issues`
- OpenAPI: `/docs`

## Quick start

```bash
git clone https://github.com/kasundigital/ArrStream.git
cd ArrStream
mkdir -p config
export SECRET_KEY="$(openssl rand -hex 32)"
export DATA_PATH=/data
docker compose up -d --build
```

Open `http://SERVER-IP:4321`.

## First-run wizard

The dashboard is unavailable until setup is completed.

1. Create the first administrator account.
2. Add and successfully test at least one Radarr or Sonarr instance.
3. ArrStream reads the instance root folders and checks that the same paths exist inside the ArrStream container.
4. Detect and select available GPU devices or choose CPU mode.
5. Select the IPTV direct-stream profile and automation settings.
6. Open the dashboard.

If a configured Arr instance later becomes unavailable, ArrStream should report an issue instead of forcing the administrator through first-run setup again.

## Recommended Docker path layout

Use the same path namespace across downloader, Radarr, Sonarr and ArrStream whenever possible:

```text
/data/
├── downloads/
├── media/
│   ├── movies/
│   └── tv/
└── arrstream-work/
```

Example volume mapping:

```yaml
volumes:
  - /data:/data
```

ArrStream queries Radarr/Sonarr root folders and checks whether those paths are readable and writable inside its own container.

## Hardware acceleration

ArrStream detects GPUs that are exposed to the container.

### NVIDIA

Install NVIDIA Container Toolkit on the Docker host and add:

```yaml
gpus: all
```

### Intel / AMD

Expose the render devices:

```yaml
devices:
  - /dev/dri:/dev/dri
```

The setup wizard supports selecting one or multiple detected GPUs. The planned scheduler assigns each conversion job to one selected GPU and can fall back to CPU.

## IPTV direct-stream target

The initial recommended profile targets broadly compatible media:

- Container: MP4
- Video: H.264 / AVC
- Audio: AAC
- Pixel format: yuv420p
- Resolution: preserve source
- Frame rate: preserve source
- MP4 faststart: enabled

The conversion engine will prefer the least expensive safe operation:

1. Skip if already compatible.
2. Remux when only the container is unsuitable.
3. Convert audio only when video can be copied.
4. Fully transcode video only when required.

## Planned processing model

ArrStream is a separate service and does not require Custom Scripts or webhooks in Radarr/Sonarr.

- Initial library scan discovers existing movie and episode files through the Radarr/Sonarr APIs.
- ArrStream stores file IDs and state locally in SQLite.
- Lightweight periodic polling discovers newly imported files.
- A periodic reconciliation scan catches missed changes.
- Existing libraries are scan-first; ArrStream will not blindly start thousands of transcodes on first installation.

## ArrMedic integration

ArrStream exposes stable diagnostic APIs so ArrMedic can inspect it without accessing the ArrStream database directly.

Implemented foundation endpoints:

```text
GET /api/v1/health
GET /api/v1/system
GET /api/v1/issues
```

Planned endpoints include paths, connections, jobs, history, readiness and statistics.

## Dashboard goals

The dashboard is being designed to show the important state on one screen:

- processed files
- successful / failed / queued jobs
- direct-stream readiness
- current conversion progress
- animated progress bars
- selected encoder / GPU
- storage saved
- downloaded data
- Radarr/Sonarr connection health
- CPU and memory status
- recent activity and filters

## Security notes

Before exposing ArrStream outside a trusted network:

- set a strong unique `SECRET_KEY`
- put ArrStream behind HTTPS/reverse proxy
- do not expose Radarr/Sonarr API keys publicly
- restrict network access where possible

## License

A project license will be selected before the first public release.
