"""Period usage and cost sensors for PSEG Long Island."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from typing import Any, Callable

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=1)


@dataclass(frozen=True)
class PSEGPeriodSensorDescription:
    """Describe one current-period sensor."""

    key: str
    name: str
    statistic_id: str
    period: str
    is_cost: bool
    rate_entity_id: str | None = None


SENSORS = (
    PSEGPeriodSensorDescription("off_peak_today_usage", "Off-Peak Today Usage", "psegli:off_peak_usage", "day", False),
    PSEGPeriodSensorDescription("off_peak_today_cost", "Off-Peak Today Cost", "psegli:off_peak_usage", "day", True, "sensor.pseg_rate_194_off_peak"),
    PSEGPeriodSensorDescription("off_peak_week_usage", "Off-Peak Week Usage", "psegli:off_peak_usage", "week", False),
    PSEGPeriodSensorDescription("off_peak_week_cost", "Off-Peak Week Cost", "psegli:off_peak_usage", "week", True, "sensor.pseg_rate_194_off_peak"),
    PSEGPeriodSensorDescription("off_peak_month_usage", "Off-Peak Month Usage", "psegli:off_peak_usage", "month", False),
    PSEGPeriodSensorDescription("off_peak_month_cost", "Off-Peak Month Cost", "psegli:off_peak_usage", "month", True, "sensor.pseg_rate_194_off_peak"),
    PSEGPeriodSensorDescription("on_peak_today_usage", "On-Peak Today Usage", "psegli:on_peak_usage", "day", False),
    PSEGPeriodSensorDescription("on_peak_today_cost", "On-Peak Today Cost", "psegli:on_peak_usage", "day", True, "sensor.pseg_rate_194_peak"),
    PSEGPeriodSensorDescription("on_peak_week_usage", "On-Peak Week Usage", "psegli:on_peak_usage", "week", False),
    PSEGPeriodSensorDescription("on_peak_week_cost", "On-Peak Week Cost", "psegli:on_peak_usage", "week", True, "sensor.pseg_rate_194_peak"),
    PSEGPeriodSensorDescription("on_peak_month_usage", "On-Peak Month Usage", "psegli:on_peak_usage", "month", False),
    PSEGPeriodSensorDescription("on_peak_month_cost", "On-Peak Month Cost", "psegli:on_peak_usage", "month", True, "sensor.pseg_rate_194_peak"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[..., None],
) -> None:
    """Set up PSEG period sensors from a config entry."""
    async_add_entities(
        [PSEGPeriodSensor(hass, entry, description) for description in SENSORS],
        True,
    )


class PSEGPeriodSensor(SensorEntity):
    """Expose an external recorder statistic as a current period total."""

    _attr_should_poll = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        description: PSEGPeriodSensorDescription,
    ) -> None:
        self.hass = hass
        self._description = description
        self._attr_name = f"PSEG {description.name}"
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_native_value: float | None = None

        if description.is_cost:
            self._attr_device_class = SensorDeviceClass.MONETARY
            self._attr_native_unit_of_measurement = hass.config.currency
            self._attr_icon = "mdi:currency-usd"
        else:
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_icon = "mdi:transmission-tower"

    async def async_update(self) -> None:
        """Read the statistic change for today, this week, or this month."""
        now_local = dt_util.now()
        if self._description.period == "day":
            start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        elif self._description.period == "week":
            start_local = (now_local - timedelta(days=now_local.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
        else:
            start_local = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        start_utc = start_local.astimezone(timezone.utc)
        end_utc = datetime.now(timezone.utc)
        lookback_utc = start_utc - timedelta(days=7)

        try:
            result = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                lookback_utc,
                end_utc,
                [self._description.statistic_id],
                "hour",
                None,
                {"start", "sum"},
            )
            rows = result.get(self._description.statistic_id, []) if result else []
            points: list[tuple[datetime, float]] = []
            for row in rows:
                if row.get("sum") is None or row.get("start") is None:
                    continue
                row_start: Any = row["start"]
                if isinstance(row_start, str):
                    timestamp = datetime.fromisoformat(row_start)
                elif isinstance(row_start, (int, float)):
                    timestamp = datetime.fromtimestamp(row_start, timezone.utc)
                else:
                    continue
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                points.append((timestamp, float(row["sum"])))

            points.sort(key=lambda point: point[0])

            current_points = [point for point in points if point[0] >= start_utc]
            if not current_points:
                # A valid statistic with no samples in this tariff period means
                # zero usage (for example, On-Peak on a weekend), not unknown.
                self._attr_native_value = 0.0 if points else None
                return

            prior_points = [point for point in points if point[0] < start_utc]
            baseline = prior_points[-1][1] if prior_points else 0.0
            value = max(0.0, current_points[-1][1] - baseline)

            if self._description.rate_entity_id:
                rate_state = self.hass.states.get(self._description.rate_entity_id)
                try:
                    rate = float(rate_state.state) if rate_state is not None else None
                except (TypeError, ValueError):
                    rate = None
                if rate is None or rate <= 0:
                    self._attr_native_value = None
                    return
                value *= rate

            self._attr_native_value = round(
                value, 2 if self._description.is_cost else 3
            )
        except Exception as err:
            _LOGGER.warning(
                "Could not update %s from %s: %s",
                self.entity_id,
                self._description.statistic_id,
                err,
            )
