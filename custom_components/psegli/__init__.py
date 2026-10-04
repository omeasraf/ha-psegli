"""The PSEG Long Island integration."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import pytz

from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.components.recorder import get_instance
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, CONF_USERNAME, CONF_PASSWORD, CONF_COOKIE, CONF_MFA_METHOD
from .psegli import InvalidAuth, PSEGLIClient
from .exceptions import PSEGLIError
from .auto_login import (
    MFA_REQUIRED,
    check_addon_health,
    complete_mfa_login,
    get_fresh_cookies,
    refresh_saved_session,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]
COOKIE_REFRESH_INTERVAL_SECONDS = 10 * 60
_COOKIE_REFRESH_LISTENER_KEY = f"{DOMAIN}_cookie_refresh_unsub"
_COOKIE_REFRESH_USERS_KEY = f"{DOMAIN}_cookie_refresh_users"
_COOKIE_REFRESH_LOCK_KEY = f"{DOMAIN}_cookie_refresh_lock"

async def get_last_cumulative_kwh(hass: HomeAssistant, statistic_id: str, before_timestamp: datetime) -> float:
    """Get the last recorded cumulative kWh for a given statistic_id BEFORE a specific timestamp."""
    try:
        from homeassistant.components.recorder.statistics import statistics_during_period
        
        # Look for statistics in a window BEFORE our timestamp to find the last cumulative sum
        # Use a 7-day lookback window to ensure we find data even for longer backfills
        lookback_start = before_timestamp - timedelta(days=7)
        
        # Ensure timestamps are timezone-aware for consistent comparison
        if before_timestamp.tzinfo is None:
            before_timestamp = before_timestamp.replace(tzinfo=timezone.utc)
        if lookback_start.tzinfo is None:
            lookback_start = lookback_start.replace(tzinfo=timezone.utc)
        
        try:
            # Get statistics in the lookback window
            stats_in_window = await get_instance(hass).async_add_executor_job(
                statistics_during_period,
                hass,
                lookback_start,  # Start 7 days before our target timestamp
                before_timestamp,  # End at our target timestamp
                [statistic_id],    # Only get our specific statistic
                "hour",            # Hourly granularity
                None,              # No additional filters
                {"start", "sum"}   # Need start time and sum values
            )
        except Exception as e:
            _LOGGER.error("Error calling statistics_during_period: %s", e)
            stats_in_window = None
        
        if stats_in_window and statistic_id in stats_in_window and stats_in_window[statistic_id]:
            # Find the most recent statistic BEFORE our timestamp
            valid_stats = []
            for stat in stats_in_window[statistic_id]:
                if 'sum' in stat and stat['sum'] is not None and 'start' in stat:
                    # Handle both string ISO format and float Unix timestamp
                    if isinstance(stat['start'], str):
                        stat_time = datetime.fromisoformat(stat['start'])
                        # Ensure timezone awareness
                        if stat_time.tzinfo is None:
                            stat_time = stat_time.replace(tzinfo=timezone.utc)
                    elif isinstance(stat['start'], (int, float)):
                        stat_time = datetime.fromtimestamp(stat['start'], tz=timezone.utc)
                    else:
                        _LOGGER.warning("Unexpected start time format: %s (type: %s)", stat['start'], type(stat['start']))
                        continue
                    
                    # Ensure both timestamps are timezone-aware for comparison
                    if stat_time.tzinfo is None:
                        stat_time = stat_time.replace(tzinfo=timezone.utc)
                    
                    if stat_time < before_timestamp:
                        valid_stats.append((stat_time, stat['sum']))
            
            if valid_stats:
                # Sort by time and get the most recent one
                valid_stats.sort(key=lambda x: x[0])
                most_recent_time, most_recent_sum = valid_stats[-1]
                
                _LOGGER.debug("Found last cumulative sum: %.6f for %s at %s (before %s)", 
                             most_recent_sum, statistic_id, most_recent_time, before_timestamp)
                return most_recent_sum
            else:
                _LOGGER.debug("No valid statistics found before %s for %s", before_timestamp, statistic_id)
                return 0.0
        else:
            _LOGGER.debug("No statistics found in lookback window for %s", statistic_id)
            return 0.0
            
    except Exception as e:
        _LOGGER.warning("Could not get last statistics for %s: %s, starting from 0", statistic_id, e)
        return 0.0

async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the PSEG Long Island component."""
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PSEG Long Island from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    
    # Get credentials from config entry
    username = entry.data.get(CONF_USERNAME)
    password = entry.data.get(CONF_PASSWORD)
    cookie = entry.data.get(CONF_COOKIE, "")
    
    if not username or not password:
        _LOGGER.error("No username/password provided")
        return False
    
    # If no cookie available, try to get one from the addon
    mfa_pending = False
    if not cookie:
        _LOGGER.debug("No cookie available, attempting to get fresh cookies from addon...")
        try:
            mfa_method = entry.data.get(CONF_MFA_METHOD, "sms")
            cookies = await get_fresh_cookies(username, password, mfa_method=mfa_method)
            
            if cookies and cookies != MFA_REQUIRED:
                # Cookies are already in string format from addon
                cookie_string = cookies
                cookie = cookie_string  # Update local for rest of setup
                _LOGGER.debug("Successfully obtained fresh cookies from addon")
                
                # Store cookie in config entry for future use
                hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_COOKIE: cookie_string},
                )
            elif cookies == MFA_REQUIRED:
                # Allow setup to complete - user must call enter_mfa_code with the code
                mfa_pending = True
                _LOGGER.info("PSEG requires MFA - setup will complete; call enter_mfa_code with the code")
                channel = "phone" if mfa_method == "sms" else "email"
                await hass.async_create_task(
                    hass.services.async_call(
                        "persistent_notification",
                        "create",
                        {
                            "title": "PSEG Integration: MFA Required",
                            "message": f"PSEG sent a verification code to your {channel}. Go to Developer Tools > Actions and call 'PSEG Long Island: Enter MFA Code' with the code.",
                            "notification_id": "psegli_mfa_required",
                        },
                    )
                )
            else:
                _LOGGER.warning("Addon not available or failed to get cookies")
        except Exception as e:
            _LOGGER.warning("Failed to get cookies from addon: %s", e)
    
    # Fail only if we have no cookie and MFA isn't pending (addon unavailable or login failed)
    if not cookie and not mfa_pending:
        _LOGGER.error("No cookie available and addon failed to provide one. Start the addon and try again, or configure a cookie manually.")
        await hass.async_create_task(
            hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "PSEG Integration: Cookie Required",
                    "message": "No authentication cookie available. Ensure the PSEG addon is running, then try again. Or go to Settings > Integrations > PSEG Long Island > Configure to provide a cookie manually.",
                    "notification_id": "psegli_cookie_required",
                },
            )
        )
        return False
    
    # Create client (cookie may be empty if MFA pending - enter_mfa_code will update it)
    client = PSEGLIClient(cookie or "")
    hass.data[DOMAIN][entry.entry_id] = client
    
    # Test connection only when we have a cookie (skip when MFA pending)
    if cookie:
        try:
            await client.test_connection()
            _LOGGER.debug("PSEG connection test successful")
        except InvalidAuth as e:
            _LOGGER.error("Authentication failed: %s", e)
            refreshed_cookie = await refresh_saved_session(
                cookie, username=username or "", password=password or "", allow_login=True
            )
            if not refreshed_cookie:
                raise ConfigEntryNotReady(
                    "PSEG session recovery is pending; automatic login retries are limited by the add-on"
                ) from e
            cookie = refreshed_cookie
            client.update_cookie(cookie)
            await client.test_connection()
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, CONF_COOKIE: cookie},
            )
        except PSEGLIError as e:
            raise ConfigEntryNotReady("PSEG dashboard is temporarily unavailable") from e
    
    # Create coordinator for automatic updates (like Opower)
    coordinator = PSEGCoordinator(hass, entry, client)
    entry.runtime_data = coordinator
    
    # Store coordinator reference in hass.data for proper cleanup
    if DOMAIN not in hass.data:
        hass.data[DOMAIN] = {}
    hass.data[DOMAIN]['coordinator'] = coordinator
    
    # Listen for config changes (when user updates cookie via options)
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    
    # Register manual service for backfilling
    async def async_update_statistics_manual(call: Any) -> None:
        """Manually update statistics table with PSEG data (for backfilling)."""
        days_back = call.data.get("days_back", 0)
        _LOGGER.info("Statistics update started (days_back: %d)", days_back)
        
        try:
            # Get the current client instance from hass.data (which gets updated during cookie refresh)
            current_client = hass.data[DOMAIN][entry.entry_id]
            
            # Get fresh data from PSEG with the specified days_back
            historical_data = await current_client.get_usage_data(days_back=days_back)
            
            if "chart_data" in historical_data:
                await _process_chart_data(hass, historical_data["chart_data"])
                _LOGGER.info("Statistics update completed successfully")
            else:
                _LOGGER.warning("No chart data found in response")
                
        except InvalidAuth as e:
            _LOGGER.error("Authentication failed during update: %s", e)
            # Recover the saved browser first. The add-on permits one credential
            # attempt and persists a cooldown, so polling cannot flood PSEG.
            retry_after = getattr(current_client, "session_refresh_retry_after", None)
            if retry_after and datetime.now(timezone.utc) < retry_after:
                return
            cookies = await refresh_saved_session(
                current_client.cookie,
                username=entry.data.get(CONF_USERNAME, ""),
                password=entry.data.get(CONF_PASSWORD, ""),
                allow_login=True,
            )
            if not cookies:
                current_client.session_refresh_retry_after = (
                    datetime.now(timezone.utc) + timedelta(hours=6)
                )
                _LOGGER.warning("Saved PSEG session unavailable; manual sign-in is required")
                await hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "PSEG Integration: Sign-in required",
                        "message": "The PSEG session expired and automatic login could not complete. Further credential attempts are paused for six hours to avoid repeated challenges. Check the PSEG Automation app logs for the reason. You can also refresh through the integration's Configure screen.",
                        "notification_id": "psegli_auth_failed",
                    },
                )
                return
            current_client.session_refresh_retry_after = None

            # Apply the new cookie and retry this update immediately.
            if entry.entry_id in hass.data.get(DOMAIN, {}):
                current_client.update_cookie(cookies)
                if hasattr(entry, "runtime_data") and entry.runtime_data:
                    coordinator = entry.runtime_data
                    if hasattr(coordinator, "client"):
                        coordinator.client.update_cookie(cookies)
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, CONF_COOKIE: cookies},
            )
            _LOGGER.debug("Retrying statistics update with refreshed cookie")

            historical_data = await current_client.get_usage_data(days_back=days_back)
            if "chart_data" in historical_data:
                await _process_chart_data(hass, historical_data["chart_data"])
                await hass.services.async_call(
                    "persistent_notification", "dismiss", {"notification_id": "psegli_auth_failed"}
                )
                _LOGGER.info("Statistics update completed successfully after refresh")
            else:
                _LOGGER.warning("No chart data found in response after refresh")
        except PSEGLIError as e:
            _LOGGER.warning("PSEG data request failed without a sign-in error: %s", e)
            
        except Exception as e:
            _LOGGER.error("Failed to update statistics: %s", e)

    # Register the manual service
    hass.services.async_register(
        DOMAIN,
        "update_statistics",
        async_update_statistics_manual
    )
    
    # Register the cookie refresh service
    async def async_refresh_cookie(call: Any) -> None:
        """Manually refresh the PSEG authentication cookie."""
        _LOGGER.debug("Cookie refresh service called")
        
        try:
            username = entry.data.get(CONF_USERNAME)
            password = entry.data.get(CONF_PASSWORD)
            
            if not username or not password:
                _LOGGER.error("No credentials available for cookie refresh")
                return
            
            _LOGGER.debug("Attempting to refresh cookie via addon...")
            
            # Check if addon is healthy before attempting refresh
            if not await check_addon_health():
                _LOGGER.error("Addon not available or unhealthy, cannot refresh cookie")
                return
            
            # Attempt to get fresh cookies
            mfa_method = entry.data.get(CONF_MFA_METHOD, "sms")
            cookies = await get_fresh_cookies(username, password, mfa_method=mfa_method)
            
            if cookies == MFA_REQUIRED:
                channel = "phone" if mfa_method == "sms" else "email"
                _LOGGER.warning("PSEG requires MFA - check %s for code, then call enter_mfa_code service", channel)
                await hass.async_create_task(
                    hass.services.async_call(
                        "persistent_notification",
                        "create",
                        {
                            "title": "PSEG Integration: MFA Required",
                            "message": f"PSEG sent a verification code to your {channel}. Check your {channel}, then go to Developer Tools > Services and call 'PSEG Long Island: Enter MFA Code' with the code. Or go to Settings > Integrations > PSEG Long Island > Configure (⋮) > Options, leave cookie empty, Submit, and enter the code there.",
                            "notification_id": "psegli_mfa_required",
                        },
                    )
                )
            elif cookies:
                # Cookies are already in string format from addon
                cookie_string = cookies
                
                # Get the actual client instance from hass.data
                current_client = hass.data[DOMAIN][entry.entry_id]
                
                # Update the client with new cookie
                current_client.update_cookie(cookie_string)
                
                # Also update the coordinator's client if it exists
                if hasattr(entry, 'runtime_data') and entry.runtime_data:
                    coordinator = entry.runtime_data
                    if hasattr(coordinator, 'client'):
                        coordinator.client.update_cookie(cookie_string)
                        _LOGGER.debug("✅ Updated coordinator client cookie")
                
                _LOGGER.debug("Updated client cookie")
                _LOGGER.debug("✅ Updated client session headers")
                
                # Update the config entry
                hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_COOKIE: cookie_string},
                )
                
                _LOGGER.debug("✅ Updated config entry with new cookie")
                _LOGGER.debug("Successfully refreshed cookie via addon")
                
                # Test the new cookie
                await current_client.test_connection()
                _LOGGER.debug("New cookie validation successful")
                
                # Fetch and save energy data with the new cookie
                try:
                    await async_update_statistics_manual(type("Call", (), {"data": {"days_back": 0}})())
                    _LOGGER.debug("Energy data saved after cookie refresh")
                except Exception as stats_err:
                    _LOGGER.warning("Statistics update after refresh failed: %s", stats_err)
                
                # Create a success notification
                await hass.async_create_task(
                    hass.services.async_call(
                        "persistent_notification",
                        "create",
                        {
                            "title": "PSEG Integration: Cookie Refreshed",
                            "message": "Successfully refreshed your PSEG authentication cookie. The integration should now work properly.",
                            "notification_id": "psegli_cookie_refreshed",
                        },
                    )
                )
                
            else:
                _LOGGER.error("Addon failed to provide fresh cookies")
                # Create an error notification
                await hass.async_create_task(
                    hass.services.async_call(
                        "persistent_notification",
                        "create",
                        {
                            "title": "PSEG Integration: Cookie Refresh Failed",
                            "message": "Failed to refresh your PSEG authentication cookie. Please check the addon status or provide a cookie manually.",
                            "notification_id": "psegli_cookie_refresh_failed",
                        },
                    )
                )
                
        except Exception as e:
            _LOGGER.error("Failed to refresh cookie: %s", e)
            # Create an error notification
            await hass.async_create_task(
                hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "PSEG Integration: Cookie Refresh Error",
                        "message": f"Error refreshing your PSEG authentication cookie: {e}",
                        "notification_id": "psegli_cookie_refresh_error",
                    },
                )
            )
    
    hass.services.async_register(
        DOMAIN,
        "refresh_cookie",
        async_refresh_cookie
    )
    
    async def async_enter_mfa_code(call: Any) -> None:
        """Complete MFA by entering the verification code from email or SMS."""
        code = call.data.get("code", "").strip()
        if not code:
            _LOGGER.error("No MFA code provided")
            return
        if not await check_addon_health():
            _LOGGER.error("Addon not available - cannot complete MFA")
            return
        cookies = await complete_mfa_login(code)
        if cookies:
            hass.config_entries.async_update_entry(
                entry,
                data={**entry.data, CONF_COOKIE: cookies},
            )
            if entry.entry_id in hass.data.get(DOMAIN, {}):
                current_client = hass.data[DOMAIN][entry.entry_id]
                current_client.update_cookie(cookies)
                if hasattr(entry, "runtime_data") and entry.runtime_data:
                    coordinator = entry.runtime_data
                    if hasattr(coordinator, "client"):
                        coordinator.client.update_cookie(cookies)
            await hass.services.async_call(
                "persistent_notification",
                "dismiss",
                {"notification_id": "psegli_mfa_required"},
            )
            # Fetch and save energy data now that we have a valid cookie
            try:
                await async_update_statistics_manual(type("Call", (), {"data": {"days_back": 0}})())
                _LOGGER.info("Energy data saved after MFA")
            except Exception as stats_err:
                _LOGGER.warning("Statistics update after MFA failed: %s", stats_err)
            msg = "Successfully completed MFA. Your PSEG integration is now working."
            if entry.entry_id not in hass.data.get(DOMAIN, {}):
                msg += " Reload the integration (Settings > Integrations > PSEG Long Island > Reload) to start using it."
            await hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "PSEG Integration: MFA Complete",
                    "message": msg,
                    "notification_id": "psegli_mfa_complete",
                },
            )
            _LOGGER.info("MFA completed successfully")
        else:
            _LOGGER.error("MFA failed - code may be invalid or session expired")
            await hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "PSEG Integration: MFA Failed",
                    "message": "MFA verification failed. The code may be invalid or expired. Try refresh_cookie again, then enter the new code.",
                    "notification_id": "psegli_mfa_failed",
                },
            )
    
    hass.services.async_register(
        DOMAIN,
        "enter_mfa_code",
        async_enter_mfa_code
    )
    
    # Poll usage every 10 minutes using the active session.
    async def async_scheduled_cookie_refresh() -> None:
        """Update usage without repeatedly launching a login browser."""
        try:
            current_client = hass.data.get(DOMAIN, {}).get(entry.entry_id)
            if not current_client or not current_client.cookie:
                return
            await async_update_statistics_manual(type("Call", (), {"data": {"days_back": 0}})())
            # Keep the saved browser profile active while the API session is
            # still usable. This avoids fresh headless credential logins later.
            auth_retry_after = getattr(current_client, "session_refresh_retry_after", None)
            keepalive_retry_after = getattr(current_client, "browser_keepalive_retry_after", None)
            now = datetime.now(timezone.utc)
            if (
                (not auth_retry_after or now >= auth_retry_after)
                and (not keepalive_retry_after or now >= keepalive_retry_after)
            ):
                browser_cookies = await refresh_saved_session(current_client.cookie)
                if browser_cookies:
                    current_client.update_cookie(browser_cookies)
                    current_client.session_refresh_retry_after = None
                    current_client.browser_keepalive_retry_after = None
                else:
                    current_client.browser_keepalive_retry_after = (
                        datetime.now(timezone.utc) + timedelta(hours=1)
                    )
            if current_client.cookie != entry.data.get(CONF_COOKIE, ""):
                hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_COOKIE: current_client.cookie},
                )
        except Exception as e:
            _LOGGER.error("Failed to update scheduled PSEG usage: %s", e)
    
    # Keep a single global cookie refresh loop across entries (to avoid duplicate tasks).
    domain_state = hass.data[DOMAIN]
    domain_state[_COOKIE_REFRESH_USERS_KEY] = domain_state.get(
        _COOKIE_REFRESH_USERS_KEY, 0
    ) + 1

    @callback
    def _stop_cookie_refresh_listener(_event=None) -> None:
        if _COOKIE_REFRESH_USERS_KEY in domain_state:
            domain_state.pop(_COOKIE_REFRESH_USERS_KEY, None)
        unsub = domain_state.pop(_COOKIE_REFRESH_LISTENER_KEY, None)
        if unsub:
            unsub()
            _LOGGER.debug("Stopped shared cookie refresh interval listener")

        refresh_lock = domain_state.pop(_COOKIE_REFRESH_LOCK_KEY, None)
        if refresh_lock is not None and refresh_lock.locked():
            _LOGGER.debug("A scheduled cookie refresh is in progress; it will complete naturally")

    @callback
    def _scheduled_cookie_refresh_tick(_now: datetime | None = None) -> None:
        """Run one scheduled cookie refresh cycle."""
        refresh_lock = domain_state.setdefault(_COOKIE_REFRESH_LOCK_KEY, asyncio.Lock())
        if refresh_lock.locked():
            _LOGGER.debug("Skipping scheduled cookie refresh tick because one is already running")
            return

        async def _do_refresh() -> None:
            async with refresh_lock:
                await async_scheduled_cookie_refresh()

        hass.async_create_task(_do_refresh())

    if _COOKIE_REFRESH_LISTENER_KEY not in domain_state:
        domain_state[_COOKIE_REFRESH_LISTENER_KEY] = async_track_time_interval(
            hass,
            _scheduled_cookie_refresh_tick,
            timedelta(minutes=10),
        )
        _LOGGER.debug(
            "Started shared periodic cookie refresh listener (interval=%ds)",
            COOKIE_REFRESH_INTERVAL_SECONDS,
        )

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop_cookie_refresh_listener)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Seed enough recorder history for yesterday, last-week, and comparison
    # sensors. Subsequent scheduled updates only request the latest day.
    if cookie:
        hass.async_create_background_task(
            hass.services.async_call(
                DOMAIN,
                "update_statistics",
                {"days_back": 21},
                blocking=True,
            ),
            name=f"{DOMAIN}_initial_statistics",
            eager_start=False,
        )
        _LOGGER.info("Scheduled initial 21-day PSEG statistics backfill")
    
    return True

class PSEGCoordinator(DataUpdateCoordinator):
    """Handle fetching PSEG data and updating statistics (like Opower)."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: PSEGLIClient):
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="PSEG",
            # No automatic updates - only manual and scheduled
            update_interval=None,
        )
        self.entry = entry
        self.client = client


    async def _async_update_data(self):
        """Fetch data from PSEG and update statistics."""
        try:
            # This ensures both manual and automatic updates use identical code paths
            await self.hass.services.async_call(
                DOMAIN,
                "update_statistics",
                {"days_back": 0},
                blocking=True
            )
            
            # Return a simple success indicator since the service handles the actual work
            return {"status": "success"}
                
        except InvalidAuth as e:
            _LOGGER.error("Authentication failed during coordinator update: %s", e)
            _LOGGER.debug("Cookie refresh will be attempted at the next scheduled time (every 10 minutes)")
            
            # Create a persistent notification to alert the user
            await self.hass.async_create_task(
                self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "PSEG Integration: Authentication Failed",
                        "message": f"Your PSEG cookie has expired. Cookie refresh will be attempted at the next scheduled interval (every 10 minutes).\n\nError: {e}",
                        "notification_id": "psegli_auth_failed",
                    },
                )
            )
            raise UpdateFailed(f"Authentication failed: {e}")
        except Exception as e:
            _LOGGER.error("Failed to update PSEG data: %s", e)
            raise UpdateFailed(f"Failed to update PSEG data: {e}")

async def _process_chart_data(hass: HomeAssistant, chart_data: dict[str, Any]) -> None:
    """Process chart data and update statistics."""
    # Create timezone once to avoid blocking calls
    local_tz = await hass.async_add_executor_job(pytz.timezone, 'America/New_York')
    
    for series_name, series_data in chart_data.items():
        try:
            _LOGGER.debug("Series %s data type: %s", series_name, type(series_data))
            _LOGGER.debug("Series %s keys: %s", series_name, list(series_data.keys()) if isinstance(series_data, dict) else "not a dict")
            
            valid_points = series_data.get("valid_points", [])
            _LOGGER.debug("Valid points type: %s, length: %s", type(valid_points), len(valid_points) if hasattr(valid_points, '__len__') else "no length")
            
            # Handle case where valid_points might be a string (defensive programming)
            if isinstance(valid_points, str):
                _LOGGER.warning("Valid points is a string, attempting to parse: %s", valid_points[:100])
                try:
                    import json
                    valid_points = json.loads(valid_points)
                    _LOGGER.debug("Successfully parsed valid_points from string")
                except Exception as e:
                    _LOGGER.error("Failed to parse valid_points string: %s", e)
                    continue
            
            if not valid_points or not isinstance(valid_points, list):
                _LOGGER.warning("Valid points is not a list: %s", type(valid_points))
                continue

            # Recorder statistics require hourly timestamps. PSEG can return
            # 15-minute kWh readings, so combine readings within each hour.
            hourly_points: dict[datetime, float] = {}
            for point in valid_points:
                timestamp = point.get("timestamp")
                value = point.get("value", 0)
                if not isinstance(timestamp, datetime):
                    continue
                if timestamp.tzinfo is None:
                    _LOGGER.warning("Skipping PSEG reading without a timezone")
                    continue
                # Bucket after conversion to UTC, so the two 1 AM hours when
                # daylight saving time ends remain distinct recorder hours.
                hour = timestamp.astimezone(timezone.utc).replace(
                    minute=0, second=0, microsecond=0
                )
                hourly_points[hour] = hourly_points.get(hour, 0.0) + max(0.0, float(value or 0))
            valid_points = [
                {"timestamp": timestamp, "value": value}
                for timestamp, value in sorted(hourly_points.items())
            ]
            _LOGGER.debug("Aggregated %d source points into %d hourly points for %s", len(series_data.get("valid_points", [])), len(valid_points), series_name)
            
            # Determine which statistic this series maps to
            if "Off-Peak" in series_name:
                statistic_id = "psegli:off_peak_usage"
                cost_statistic_id = "psegli:off_peak_cost"
                rate_entity_id = "sensor.pseg_rate_194_off_peak"
            elif "On-Peak" in series_name:
                statistic_id = "psegli:on_peak_usage"
                cost_statistic_id = "psegli:on_peak_cost"
                rate_entity_id = "sensor.pseg_rate_194_peak"
            else:
                continue  # Skip non-peak series
            
            statistics = []
            cost_statistics = []
            
            # Check if this series has any meaningful data (non-zero values)
            non_zero_points = [point for point in valid_points if point.get("value", 0) > 0]
            if not non_zero_points:
                _LOGGER.debug("Skipping %s - all values are 0, no meaningful data", series_name)
                continue
            
            # Get the first timestamp to determine the hour
            first_timestamp = valid_points[0]["timestamp"] if valid_points else None
            if first_timestamp is None:
                _LOGGER.warning("No valid timestamp found for %s, skipping", series_name)
                continue
                
            # Convert to datetime if it's a timestamp
            if isinstance(first_timestamp, (int, float)):
                first_dt = datetime.fromtimestamp(first_timestamp)
            else:
                first_dt = first_timestamp
                
            # Ensure timezone awareness
            if first_dt.tzinfo is None:
                local_tz = pytz.timezone("America/New_York")
                first_dt = local_tz.localize(first_dt)
            
            # Get the last cumulative sum before our first data point to ensure continuity
            _LOGGER.debug("Getting last cumulative sum for %s before %s", series_name, first_dt.strftime("%Y-%m-%d %H:%M"))
            cumulative_offset = await get_last_cumulative_kwh(hass, statistic_id, first_dt)

            rate_state = hass.states.get(rate_entity_id)
            try:
                rate = float(rate_state.state) if rate_state is not None else None
            except (TypeError, ValueError):
                rate = None

            if rate is None or rate <= 0:
                _LOGGER.warning(
                    "Skipping cost statistics for %s because %s is unavailable",
                    series_name,
                    rate_entity_id,
                )
                cumulative_cost_offset = None
            else:
                cumulative_cost_offset = await get_last_cumulative_kwh(
                    hass, cost_statistic_id, first_dt
                )
            
            _LOGGER.debug("Starting statistics processing for %s with %d points, continuing from cumulative offset %.6f", 
                         series_name, len(valid_points), cumulative_offset)
            
            points_processed = 0
            
            try:
                for i, point in enumerate(valid_points):
                    try:
                        # Extract timestamp and value from the point
                        if isinstance(point, dict) and "timestamp" in point and "value" in point:
                            timestamp = point["timestamp"]
                            value = point["value"]
                            
                            # Convert timestamp to datetime if it's not already
                            if isinstance(timestamp, (int, float)):
                                timestamp = datetime.fromtimestamp(timestamp)
                            
                            # Ensure we have a timezone-aware datetime
                            if timestamp.tzinfo is None:
                                timestamp = local_tz.localize(timestamp)
                            
                            # Convert to UTC for HA
                            start_time = timestamp.astimezone(timezone.utc)
                            
                            # Check for problematic values before conversion
                            if value is None:
                                _LOGGER.warning("Point %d: value is None, replacing with 0", i)
                                value = 0
                            
                            if isinstance(value, str):
                                try:
                                    raw_energy_value = float(value)
                                except ValueError:
                                    _LOGGER.error("Point %d: cannot convert string value '%s' to float", i, value)
                                    continue
                            else:
                                raw_energy_value = float(value)
                            
                            # Ensure energy value is non-negative
                            energy_value = max(0.0, raw_energy_value)
                            
                            # Additional validation: check for unreasonably large values
                            if energy_value > 1000:  # More than 1000 kWh in an hour is suspicious
                                _LOGGER.warning("Point %d: suspiciously large energy value: %.6f kWh, capping at 100", i, energy_value)
                                energy_value = 100.0
                            
                            # Calculate cumulative total
                            cumulative_kwh = energy_value + cumulative_offset
                            points_processed += 1
                            
                            statistics.append({
                                "start": start_time,        # Time block start
                                "sum": cumulative_kwh,      # Cumulative total
                            })

                            if cumulative_cost_offset is not None:
                                cumulative_cost_offset += energy_value * rate
                                cost_statistics.append({
                                    "start": start_time,
                                    "sum": cumulative_cost_offset,
                                })
                            
                            # Update cumulative_offset for the next point
                            cumulative_offset = cumulative_kwh
                            
                        else:
                            _LOGGER.warning("Skipping invalid point %d: %s", i, point)
                            continue
                    except Exception as e:
                        _LOGGER.error("Error processing point %d (%s): %s", i, point, e)
                        continue
                
                _LOGGER.debug("Processed %d points for %s", points_processed, series_name)
                
            except Exception as e:
                _LOGGER.error("Error in enumerate loop for series %s: %s", series_name, e)
                continue
            
            # Use HA's Statistics API to update
            try:
                _LOGGER.debug("Calling async_add_external_statistics with %d statistics entries", len(statistics))
                if statistics:
                    _LOGGER.debug("First statistics entry: %s", statistics[0])
                    _LOGGER.debug("Last statistics entry: %s", statistics[-1])
                    _LOGGER.debug("Sample of statistics data being sent:")
                    for i, stat in enumerate(statistics[:3]):  # Show first 3 entries
                        _LOGGER.debug("  Entry %d: %s", i, stat)
                
                # Create metadata for the statistic
                metadata = {
                    "statistic_id": statistic_id,  # Use proper format
                    "source": "psegli",  # Use domain as source
                    "unit_of_measurement": "kWh",
                    "unit_class": "energy",
                    "has_mean": False,
                    "mean_type": StatisticMeanType.NONE,
                    "has_sum": True,  # Set to True since we're sending cumulative totals
                    "name": f"PSEG {series_name}",
                }
                
                _LOGGER.debug("Using metadata: %s", metadata)
                
                # Check if the function is callable
                if not callable(async_add_external_statistics):
                    _LOGGER.error("async_add_external_statistics is not callable: %s", type(async_add_external_statistics))
                    continue
                
                result = async_add_external_statistics(
                    hass,
                    metadata,
                    statistics
                )
                
                # Check if result is awaitable
                if hasattr(result, '__await__'):
                    await result
                    _LOGGER.debug("Successfully updated statistics for %s", statistic_id)
                else:
                    _LOGGER.debug("Statistics update completed (non-awaitable result) for %s", statistic_id)

                if cost_statistics:
                    currency = getattr(hass.config, "currency", None) or "USD"
                    cost_metadata = {
                        "statistic_id": cost_statistic_id,
                        "source": "psegli",
                        "unit_of_measurement": currency,
                        "unit_class": None,
                        "has_mean": False,
                        "mean_type": StatisticMeanType.NONE,
                        "has_sum": True,
                        "name": f"PSEG {series_name} Cost",
                    }
                    cost_result = async_add_external_statistics(
                        hass,
                        cost_metadata,
                        cost_statistics,
                    )
                    if hasattr(cost_result, '__await__'):
                        await cost_result
                    _LOGGER.debug(
                        "Successfully updated cost statistics for %s at %.6f %s/kWh",
                        cost_statistic_id,
                        rate,
                        currency,
                    )
                
                # Verify statistics were stored by checking again
                _LOGGER.debug("Verifying statistics were stored by checking again...")
                try:
                    from homeassistant.components.recorder.statistics import statistics_during_period
                    
                    # Query for the statistics we just stored to verify the sum values
                    end_time = datetime.now()
                    start_time = end_time - timedelta(hours=24)  # Last 24 hours
                    
                    verification_stats = await get_instance(hass).async_add_executor_job(
                        statistics_during_period,
                        hass,
                        start_time,
                        end_time,
                        [statistic_id],  # Only check our specific statistic
                        "hour",
                        None,
                        {"start", "end", "sum"},  # Include sum field
                    )
                    
                    _LOGGER.debug("Verification check returned: %s", verification_stats)
                    
                    if verification_stats and statistic_id in verification_stats and verification_stats[statistic_id]:
                        # Get the last stored statistic with sum value
                        stored_stats = verification_stats[statistic_id]
                        last_stored = None
                        
                        # Find the last entry that has a sum value
                        for stat in reversed(stored_stats):
                            if 'sum' in stat and stat['sum'] is not None:
                                last_stored = stat
                                break
                        
                        if last_stored:
                            last_sum = last_stored.get("sum", 0.0)
                            _LOGGER.debug("Verification: Statistics confirmed stored for %s, last sum: %.6f", statistic_id, last_sum)
                        else:
                            _LOGGER.warning("Verification: No sum values found in stored statistics for %s", statistic_id)
                    else:
                        _LOGGER.warning("Verification: No statistics found for %s", statistic_id)
                        
                except Exception as e:
                    _LOGGER.debug("Could not verify statistics: %s", e)
                    
            except Exception as e:
                _LOGGER.error("Error calling async_add_external_statistics for %s: %s", statistic_id, e)
        except Exception as e:
            _LOGGER.error("Error processing series %s: %s", series_name, e)
            continue


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply a supplied cookie to the running client without duplicate timers."""
    client = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    cookie = entry.data.get(CONF_COOKIE, "")
    if not client or not cookie or cookie == client.cookie:
        return
    client.update_cookie(cookie)
    client.session_refresh_retry_after = None
    client.browser_keepalive_retry_after = None
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator:
        if coordinator.client is not client:
            coordinator.client.update_cookie(cookie)
        await coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Unload the coordinator
    if entry.runtime_data:
        await entry.runtime_data.async_shutdown()
    
    # Clean up coordinator reference from hass.data
    if DOMAIN in hass.data and 'coordinator' in hass.data[DOMAIN]:
        del hass.data[DOMAIN]['coordinator']
    
    # Clean up global scheduled cookie refresh listener when the last entry unloads.
    domain_data = hass.data.get(DOMAIN, {})
    users = domain_data.get(_COOKIE_REFRESH_USERS_KEY, 0)
    if users:
        users = max(0, int(users) - 1)
        domain_data[_COOKIE_REFRESH_USERS_KEY] = users
        if users == 0:
            unsub = domain_data.pop(_COOKIE_REFRESH_LISTENER_KEY, None)
            if unsub:
                unsub()
                _LOGGER.debug("Stopped shared periodic cookie refresh listener")
            domain_data.pop(_COOKIE_REFRESH_USERS_KEY, None)
            lock = domain_data.get(_COOKIE_REFRESH_LOCK_KEY)
            if lock is not None and lock.locked():
                _LOGGER.debug("A scheduled cookie refresh is in progress during unload; it will complete naturally")
            domain_data.pop(_COOKIE_REFRESH_LOCK_KEY, None)
    
    # Remove the services
    hass.services.async_remove(DOMAIN, "update_statistics")
    hass.services.async_remove(DOMAIN, "refresh_cookie")
    hass.services.async_remove(DOMAIN, "enter_mfa_code")
    
    return unload_ok
