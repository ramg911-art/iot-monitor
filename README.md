# IoT Monitor

Production-grade single-user IoT monitoring platform with RTSP cameras, Tapo H100 hub, eWeLink Cloud, and real-time WebSocket updates.

## Features

- **RTSP Cameras** via go2rtc (WebRTC/MSE streaming)
- **Tapo H100 Hub** – temperature, humidity, door sensors (python-kasa)
- **Tapo WiFi Switches/Plugs** – on/off control, power monitoring
- **eWeLink Cloud** – OAuth, device sync, webhook-driven updates
- **WebSocket** – real-time dashboard updates
- **Sensor History** – temperature (0.2°C), door state, power (1W) rules
- **JWT Authentication** – single admin account
- **Docker** – FastAPI + go2rtc

## Quick Start

### Local Development

```bash
cd iot-monitor
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env - set JWT_SECRET, optionally eWeLink credentials
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Default login: `admin` / `admin` – change immediately.

### Docker

```bash
cp .env.example .env
# Edit .env
docker-compose up -d
```

Open http://localhost:8000

## Configuration

| Variable | Description |
|----------|-------------|
| `JWT_SECRET` | Secret for JWT signing (min 32 chars) |
| `ENCRYPTION_KEY` | Fernet key for eWeLink token storage |
| `EWELINK_CLIENT_ID` | From [dev.ewelink.cc](https://dev.ewelink.cc) |
| `EWELINK_CLIENT_SECRET` | App secret |
| `EWELINK_REDIRECT_URI` | e.g. `https://your-domain.com/api/ewelink/callback` |

### eWeLink Setup

1. Register at [dev.ewelink.cc](https://dev.ewelink.cc)
2. Create an app, set Redirect URI to `https://your-domain.com/api/ewelink/callback`
3. Add Client ID and Secret to `.env`
4. In the dashboard, click "Connect eWeLink" and complete OAuth
5. For webhook: create a scene in eWeLink Web that POSTs to `https://your-domain.com/api/ewelink/webhook` when devices change

### go2rtc (Cameras)

Edit `go2rtc.yaml`:

```yaml
streams:
  front_door: rtsp://user:pass@192.168.1.10:554/stream1
  garage: rtsp://admin:admin@192.168.1.11:554/cam/realmonitor
```

### Tapo

- **H100 Hub**: Add by IP in the dashboard (same flow as switches – auto-detects hub vs plug)
- **WiFi Switch/Plug**: Add by IP, test connectivity first. Some newer models (L510/L530, certain WiFi switches) require `TAPO_USERNAME` and `TAPO_PASSWORD` (TP-Link cloud email/password) in `.env`

## Cloudflare Tunnel

```bash
cloudflared tunnel create iot-monitor
# Edit cloudflare-tunnel.yml with tunnel ID
cloudflared tunnel run
```

## API

- `POST /api/auth/login` – Login
- `GET /api/devices` – List devices (JWT)
- `GET /api/devices/{id}/history?hours=24` – Sensor history
- `POST /api/tapo/add` – Add Tapo device by IP
- `POST /api/tapo/{id}/toggle` – Toggle Tapo switch/plug
- `GET /api/ewelink/login-url` – OAuth URL
- `POST /api/ewelink/sync` – Sync eWeLink devices
- `POST /api/ewelink/toggle` – Toggle eWeLink device
- `POST /api/ewelink/webhook` – eWeLink webhook (no auth)
- `GET /api/cameras/streams` – go2rtc streams
- `WS /ws?token=<jwt>` – WebSocket

## Project Structure

```
app/
  main.py           # FastAPI app, WebSocket
  config.py         # Settings
  database.py       # Async SQLAlchemy
  deps.py           # Auth dependency
  models/           # User, Device, SensorHistory, EwelinkToken
  routers/          # auth, devices, tapo, ewelink, cameras, admin
  services/         # auth, encryption, history, tapo, ewelink
  background/       # Polling tasks
  websocket/        # Connection manager
static/             # Frontend
go2rtc.yaml         # Camera streams
docker-compose.yml
```

## License

MIT
