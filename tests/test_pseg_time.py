"""Regression checks for Smart Energy's wall-clock chart timestamps."""

import importlib.util
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


def load_client_module():
    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.HomeAssistantError = Exception
    sys.modules.setdefault("homeassistant", homeassistant)
    sys.modules.setdefault("homeassistant.exceptions", exceptions)

    root = Path(__file__).resolve().parents[1]
    package = types.ModuleType("custom_components")
    package.__path__ = [str(root / "custom_components")]
    integration = types.ModuleType("custom_components.psegli")
    integration.__path__ = [str(root / "custom_components" / "psegli")]
    sys.modules.setdefault("custom_components", package)
    sys.modules.setdefault("custom_components.psegli", integration)
    name = "custom_components.psegli.psegli"
    spec = importlib.util.spec_from_file_location(
        name, root / "custom_components" / "psegli" / "psegli.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


client_module = load_client_module()
NY = ZoneInfo("America/New_York")


class ChartTimeTests(unittest.TestCase):
    def test_peak_hour_and_winter_offset(self):
        client = client_module.PSEGLIClient("")
        points = [
            {"x": int(datetime(2026, 10, 2, 15, tzinfo=timezone.utc).timestamp() * 1000), "y": 2.25},
            {"x": int(datetime(2026, 1, 2, 15, tzinfo=timezone.utc).timestamp() * 1000), "y": 1.5},
            {"x": int(datetime(2026, 10, 2, 16, tzinfo=timezone.utc).timestamp() * 1000), "y": None},
        ]
        result = client._parse_data({}, {"series": [{"name": "On-Peak", "data": points}]})
        parsed = result["chart_data"]["On-Peak"]["valid_points"]
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["timestamp"].isoformat(), "2026-10-02T15:00:00-04:00")
        self.assertEqual(parsed[1]["timestamp"].isoformat(), "2026-01-02T15:00:00-05:00")

    def test_repeated_fall_back_hour_remains_distinct(self):
        client = client_module.PSEGLIClient("")
        x = int(datetime(2026, 11, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        result = client._parse_data({}, {"series": [{"name": "Off-Peak", "data": [{"x": x, "y": 1}, {"x": x, "y": 2}]}]})
        times = [p["timestamp"].astimezone(timezone.utc).isoformat() for p in result["chart_data"]["Off-Peak"]["valid_points"]]
        self.assertEqual(times, ["2026-11-01T05:00:00+00:00", "2026-11-01T06:00:00+00:00"])

    def test_chart_range_uses_wall_clock_milliseconds(self):
        midnight = datetime(2026, 10, 2, tzinfo=NY)
        expected = int(datetime(2026, 10, 2, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(client_module._pseg_wall_clock_milliseconds(midnight), expected)


if __name__ == "__main__":
    unittest.main()
