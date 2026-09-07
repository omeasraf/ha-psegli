"""PSEG Long Island client."""

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

from .exceptions import InvalidAuth

_LOGGER = logging.getLogger(__name__)


class PSEGLIClient:
    """PSEG Long Island API client."""

    def __init__(self, cookie: str) -> None:
        """Initialize the client."""
        self.cookie = cookie
        self.session = requests.Session()
        self.session.headers.update({
            "Cookie": cookie,
            "Referer": "https://mysmartenergy.psegliny.com/Dashboard",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Accept-Language": "en-US,en;q=0.8",
            "X-Requested-With": "XMLHttpRequest",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Ch-Ua": '"Not)A;Brand";v="8", "Chromium";v="138", "Brave";v="138"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"macOS"',
            "Sec-Gpc": "1"
        })

    def update_cookie(self, new_cookie: str) -> None:
        """Update the cookie in this client instance."""
        self.cookie = new_cookie
        self.session.headers.update({"Cookie": new_cookie})
        _LOGGER.debug("Updated client cookie to: %s", new_cookie[:50] + "..." if len(new_cookie) > 50 else new_cookie)

    @staticmethod
    def _parse_cookie_header(cookie_header: str) -> dict[str, str]:
        """Parse a Cookie header while preserving values that contain equals signs."""
        cookies: dict[str, str] = {}
        for item in cookie_header.split(";"):
            if "=" not in item:
                continue
            name, value = item.strip().split("=", 1)
            if name:
                cookies[name] = value
        return cookies

    def _sync_response_cookies(self, response: requests.Response) -> None:
        """Carry rolling Set-Cookie values into the explicit Cookie header."""
        cookies = self._parse_cookie_header(self.cookie)
        for item in [*response.history, response]:
            for response_cookie in item.cookies:
                if response_cookie.value:
                    cookies[response_cookie.name] = response_cookie.value
                else:
                    cookies.pop(response_cookie.name, None)

        refreshed_cookie = "; ".join(
            f"{name}={value}" for name, value in cookies.items() if name and value
        )
        if refreshed_cookie and refreshed_cookie != self.cookie:
            self.update_cookie(refreshed_cookie)
            _LOGGER.debug("Applied refreshed cookies returned by PSEG")

    def _test_connection_sync(self) -> bool:
        """Test the connection to PSEG (synchronous)."""
        try:
            response = self.session.get("https://mysmartenergy.psegliny.com/Dashboard")
            self._sync_response_cookies(response)
            response.raise_for_status()
            
            # Check if we're redirected to login page
            if "login" in response.url.lower() or "signin" in response.url.lower():
                _LOGGER.error("Cookie rejected - redirected to login page")
                raise InvalidAuth("Cookie rejected - redirected to login page")
            
            _LOGGER.debug("PSEG connection test successful")
            return True
        except requests.exceptions.RequestException as err:
            _LOGGER.error("Failed to connect to PSEG: %s", err)
            raise InvalidAuth("Invalid authentication") from err

    async def test_connection(self) -> bool:
        """Test the connection to PSEG (async wrapper)."""
        try:
            loop = asyncio.get_running_loop()
            with ThreadPoolExecutor() as executor:
                return await loop.run_in_executor(executor, self._test_connection_sync)
        except RuntimeError:
            # Fallback for when there's no running loop
            return self._test_connection_sync()

    def _get_dashboard_page(self) -> tuple[str, str]:
        """Get the Dashboard page and extract RequestVerificationToken."""
        dashboard_response = self.session.get("https://mysmartenergy.psegliny.com/Dashboard")
        self._sync_response_cookies(dashboard_response)
        if dashboard_response.status_code != 200:
            raise InvalidAuth("Failed to get Dashboard page")

        response_url = dashboard_response.url.lower()
        response_text = dashboard_response.text
        response_text_lower = response_text.lower()
        if "login" in response_url or "signin" in response_url or "sign in" in response_text_lower:
            raise InvalidAuth("Authentication cookie expired or was rejected")
        
        # Extract the token from the page
        import re
        token_tag = BeautifulSoup(response_text, "html.parser").find(
            "input", attrs={"name": "__RequestVerificationToken"}
        )
        request_token = token_tag.get("value") if token_tag else None
        if not request_token:
            # Keep a tolerant fallback for malformed HTML responses.
            token_match = re.search(
                r"__RequestVerificationToken[^>]+value=['\"]([^'\"]+)['\"]",
                response_text,
                flags=re.IGNORECASE,
            )
            request_token = token_match.group(1) if token_match else None

        if request_token:
            _LOGGER.debug("Found RequestVerificationToken: %s...", request_token[:20])
        else:
            _LOGGER.error(
                "Could not find RequestVerificationToken on /Dashboard (url=%s, content_type=%s, length=%d, title=%s)",
                dashboard_response.url,
                dashboard_response.headers.get("Content-Type", "unknown"),
                len(response_text),
                BeautifulSoup(response_text, "html.parser").title.get_text(strip=True)
                if BeautifulSoup(response_text, "html.parser").title
                else "unknown",
            )
            raise InvalidAuth("Could not find RequestVerificationToken on /Dashboard")
        
        return dashboard_response.text, request_token

    def _setup_chart_context(self, request_token: str, start_date: datetime, end_date: datetime) -> None:
        """Set up the Chart context with hourly granularity."""
        chart_setup_url = "https://mysmartenergy.psegliny.com/Dashboard/Chart"
        chart_setup_data = {
            "__RequestVerificationToken": request_token,
            "UsageInterval": "5",  # 5 = Hourly granularity
            "UsageType": "1",
            # Smart Energy uses this field to determine which chart setting
            # changed. Target UsageType so value 1 switches Demand to kWh.
            "jsTargetName": "UsageType",
            "EnableHoverChart": "true",
            "Start": start_date.strftime("%Y-%m-%d"),
            "End": end_date.strftime("%Y-%m-%d"),
            "IsRangeOpen": "False",
            "MaintainMaxDate": "true",
            "SelectedViaDateRange": "False",
            "ChartComparison": "1",
            "ChartComparison2": "0",
            "ChartComparison3": "0",
            "ChartComparison4": "0"
        }
        
        _LOGGER.debug("Making Chart/ setup request with hourly granularity (start: %s, end: %s)", 
                    start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))
        _LOGGER.debug("Chart setup data: %s", chart_setup_data)
        
        chart_setup_response = self.session.post(chart_setup_url, data=chart_setup_data)
        self._sync_response_cookies(chart_setup_response)
        chart_setup_response.raise_for_status()
        
        # Check for redirect response in Chart/ request - if it redirects, the request failed
        try:
            chart_setup_json = json.loads(chart_setup_response.text)
            if "AjaxResults" in chart_setup_json and chart_setup_json["AjaxResults"]:
                for result in chart_setup_json["AjaxResults"]:
                    if result.get("Action") == "Redirect":
                        _LOGGER.error("Chart setup request FAILED - redirected to: %s", result.get('Value'))
                        _LOGGER.error(
                            "Chart setup response: status=%s content_type=%s body=%s",
                            chart_setup_response.status_code,
                            chart_setup_response.headers.get("Content-Type", "unknown"),
                            chart_setup_response.text[:500],
                        )
                        _LOGGER.warning("Continuing to ChartData; PSEG may already have the requested chart context")
        except json.JSONDecodeError:
            _LOGGER.error("Chart setup response is not JSON - request failed")
            raise InvalidAuth("Chart setup response is not JSON - request failed")

    def _get_chart_data(self, start_date: datetime, end_date: datetime) -> dict[str, Any]:
        """Get the actual chart data from PSEG."""
        chart_data_url = "https://mysmartenergy.psegliny.com/Dashboard/ChartData"
        pseg_timezone = ZoneInfo("America/New_York")
        if start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=pseg_timezone)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=pseg_timezone)
        chart_data_params = {
            # These are the parameters used by PSEG's own Highcharts range
            # handler. Supplying them makes historical backfills deterministic.
            "unixTimeStart": int(start_date.timestamp() * 1000),
            "unixTimeEnd": int(end_date.timestamp() * 1000),
            "_": int(datetime.now().timestamp() * 1000)  # Cache buster
        }
        
        _LOGGER.debug("Making ChartData/ request to get hourly data")
        chart_response = self.session.get(chart_data_url, params=chart_data_params)
        self._sync_response_cookies(chart_response)
        chart_response.raise_for_status()
        
        # Debug: Log the response content
        _LOGGER.debug("ChartData response status: %s", chart_response.status_code)
        _LOGGER.debug("ChartData response headers: %s", dict(chart_response.headers))
        _LOGGER.debug("ChartData response content (first 500 chars): %s", chart_response.text[:500])
        
        chart_data = json.loads(chart_response.text)
        return chart_data

    def _get_usage_data_sync(self, start_date: Optional[datetime] = None, end_date: Optional[datetime] = None, days_back: int = 0) -> Dict[str, Any]:
        """Get usage data from PSEG (synchronous)."""
        try:
            # First check if our cookie is still valid
            self._test_connection_sync()
            
            # Calculate date range based on days_back parameter
            if days_back == 0:
                # Yesterday to today (accounting for data lag)
                end_date = datetime.now()
                start_date = end_date - timedelta(days=1)
            else:
                # days_back days ago to now
                end_date = datetime.now()
                start_date = end_date - timedelta(days=days_back)
            
            _LOGGER.debug("Date calculation: days_back=%d, start_date=%s, end_date=%s", 
                        days_back, start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))
            
            # Step 1: Get Dashboard page and extract token
            _, request_token = self._get_dashboard_page()
            
            # Step 2: Set up Chart context
            self._setup_chart_context(request_token, start_date, end_date)
            
            # Step 3: Get actual chart data
            chart_data = self._get_chart_data(start_date, end_date)
            
            # Create a minimal widget data structure since we're not fetching it
            widget_data = {"AjaxResults": []}

            return self._parse_data(widget_data, chart_data)

        except requests.exceptions.RequestException as err:
            _LOGGER.error("Failed to get usage data: %s", err)
            raise InvalidAuth("Failed to get usage data") from err
        except json.JSONDecodeError as err:
            _LOGGER.error("Failed to parse JSON response: %s", err)
            # This usually indicates an expired cookie (server returns HTML login page instead of JSON)
            _LOGGER.error("This error typically indicates an expired authentication cookie. Please update your cookie in the PSEG integration configuration.")
            raise InvalidAuth("Authentication cookie has expired - please update your cookie") from err

    async def get_usage_data(self, start_date: Optional[datetime] = None, end_date: Optional[datetime] = None, days_back: int = 0) -> Dict[str, Any]:
        """Get usage data from PSEG (async wrapper)."""
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as executor:
            return await loop.run_in_executor(executor, self._get_usage_data_sync, start_date, end_date, days_back)

    def _parse_data(self, widget_data: Dict[str, Any], chart_data: Dict[str, Any]) -> Dict[str, Any]:
        """Parse the widget and chart data."""
        result = {
            "widgets": {},
            "chart_data": {},
            "last_update": datetime.now().isoformat()
        }

        # Parse widget data
        for result_item in widget_data.get("AjaxResults", []):
            if result_item.get("Action") == "Prepend" and "usageWidget" in result_item.get("Value", ""):
                html_content = result_item.get("Value", "")
                soup = BeautifulSoup(html_content, "html.parser")
                
                usage_widgets = soup.find_all("div", class_="usageWidget")
                for widget in usage_widgets:
                    usage_h2 = widget.find("h2")
                    if usage_h2:
                        usage_value = usage_h2.get_text(strip=True)
                        
                        description_div = widget.find("div", class_="widgetDescription")
                        description = description_div.get_text(strip=True) if description_div else ""
                        
                        range_alert = widget.find("div", class_="rangeAlert")
                        comparison = range_alert.get_text(strip=True) if range_alert else ""
                        
                        # Extract numeric value
                        try:
                            numeric_value = float(usage_value.replace("kWh", "").strip())
                        except ValueError:
                            numeric_value = 0.0
                        
                        result["widgets"][description] = {
                            "value": numeric_value,
                            "raw_value": usage_value,
                            "description": description,
                            "comparison": comparison,
                        }

        # Smart Energy has returned both {"Data": {"series": [...]}} and
        # {"series": [...]} response shapes over time.
        chart_series = chart_data.get("series", [])
        if not chart_series and isinstance(chart_data.get("Data"), dict):
            chart_series = chart_data["Data"].get("series", [])

        if not chart_series:
            _LOGGER.warning("Chart response contained no series; keys=%s", list(chart_data))

        for series in chart_series:
                series_name = series.get("name", "Unknown")
                data_points = series.get("data", [])

                value_suffix = series.get("tooltip", {}).get("valueSuffix", "")
                if value_suffix and value_suffix.strip().lower() != "kwh":
                    _LOGGER.debug("Skipping %s because unit is %s, not kWh", series_name, value_suffix)
                    continue
                
                _LOGGER.debug("Processing series: %s with %d data points", series_name, len(data_points))
                
                valid_points = []
                for i, point in enumerate(data_points):
                    if isinstance(point, dict) and "x" in point and "y" in point:
                        # Object format: hourly data with proper structure
                        timestamp = point["x"] / 1000
                        value = point["y"]
                        # Replace None values with 0 to ensure continuous data flow
                        if value is None:
                            value = 0
                        # Timestamps need to be shifted by +4 hours to align with actual peak hours
                        # Raw timestamp shows 11:00 AM but should be 3:00 PM for peak hours
                        shifted_timestamp = timestamp + (4 * 3600)  # Add 4 hours
                        local_time = datetime.fromtimestamp(shifted_timestamp)
                        valid_points.append({
                            "timestamp": local_time,
                            "value": value
                        })
                        _LOGGER.debug("Point %d: timestamp=%s, value=%s", i, local_time, value)
                    elif isinstance(point, list) and len(point) >= 2:
                        # Array format: appears to be daily summaries, not hourly data
                        # Skip this format when we're looking for hourly consumption data
                        continue
                
                if valid_points:
                    latest_point = max(valid_points, key=lambda x: x["timestamp"])
                    values = [p["value"] for p in valid_points]
                    
                    # Debug logging
                    _LOGGER.debug("Series %s: %d valid points, values: %s", 
                                 series_name, len(valid_points), values[:5])
                    
                    result["chart_data"][series_name] = {
                        "latest_value": latest_point["value"],
                        "latest_timestamp": latest_point["timestamp"].isoformat(),
                        "min_value": min(values) if values else 0,
                        "max_value": max(values) if values else 0,
                        "avg_value": sum(values) / len(values) if values else 0,
                        "data_points": len(valid_points),
                        "valid_points": valid_points  # Include the actual data points
                    }

        return result
