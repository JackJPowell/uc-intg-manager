# Unfolded Circle Integration Template

A template repository for creating [Unfolded Circle Remote Two/3](https://www.unfoldedcircle.com/) integration drivers using the [ucapi-framework](https://github.com/jackjpowell/ucapi-framework).

## Getting Started

1. **Clone or use this template** to create your own integration repository
2. **Rename the integration folder** from `intg-template` to `intg-yourdevice`
3. **Update the following files** with your device-specific information:
   - `driver.json` - Integration metadata (name, description, developer info)
   - `intg-template/const.py` - Device configuration and constants
   - `intg-template/device.py` - Device communication logic
   - `intg-template/media_player.py` - Media player entity implementation
   - `intg-template/setup.py` - Setup flow and configuration forms
   - `intg-template/discover.py` - Device discovery (if applicable)

## Project Structure

```
├── driver.json              # Integration metadata and configuration
├── intg-template/           # Main integration code (rename this folder)
│   ├── const.py             # Constants and device configuration dataclass
│   ├── device.py            # Device communication and state management
│   ├── discover.py          # Network device discovery
│   ├── driver.py            # Main entry point
│   ├── media_player.py      # Media player entity
│   └── setup.py             # Setup flow and user configuration
├── config/                  # Runtime configuration storage
├── Dockerfile               # Container build configuration
└── requirements.txt         # Python dependencies
```

## Development

### Prerequisites

- Python 3.11+
- Docker (optional, for containerized deployment)

### Local Development

1. Create a virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Run the integration:
   ```bash
   python intg-template/driver.py
   ```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `UC_LOG_LEVEL` | Logging level (DEBUG, INFO, WARNING, ERROR) | `DEBUG` |
| `UC_CONFIG_HOME` | Configuration directory path | `/config` |
| `UC_INTEGRATION_INTERFACE` | Network interface to bind | `0.0.0.0` |
| `UC_INTEGRATION_HTTP_PORT` | HTTP port for the integration | `9090` |
| `UC_DISABLE_MDNS_PUBLISH` | Disable mDNS advertisement | `false` |

## Deployment

### Install on Remote

1. Build the integration package (tar.gz file)
2. Upload via the Remote's web configurator under Integrations
3. Configure your device through the setup wizard

### Docker

```bash
docker run -d \
  --name=uc-intg-yourdevice \
  --network host \
  -v $(pwd)/config:/config \
  --restart unless-stopped \
  ghcr.io/yourusername/uc-intg-yourdevice:latest
```

### Docker Compose

```yaml
services:
  uc-intg-yourdevice:
    image: ghcr.io/yourusername/uc-intg-yourdevice:latest
    container_name: uc-intg-yourdevice
    network_mode: host
    volumes:
      - ./config:/config
    restart: unless-stopped
```

## Customization Guide

### Adding Entity Types

The template includes a Media Player entity. To add additional entity types:

1. Create a new entity file (e.g., `light.py`, `switch.py`, `climate.py`)
2. Import and add the entity class to `driver.py`:
   ```python
   driver = BaseIntegrationDriver(
       device_class=Device, entity_classes=[DeviceMediaPlayer, DeviceLight]
   )
   ```

### Implementing Device Communication

In `device.py`, implement the communication methods for your device:

1. `connect()` - Establish connection to the device
2. `disconnect()` - Clean up connection
3. `verify_connection()` - Check device availability and get current state
4. Device-specific methods (power control, volume, etc.)

### Customizing the Setup Flow

In `setup.py`, modify the `_MANUAL_INPUT_SCHEMA` to add fields for your device's configuration (IP, port, credentials, etc.).

## Resources

- [UC Integration Python Library](https://github.com/aitatoi/integration-python-library)
- [UCAPI Framework](https://github.com/jackjpowell/ucapi-framework)
- [Unfolded Circle Developer Documentation](https://github.com/unfoldedcircle/core-api)

## License

Mozilla Public License Version 2.0 - see [LICENSE](LICENSE) for details.
