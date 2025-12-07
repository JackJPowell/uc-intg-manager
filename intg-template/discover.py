"""
Device Discovery Module.

This module handles automatic device discovery on the local network.
It uses mDNS (Multicast DNS) by default, but can be
modified to use other discovery protocols like SSDP, SDDP, etc.

TODO: Customize the discovery logic for your device's discovery protocol.

:license: Mozilla Public License Version 2.0, see LICENSE for more details.
"""

import logging
from typing import Any

from ucapi_framework import DiscoveredDevice
from ucapi_framework.discovery import MDNSDiscovery

_LOG = logging.getLogger(__name__)


# TODO: Update to your specific device discovery base class
class DeviceDiscovery(MDNSDiscovery):
    """
    Discover devices on the local network.

    This class uses mDNS (Multicast DNS) for device discovery. If your device uses a
    different discovery protocol, you can:
    - Use SSDPDiscovery for SSDP/UPnP devices
    - Use SDDPDiscovery for SDDP devices
    - Implement a custom discovery class

    TODO: Modify this class to match your device's discovery protocol.
    """

    def parse_mdns_service(self, service_info: Any) -> DiscoveredDevice | None:
        """
        Parse response into DiscoveredDevice.
        """
        pass
