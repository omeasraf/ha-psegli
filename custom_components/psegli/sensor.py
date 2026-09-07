"""Period usage, cost, and comparison sensors for PSEG Long Island."""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(minutes=1)

OFF_PEAK_STATISTIC = "psegli:off_peak_usage"
ON_PEAK_STATISTIC = "psegli:on_peak_usage"


@dataclass(frozen=True)
class StatisticComponent:
    """One recorder statistic contributing to a sensor."""

    statistic_id: str
    rate_entity_id: str | None = None


@dataclass(frozen=True)
class PSEGPeriodSensorDescription:
    """Describe one PSEG period summary sensor."""

    key: str
    name: str
    period: str
    components: tuple[StatisticComponent, ...]
    value_type: str = "energy"
    calculation: str = "total"


OFF_PEAK = (StatisticComponent(OFF_PEAK_STATISTIC),)
ON_PEAK = (StatisticComponent(ON_PEAK_STATISTIC),)
TOTAL = (StatisticComponent(OFF_PEAK_STATISTIC), StatisticComponent(ON_PEAK_STATISTIC))
OFF_PEAK_COST = (
    StatisticComponent(OFF_PEAK_STATISTIC, "sensor.pseg_rate_194_off_peak"),
)
ON_PEAK_COST = (
    StatisticComponent(ON_PEAK_STATISTIC, "sensor.pseg_rate_194_peak"),
)
TOTAL_COST = (
    StatisticComponent(OFF_PEAK_STATISTIC, "sensor.pseg_rate_194_off_peak"),
    StatisticComponent(ON_PEAK_STATISTIC, "sensor.pseg_rate_194_peak"),
)


SENSORS = (
    # Existing tariff-period sensors.
    PSEGPeriodSensorDescription("off_peak_today_usage", "Off-Peak Today Usage", "day", OFF_PEAK),
    PSEGPeriodSensorDescription("off_peak_today_cost", "Off-Peak Today Cost", "day", OFF_PEAK_COST, "cost"),
    PSEGPeriodSensorDescription("off_peak_week_usage", "Off-Peak Week Usage", "week", OFF_PEAK),
    PSEGPeriodSensorDescription("off_peak_week_cost", "Off-Peak Week Cost", "week", OFF_PEAK_COST, "cost"),
    PSEGPeriodSensorDescription("off_peak_month_usage", "Off-Peak Month Usage", "month", OFF_PEAK),
    PSEGPeriodSensorDescription("off_peak_month_cost", "Off-Peak Month Cost", "month", OFF_PEAK_COST, "cost"),
    PSEGPeriodSensorDescription("on_peak_today_usage", "On-Peak Today Usage", "day", ON_PEAK),
    PSEGPeriodSensorDescription("on_peak_today_cost", "On-Peak Today Cost", "day", ON_PEAK_COST, "cost"),
    PSEGPeriodSensorDescription("on_peak_week_usage", "On-Peak Week Usage", "week", ON_PEAK),
    PSEGPeriodSensorDescription("on_peak_week_cost", "On-Peak Week Cost", "week", ON_PEAK_COST, "cost"),
    PSEGPeriodSensorDescription("on_peak_month_usage", "On-Peak Month Usage", "month", ON_PEAK),
    PSEGPeriodSensorDescription("on_peak_month_cost", "On-Peak Month Cost", "month", ON_PEAK_COST, "cost"),
    # Combined summaries matching the useful cards and comparisons on PSEG's dashboard.
    PSEGPeriodSensorDescription("today_usage", "Today Usage", "day", TOTAL),
    PSEGPeriodSensorDescription("yesterday_usage", "Yesterday Usage", "yesterday", TOTAL),
    PSEGPeriodSensorDescription("week_usage", "This Week Usage", "week", TOTAL),
    PSEGPeriodSensorDescription("last_week_usage", "Last Week Usage", "last_week", TOTAL),
    PSEGPeriodSensorDescription("month_usage", "This Month Usage", "month", TOTAL),
    PSEGPeriodSensorDescription("rolling_7_day_usage", "Rolling 7-Day Usage", "rolling_7_days", TOTAL),
    PSEGPeriodSensorDescription(
        "seven_day_daily_average",
        "7-Day Daily Average",
        "last_7_complete_days",
        TOTAL,
        calculation="daily_average",
    ),
    PSEGPeriodSensorDescription(
        "yesterday_change",
        "Yesterday Usage Change",
        "yesterday",
        TOTAL,
        "percentage",
        "percent_change",
    ),
    PSEGPeriodSensorDescription(
        "last_week_change",
        "Last Week Usage Change",
        "last_week",
        TOTAL,
        "percentage",
        "percent_change",
    ),
    PSEGPeriodSensorDescription(
        "on_peak_share",
        "On-Peak Share (Last 7 Days)",
        "last_7_complete_days",
        (StatisticComponent(ON_PEAK_STATISTIC), StatisticComponent(OFF_PEAK_STATISTIC)),
        "percentage",
        "share",
    ),
    PSEGPeriodSensorDescription("today_cost", "Today Cost", "day", TOTAL_COST, "cost"),
    PSEGPeriodSensorDescription("yesterday_cost", "Yesterday Cost", "yesterday", TOTAL_COST, "cost"),
    PSEGPeriodSensorDescription("week_cost", "This Week Cost", "week", TOTAL_COST, "cost"),
    PSEGPeriodSensorDescription("last_week_cost", "Last Week Cost", "last_week", TOTAL_COST, "cost"),
    PSEGPeriodSensorDescription("month_cost", "This Month Cost", "month", TOTAL_COST, "cost"),
)


def _row_timestamp(value: Any) -> datetime | None:
    """Normalize a recorder statistic timestamp to UTC."""
    if isinstance(value, str):
        timestamp = datetime.fromisoformat(value)
    elif isinstance(value, (int, float)):
        timestamp = datetime.fromtimestamp(value, timezone.utc)
    else:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _statistic_delta(rows: list[dict[str, Any]], start: datetime, end: datetime) -> float | None:
    """Return the cumulative-statistic change inside [start, end)."""
    points: list[tuple[datetime, float]] = []
    for row in rows:
        if row.get("sum") is None or row.get("start") is None:
            continue
        timestamp = _row_timestamp(row["start"])
        if timestamp is not None:
            points.append((timestamp, float(row["sum"])))

    points.sort(key=lambda point: point[0])
    if not points:
        return None

    in_period = [point for point in points if start <= point[0] < end]
    if not in_period:
        return 0.0

    before_period = [point for point in points if point[0] < start]
    baseline = before_period[-1][1] if before_period else 0.0
    return max(0.0, in_period[-1][1] - baseline)


def _period_range(period: str, now_local: datetime) -> tuple[datetime, datetime]:
    """Return local start/end boundaries, using PSEG's Sunday-based weeks."""
    today = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    this_week = today - timedelta(days=(today.weekday() + 1) % 7)

    if period == "day":
        return today, now_local
    if period == "yesterday":
        return today - timedelta(days=1), today
    if period == "week":
        return this_week, now_local
    if period == "last_week":
        return this_week - timedelta(days=7), this_week
    if period == "month":
        return today.replace(day=1), now_local
    if period == "rolling_7_days":
        return now_local - timedelta(days=7), now_local
    if period == "last_7_complete_days":
        return today - timedelta(days=7), today
    raise ValueError(f"Unknown PSEG sensor period: {period}")


class PSEGRecorderCoordinator(DataUpdateCoordinator[dict[str, list[dict[str, Any]]]]):
    """Read all PSEG recorder statistics once for every sensor update cycle."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="PSEG period summaries",
            config_entry=entry,
            update_interval=SCAN_INTERVAL,
            always_update=False,
        )

    async def _async_update_data(self) -> dict[str, list[dict[str, Any]]]:
        now_local = dt_util.now()
        start_local = now_local - timedelta(days=45)
        result = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start_local.astimezone(timezone.utc),
            datetime.now(timezone.utc),
            [OFF_PEAK_STATISTIC, ON_PEAK_STATISTIC],
            "hour",
            None,
            {"start", "sum"},
        )
        return result or {}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: Callable[..., None],
) -> None:
    """Set up PSEG period sensors from a config entry."""
    coordinator = PSEGRecorderCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    async_add_entities(
        [PSEGPeriodSensor(hass, entry, coordinator, description) for description in SENSORS]
    )


class PSEGPeriodSensor(CoordinatorEntity[PSEGRecorderCoordinator], SensorEntity):
    """Expose PSEG recorder statistics as period summaries and comparisons."""

    _attr_has_entity_name = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: PSEGRecorderCoordinator,
        description: PSEGPeriodSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.hass = hass
        self._description = description
        self._attr_name = f"PSEG {description.name}"
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"

        if description.value_type == "cost":
            self._attr_device_class = SensorDeviceClass.MONETARY
            self._attr_native_unit_of_measurement = hass.config.currency
            self._attr_icon = "mdi:currency-usd"
        elif description.value_type == "percentage":
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_icon = "mdi:percent"
        else:
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_icon = "mdi:transmission-tower"

    def _component_values(self, start: datetime, end: datetime) -> list[float] | None:
        values: list[float] = []
        for component in self._description.components:
            rows = self.coordinator.data.get(component.statistic_id, [])
            value = _statistic_delta(rows, start, end)
            if value is None:
                return None

            if self._description.value_type == "cost":
                rate_state = self.hass.states.get(component.rate_entity_id)
                try:
                    rate = float(rate_state.state) if rate_state is not None else None
                except (TypeError, ValueError):
                    rate = None
                if rate is None or rate <= 0:
                    return None
                value *= rate

            values.append(value)
        return values

    @property
    def native_value(self) -> float | None:
        """Calculate the current summary from the shared recorder snapshot."""
        start_local, end_local = _period_range(self._description.period, dt_util.now())
        start = start_local.astimezone(timezone.utc)
        end = end_local.astimezone(timezone.utc)
        values = self._component_values(start, end)
        if values is None:
            return None

        calculation = self._description.calculation
        if calculation == "share":
            total = sum(values)
            value = (values[0] / total * 100) if total > 0 else 0.0
        elif calculation == "percent_change":
            duration = end - start
            previous_values = self._component_values(start - duration, start)
            if previous_values is None:
                return None
            previous = sum(previous_values)
            if previous <= 0:
                return None
            value = (sum(values) - previous) / previous * 100
        elif calculation == "daily_average":
            value = sum(values) / 7
        else:
            value = sum(values)

        decimals = 2 if self._description.value_type in ("cost", "percentage") else 3
        return round(value, decimals)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose date boundaries and tariff breakdowns for dashboards."""
        start, end = _period_range(self._description.period, dt_util.now())
        attributes: dict[str, Any] = {
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
        }
        values = self._component_values(
            start.astimezone(timezone.utc),
            end.astimezone(timezone.utc),
        )
        if values is not None and len(values) == 2:
            suffix = "cost" if self._description.value_type == "cost" else "kwh"
            for component, value in zip(self._description.components, values):
                component_name = component.statistic_id.split(":", 1)[-1].removesuffix("_usage")
                attributes[f"{component_name}_{suffix}"] = round(value, 3)
        return attributes
