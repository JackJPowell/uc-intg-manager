"""Notification service for sending notifications to various providers."""

from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Any

import aiohttp
import certifi

from notification_settings import (
    DiscordNotificationConfig,
    HomeAssistantNotificationConfig,
    NtfyNotificationConfig,
    PushoverNotificationConfig,
    WebhookNotificationConfig,
)

_LOG = logging.getLogger(__name__)


# Create SSL context with certifi certificates for HTTPS requests
def _get_ssl_context() -> ssl.SSLContext:
    """Get SSL context with certifi certificates."""
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    return ssl_context


class NotificationService:
    """Service for sending notifications to configured providers."""

    @staticmethod
    async def send_home_assistant(
        config: HomeAssistantNotificationConfig,
        title: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> bool:
        """
        Send notification to Home Assistant.

        Args:
            config: Home Assistant configuration
            title: Notification title
            message: Notification message
            data: Optional additional data

        Returns:
            True if successful, False otherwise
        """
        if not config.enabled or not config.url or not config.token:
            _LOG.warning("Home Assistant notifications not properly configured")
            return False

        url = f"{config.url.rstrip('/')}/api/services/notify/notify"
        headers = {
            "Authorization": f"Bearer {config.token}",
            "Content-Type": "application/json",
        }

        payload = {
            "title": title,
            "message": message,
        }
        if data:
            payload["data"] = data

        try:
            ssl_context = _get_ssl_context()
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    url, headers=headers, json=payload, timeout=10
                ) as resp:
                    if resp.status == 200:
                        _LOG.info("Notification sent to Home Assistant successfully")
                        return True
                    _LOG.error(
                        "Failed to send Home Assistant notification: %s %s",
                        resp.status,
                        await resp.text(),
                    )
                    return False
        except Exception as e:
            _LOG.error("Error sending Home Assistant notification: %s", e)
            return False

    @staticmethod
    async def send_webhook(
        config: WebhookNotificationConfig,
        title: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> bool:
        """
        Send notification via webhook.

        Args:
            config: Webhook configuration
            title: Notification title
            message: Notification message
            data: Optional additional data

        Returns:
            True if successful, False otherwise
        """
        if not config.enabled or not config.url:
            _LOG.warning("Webhook notifications not properly configured")
            return False

        headers = {"Content-Type": "application/json"}
        if config.headers:
            headers.update(config.headers)

        payload = {
            "title": title,
            "message": message,
            "timestamp": data.get("timestamp") if data else None,
        }
        if data:
            payload.update(data)

        try:
            ssl_context = _get_ssl_context()
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    config.url, headers=headers, json=payload, timeout=10
                ) as resp:
                    if resp.status in (200, 201, 202, 204):
                        _LOG.info("Notification sent via webhook successfully")
                        return True
                    _LOG.error(
                        "Failed to send webhook notification: %s %s",
                        resp.status,
                        await resp.text(),
                    )
                    return False
        except Exception as e:
            _LOG.error("Error sending webhook notification: %s", e)
            return False

    @staticmethod
    async def send_pushover(
        config: PushoverNotificationConfig,
        title: str,
        message: str,
        priority: int = 0,
    ) -> bool:
        """
        Send notification via Pushover.

        Args:
            config: Pushover configuration
            title: Notification title
            message: Notification message
            priority: Priority level (-2 to 2, default 0)

        Returns:
            True if successful, False otherwise
        """
        if not config.enabled or not config.user_key or not config.app_token:
            _LOG.warning("Pushover notifications not properly configured")
            return False

        url = "https://api.pushover.net/1/messages.json"
        payload = {
            "token": config.app_token,
            "user": config.user_key,
            "title": title,
            "message": message,
            "priority": priority,
        }

        try:
            ssl_context = _get_ssl_context()
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(url, data=payload, timeout=10) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        if result.get("status") == 1:
                            _LOG.info("Notification sent via Pushover successfully")
                            return True
                    _LOG.error(
                        "Failed to send Pushover notification: %s %s",
                        resp.status,
                        await resp.text(),
                    )
                    return False
        except Exception as e:
            _LOG.error("Error sending Pushover notification: %s", e)
            return False

    @staticmethod
    async def send_ntfy(
        config: NtfyNotificationConfig,
        title: str,
        message: str,
        priority: int = 3,
        tags: list[str] | None = None,
    ) -> bool:
        """
        Send notification via ntfy.

        Args:
            config: ntfy configuration
            title: Notification title
            message: Notification message
            priority: Priority level (1-5, default 3 = default priority)
            tags: Optional list of tags/emojis

        Returns:
            True if successful, False otherwise
        """
        if not config.enabled or not config.server or not config.topic:
            _LOG.warning("ntfy notifications not properly configured")
            return False

        url = f"{config.server.rstrip('/')}/{config.topic}"

        # Ensure priority is valid (1-5)
        priority = max(1, min(5, priority))

        headers = {
            "Title": title,
            "Priority": str(priority),
        }

        if tags:
            headers["Tags"] = ",".join(tags)

        if config.token:
            headers["Authorization"] = f"Bearer {config.token}"

        try:
            ssl_context = _get_ssl_context()
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    url, headers=headers, data=message.encode("utf-8"), timeout=10
                ) as resp:
                    if resp.status == 200:
                        _LOG.info("Notification sent via ntfy successfully")
                        return True
                    _LOG.error(
                        "Failed to send ntfy notification: %s %s",
                        resp.status,
                        await resp.text(),
                    )
                    return False
        except Exception as e:
            _LOG.error("Error sending ntfy notification: %s", e)
            return False

    @staticmethod
    async def send_discord(
        config: DiscordNotificationConfig,
        title: str,
        message: str,
        color: int = 5814783,  # Default blue color (0x58B9FF)
    ) -> bool:
        """
        Send notification to Discord via webhook.

        Args:
            config: Discord configuration
            title: Notification title
            message: Notification message
            color: Embed color (decimal, default is blue)

        Returns:
            True if successful, False otherwise
        """
        if not config.enabled or not config.webhook_url:
            _LOG.warning("Discord notifications not properly configured")
            return False

        # Discord webhook expects embeds format
        payload = {
            "content": f"**{title}**\n{message}",
            "embeds": [
                {
                    "title": title,
                    "description": message,
                    "color": color,
                    "footer": {"text": "Integration Manager"},
                }
            ],
        }

        try:
            ssl_context = _get_ssl_context()
            connector = aiohttp.TCPConnector(ssl=ssl_context)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    config.webhook_url, json=payload, timeout=10
                ) as resp:
                    if resp.status == 204:
                        _LOG.info("Notification sent to Discord successfully")
                        return True
                    _LOG.error(
                        "Failed to send Discord notification: %s %s",
                        resp.status,
                        await resp.text(),
                    )
                    return False
        except Exception as e:
            _LOG.error("Error sending Discord notification: %s", e)
            return False

    @staticmethod
    async def send_all(
        settings,
        title: str,
        message: str,
        data: dict[str, Any] | None = None,
        priority: int = 0,
    ) -> dict[str, bool]:
        """
        Send notification to all enabled providers.

        Args:
            settings: NotificationSettings instance
            title: Notification title
            message: Notification message
            data: Optional additional data
            priority: Priority level for applicable providers

        Returns:
            Dictionary mapping provider names to success status
        """
        results = {}

        tasks = []
        providers = []

        if settings.home_assistant.enabled:
            tasks.append(
                NotificationService.send_home_assistant(
                    settings.home_assistant, title, message, data
                )
            )
            providers.append("home_assistant")

        if settings.webhook.enabled:
            tasks.append(
                NotificationService.send_webhook(settings.webhook, title, message, data)
            )
            providers.append("webhook")

        if settings.pushover.enabled:
            tasks.append(
                NotificationService.send_pushover(
                    settings.pushover, title, message, priority
                )
            )
            providers.append("pushover")

        if settings.ntfy.enabled:
            tasks.append(
                NotificationService.send_ntfy(settings.ntfy, title, message, priority)
            )
            providers.append("ntfy")

        if settings.discord.enabled:
            tasks.append(
                NotificationService.send_discord(settings.discord, title, message)
            )
            providers.append("discord")

        if tasks:
            task_results = await asyncio.gather(*tasks, return_exceptions=True)
            for provider, result in zip(providers, task_results):
                if isinstance(result, Exception):
                    _LOG.error(
                        "Exception sending notification to %s: %s", provider, result
                    )
                    results[provider] = False
                else:
                    results[provider] = result
        else:
            _LOG.info("No notification providers enabled")

        return results
