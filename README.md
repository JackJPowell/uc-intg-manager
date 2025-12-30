[![Discord](https://badgen.net/discord/online-members/zGVYf58)](https://discord.gg/zGVYf58)
![GitHub Release](https://img.shields.io/github/v/release/jackjpowell/uc-intg-manager)
![GitHub Downloads (all assets, all releases)](https://img.shields.io/github/downloads/jackjpowell/uc-intg-manager/total)
![Maintenance](https://img.shields.io/maintenance/yes/2025.svg)
[![Buy Me A Coffee](https://img.shields.io/badge/Buy_Me_A_Coffee%20☕-FFDD00?logo=buy-me-a-coffee&logoColor=white&labelColor=555)](https://buymeacoffee.com/jackpowell)


# Unfolded Circle Integration Manager

A web-based integration manager for [Unfolded Circle Remote Two/3](https://www.unfoldedcircle.com/). This integration provides a convenient web interface to manage your custom integrations, automatically check for updates, install new integrations, and backup integration config files. And it runs directly on your remote.

<img width="800" height="694" alt="manager" src="https://github.com/user-attachments/assets/9f51dc19-4acd-457d-9591-ed62e4415a7a" />




## Features

### Release Notes and Version Management

Access comprehensive release information directly from the web interface.

- **Release Notes Viewer**: Click on any version number to view detailed release notes from GitHub
- **Beta Release Support**: Enable beta releases in settings to see and install pre-release versions
- **Version Selection**: Choose specific versions to install or downgrade to any previous version
- **Rollback Capability**: Install previous versions if needed, with full downgrade support
- **Flexible Version Management**: Select any available version from the dropdown menu, not just the latest

<img width="500" alt="release" src="https://github.com/user-attachments/assets/1fcf8253-b1f1-468d-9894-4e4493680bce" /> <img width="500" height="257" alt="version" src="https://github.com/user-attachments/assets/bf4aaf89-e96e-4a82-9aa3-d9a5591d0862" />





### Automatic Updates with Configuration Preservation

The Integration Manager can automatically detect when newer versions of your custom integrations are available on GitHub and update them while preserving your existing configuration.

- **Automatic Update Detection**: Periodically checks GitHub releases for new versions
- **One-Click Updates**: Update integrations directly from the web interface
- **Configuration Backup & Restore**: Automatically backs up integration settings before updating and restores them after installation (for integrations that support the backup feature)
- **Universal Update Support**: Update any integration, even those without backup/restore support (will require reconfiguration)
- **Version Tracking**: View current and available versions for all installed integrations
- **Beta Update Options**: Receive update notifications for beta releases when enabled in settings

> [!NOTE]
> For integrations that support the backup/restore API, configuration is automatically preserved during updates. Integrations without this support can still be updated, but will require manual reconfiguration afterward. The update button indicates which method applies.

### Integration Management

Full lifecycle management for your custom integrations.

- **Delete Integrations**: Remove installed integrations directly from the web interface
- **One-Click Installation**: Install new integrations from the community registry
- **Update Control**: Update to latest, select specific versions, or choose beta releases
- **Safe Uninstall**: Clean removal of integration drivers and configurations

<img width="500" alt="delete" src="https://github.com/user-attachments/assets/dd7b0604-09d8-4647-9b24-be69cb99df2c" />

### Available Integration Registry

Browse and install integrations from the community registry with a single click.

- **Searchable Registry**: View all available integrations from the Unfolded Circle community
- **Category Filtering**: Filter integrations by category (media, lighting, climate, etc.)
- **Detailed Information**: See integration descriptions, developers, versions, and GitHub links
- **One-Click Installation**: Install new integrations directly from the web interface
  
<img width="800" alt="available" src="https://github.com/user-attachments/assets/41b4352d-13d8-4a78-9fca-e6575240b573" />

### Automated Configuration Backups

Protect your integration configurations with automatic scheduled backups.

- **Scheduled Backups**: Configure automatic daily backups at a specified time
- **Manual Backups**: Trigger immediate backups of all integrations or individual ones
- **Backup Viewing**: View and manage all saved configuration snapshots
- **Export & Import**: Download complete backup files for safekeeping or transfer to another Remote
- **Per-Integration Backups**: Each integration's configuration is backed up separately with timestamps

Backups are stored locally on the Remote and can be exported as JSON files for external storage.

### Settings & Configuration

Customize the Integration Manager's behavior through the Settings page:

- **Shutdown on Battery**: Automatically stop the web server when the Remote is on battery power to conserve energy (default: enabled)
- **Automatic Updates**: Enable automatic installation of integration updates when detected (default: disabled - manual confirmation required)
- **Show Beta Releases**: Display and allow installation of pre-release versions from GitHub (default: disabled)
- **Automatic Backups**: Enable scheduled daily backups of integration configurations (default: disabled)
- **Backup Time**: Set the time of day for automatic backups (24-hour format, e.g., "02:00")
  
<img width="800" alt="settings" src="https://github.com/user-attachments/assets/14b1b4b1-c8dc-43e9-b3bf-4741873f788b" />

### Integrated Log Viewer

View real-time logs directly in the web interface.

- **Manager Logs**: View Integration Manager logs in real-time with filtering by log level (INFO, WARNING, ERROR)
- **Integration Logs**: Access individual integration logs from the main integration list for easier debugging
- **Live Log Streaming**: Logs update automatically as new entries are added
- **Clear Logs**: Clear the current log buffer with one click
- **Diagnostic Information**: Helpful for troubleshooting issues
  
<img width="800" alt="logs" src="https://github.com/user-attachments/assets/f09bb6ff-8974-4715-8c66-f5b402c83fca" />

### Notifications

Stay informed about integration updates, errors, and new integrations with multiple notification options.

- **Multiple Notification Services**: Choose from Discord, Home Assistant, ntfy, Pushover, or generic Webhooks
- **Update Notifications**: Get notified when updates are available for your integrations
- **Error Notifications**: Receive alerts when integrations enter an error state
- **New Integration Alerts**: Be informed when new integrations are added to the registry
- **Customizable Options**: Enable/disable specific notification types based on your preferences
- **Test Notifications**: Verify your notification service configuration with a test message

#### Setting Up Notifications

1. Navigate to **Notifications** in the sidebar
2. Click **Configure Services** to add a notification service:
   - **Discord**: Enter your webhook URL from Discord server settings
   - **Home Assistant**: Provide your Home Assistant URL and long-lived access token
   - **ntfy**: Configure your ntfy server URL and topic (supports self-hosted or ntfy.sh)
   - **Pushover**: Enter your user key and application token
   - **Webhook**: Set up a custom webhook URL with optional authentication
3. Configure **Notification Options**:
   - Enable/disable notifications for updates, errors, and new integrations
   - Multiple services can be enabled simultaneously
4. Use **Test Notification** to verify your configuration
5. Backups are automatically included when exporting settings

### Power-Aware Operation

The web server automatically starts when your Remote is docked and shuts down when on battery (configurable).

- **Automatic Start/Stop**: Web server lifecycle tied to dock status
- **Battery Conservation**: No background processes draining battery during mobile use
- **Status Indicators**: Dashboard shows current dock status and server state






## Installation

### Option 1: Install on Remote

1. Download the latest release archive (`.tar.gz`) from the [Releases](https://github.com/JackJPowell/uc-intg-manager/releases) page
2. Upload and install via the Web Configurator:
   - Go to **Settings** → **Integrations & Docks** → **Custom Integrations**
   - Click **Upload** and select the downloaded archive
3. Configure the integration:
   - The integration will automatically discover your Remote using mDNS
   - **Web Configurator PIN**: Enter the PIN from Settings → Profile → Web Configurator
   - If discovery fails, you'll be prompted to manually enter:
     - **IP Address**: Your Remote's IP address (e.g., `192.168.1.100`)
     - **Web Configurator PIN**: The PIN from Settings → Profile → Web Configurator
   - Note: The PIN is only required during initial setup. An API key will be created and used for subsequent authentication.
4. Access the web interface at `http://<remote-ip>:8088` when docked

### Option 2: Run in Docker

You can run the Integration Manager as a Docker container on an external server to manage your Remote.

#### Using docker run

```bash
docker run -d \\
  --name uc-intg-manager \\
  --network host \\
  -e UC_INTG_MANAGER_HTTP_PORT=8088 \\
  -e UC_CONFIG_HOME=/config \\
  -v uc-intg-manager:/config \\
  ghcr.io/jackjpowell/uc-intg-manager:latest
```

#### Using docker-compose

```yaml
version: '3.8'

services:
  uc-intg-manager:
    image: ghcr.io/jackjpowell/uc-intg-manager:latest
    container_name: uc-intg-manager
    network_mode: host
    environment:
      - UC_INTG_MANAGER_HTTP_PORT=8088
      - UC_CONFIG_HOME=/config
    volumes:
      - uc-intg-manager:/config
    restart: unless-stopped
```

#### Environment Variables

| Variable | Description | Default | Required |
|----------|-------------|---------|----------|
| `UC_INTG_MANAGER_HTTP_PORT` | HTTP port for Integration Manager Web Server | `8088` | No|
| `UC_CONFIG_HOME` | Configuration directory path | `/config` | No |
| `UC_INTEGRATION_INTERFACE` | Network interface to bind integration API | `0.0.0.0` | No |
| `UC_INTEGRATION_HTTP_PORT` | HTTP port for integration API | `9090` | No |


## Usage

### Accessing the Web Interface

1. Ensure your Remote is docked (if running on the Remote itself)
2. Open a browser and navigate to `http://<remote-ip>:8088`
3. That's it!

### Managing Integrations

- **Your Integrations**: View all installed integrations with status, version, and helpful links
- **Available Integrations**: Browse the community registry and install new integrations
- **Settings**: Configure automatic updates, manage backups, and other preferences
- **Logs**: View real-time integration manager logs for diagnostics

### Updating an Integration

1. Navigate to **Your Integrations**
2. If an update is available, you'll see an "Update Available" badge
3. Click the **Update** button to install the latest version, or use the dropdown to:
   - Select a specific version to install
   - View release notes for any version
   - Choose an alternate update method (with or without entity re-registration)
4. The manager will:
   - Backup the current configuration
   - Download the selected release from GitHub
   - Uninstall the old version
   - Install the new version
   - Restore the configuration

### Installing a New Integration

1. Navigate to **Available Integrations**
2. Browse or search for the integration you want
3. Click the **Install** button to install the latest version, or use the dropdown to select a specific version
4. The integration will be downloaded from GitHub and installed
5. Configure it through the Remote's normal integration setup

### Deleting an Integration

1. Navigate to **Your Integrations** or **Available Integrations**
2. Click the trash icon on any installed integration
3. Confirm the deletion in the modal dialog
4. The integration driver will be removed from your Remote

### Managing Backups

1. Navigate to **Settings**
2. In the **Export & Import Data** section:
   - **Export Backup File**: Download a complete backup including all integration configs and settings
   - **Import Backup File**: Upload a previously exported backup to restore everything
3. In the **Integration Configurations** section:
   - **Capture Configs Now**: Immediately backup all integration configurations
   - **View Saved Configs**: See all stored backups with timestamps
4. Enable **Automatic Backups** and set a time for daily scheduled backups

## Development

### Prerequisites

- Python 3.11 or later
- pip or uv package manager

### Local Development Setup

Clone the repository:

```bash
git clone https://github.com/JackJPowell/uc-intg-manager.git
cd uc-intg-manager
```

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\\Scripts\\activate
```

or using uv

```bash
uv venv
source .venv/bin/activate  # On Windows: .venv\\Scripts\\activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```
or using uv

```bash
uv pip install -r requirements.txt
```

Run the driver:

```bash
python -m intg-manager\driver.py
```

Access the web interface at `http://localhost:8088`


## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add some amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License

This project is licensed under the Mozilla Public License 2.0 - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- [Unfolded Circle](https://www.unfoldedcircle.com/) for the amazing Remote devices
- The Unfolded Circle community for inspiration and feedback

## Related Projects

- [ucapi](https://github.com/unfoldedcircle/integration-python-library) - Python integration library for building custom integrations
- [core-api](https://github.com/unfoldedcircle/core-api) - Official Remote Core API documentation
- [ucapi-framework](https://github.com/jackjpowell/ucapi-framework) - Unofficial python ucapi framework
