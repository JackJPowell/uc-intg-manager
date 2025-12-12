"""Discovery module for Zeroconf protocol."""

from typing import Any

from ucapi_framework import DiscoveredDevice
from ucapi_framework.discovery import MDNSDiscovery
from zeroconf import IPVersion


class ManagerDiscovery(MDNSDiscovery):
    """mDNS discovery for Manager devices."""

    def parse_mdns_service(self, service_info: Any) -> DiscoveredDevice | None:
        """
        Parse mDNS service info into DiscoveredDevice.

        :param service_info: mDNS service info object from zeroconf
        :return: DiscoveredDevice or None if parsing fails
        """
        if not service_info.parsed_addresses():
            return None

        # Get the first IPv4 address
        addresses = service_info.parsed_addresses(version=IPVersion.V4Only)
        address = addresses[0] if addresses else None

        if not address:
            return None

        # Extract name from service info (remove service suffix)
        name = service_info.name
        if name.endswith(self.service_type):
            name = name.replace(self.service_type, "")

        return DiscoveredDevice(
            identifier=name,
            name=name,
            address=address,
            extra_data={
                "port": service_info.port,
                "server": service_info.server,
                "properties": dict(service_info.properties)
                if service_info.properties
                else {},
            },
        )
