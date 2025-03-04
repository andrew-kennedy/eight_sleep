"""Support for Eight smart mattress covers and mattresses."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging

from .pyEight.eight import EightSleep
from .pyEight.exceptions import RequestError
from .pyEight.user import EightUser
import voluptuous as vol

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry
from homeassistant.const import (
    ATTR_HW_VERSION,
    ATTR_MANUFACTURER,
    ATTR_MODEL,
    ATTR_SW_VERSION,
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.device_registry import DeviceInfo, async_get
from homeassistant.helpers.typing import UNDEFINED, ConfigType
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import DOMAIN, NAME_MAP

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

HEAT_SCAN_INTERVAL = timedelta(seconds=60)
USER_SCAN_INTERVAL = timedelta(seconds=300)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_USERNAME): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
                vol.Optional(CONF_CLIENT_ID): cv.string,
                vol.Optional(CONF_CLIENT_SECRET): cv.string,
            }
        ),
    },
    extra=vol.ALLOW_EXTRA,
)


@dataclass
class EightSleepConfigEntryData:
    """Data used for all entities for a given config entry."""

    api: EightSleep
    heat_coordinator: DataUpdateCoordinator
    user_coordinator: DataUpdateCoordinator


def _get_device_unique_id(eight: EightSleep, user_obj: EightUser | None = None) -> str:
    """Get the device's unique ID."""
    unique_id = eight.device_id
    assert unique_id
    if user_obj:
        unique_id = f"{unique_id}.{user_obj.user_id}.{user_obj.side}"
    return unique_id


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Old set up method for the Eight Sleep component."""
    if DOMAIN in config:
        _LOGGER.warning(
            "Your Eight Sleep configuration has been imported into the UI; "
            "please remove it from configuration.yaml as support for it "
            "will be removed in a future release"
        )
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT}, data=config[DOMAIN]
            )
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the Eight Sleep config entry."""
    if CONF_CLIENT_ID in entry.data:
        client_id = entry.data[CONF_CLIENT_ID]
    else:
        client_id = None
    if CONF_CLIENT_SECRET in entry.data:
        client_secret = entry.data[CONF_CLIENT_SECRET]
    else:
        client_secret = None
    eight = EightSleep(
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
        hass.config.time_zone,
        client_id,
        client_secret,
        client_session=async_get_clientsession(hass),
    )
    # Authenticate, build sensors
    try:
        success = await eight.start()
    except RequestError as err:
        raise ConfigEntryNotReady from err
    if not success:
        # Authentication failed, cannot continue
        return False

    heat_coordinator: DataUpdateCoordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{DOMAIN}_heat",
        update_interval=HEAT_SCAN_INTERVAL,
        update_method=eight.update_device_data,
    )
    user_coordinator: DataUpdateCoordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{DOMAIN}_user",
        update_interval=USER_SCAN_INTERVAL,
        update_method=eight.update_user_data,
    )
    await heat_coordinator.async_config_entry_first_refresh()
    await user_coordinator.async_config_entry_first_refresh()

    if not eight.users:
        # No users, cannot continue
        return False

    dev_reg = async_get(hass)
    assert eight.device_data
    device_data = {
        ATTR_MANUFACTURER: "Eight Sleep",
        ATTR_MODEL: eight.device_data.get("modelString", UNDEFINED),
        ATTR_HW_VERSION: eight.device_data.get("sensorInfo", {}).get(
            "hwRevision", UNDEFINED
        ),
        ATTR_SW_VERSION: eight.device_data.get("firmwareVersion", UNDEFINED),
    }
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, _get_device_unique_id(eight))},
        name=f"{entry.data[CONF_USERNAME]}'s Eight Sleep",
        **device_data,
    )
    for user in eight.users.values():
        assert user.user_profile
        dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, _get_device_unique_id(eight, user))},
            name=f"{user.user_profile['firstName']}'s Eight Sleep Side",
            via_device=(DOMAIN, _get_device_unique_id(eight)),
            **device_data,
        )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = EightSleepConfigEntryData(
        eight, heat_coordinator, user_coordinator
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        # stop the API before unloading everything
        config_entry_data: EightSleepConfigEntryData = hass.data[DOMAIN][entry.entry_id]
        await config_entry_data.api.stop()
        hass.data[DOMAIN].pop(entry.entry_id)
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)

    return unload_ok


class EightSleepBaseEntity(CoordinatorEntity[DataUpdateCoordinator]):
    """The base Eight Sleep entity class."""

    def __init__(
        self,
        entry: ConfigEntry,
        coordinator: DataUpdateCoordinator,
        eight: EightSleep,
        user_id: str | None,
        sensor: str,
    ) -> None:
        """Initialize the data object."""
        super().__init__(coordinator)
        self._config_entry = entry
        self._eight = eight
        self._user_id = user_id
        self._sensor = sensor
        self._user_obj: EightUser | None = None
        if user_id:
            self._user_obj = self._eight.users[user_id]

        mapped_name = str(NAME_MAP.get(sensor, sensor.replace("_", " ").title()))

        if self._user_obj is not None:
            assert self._user_obj.user_profile
            name = f"{self._user_obj.user_profile['firstName']}'s {mapped_name}"
            self._attr_name = name
        else:
            self._attr_name = f"Eight Sleep {mapped_name}"
        unique_id = f"{_get_device_unique_id(eight, self._user_obj)}.{sensor}"
        self._attr_unique_id = unique_id
        identifiers = {(DOMAIN, _get_device_unique_id(eight, self._user_obj))}
        self._attr_device_info = DeviceInfo(identifiers=identifiers)

    async def _generic_service_call(self, service_method):
        if self._user_obj is None:
            raise HomeAssistantError(
                "This entity does not support the service call. Ensure you have a target <xxx>_bed_temperature entity set as the target."
            )
        await service_method()
        config_entry_data: EightSleepConfigEntryData = self.hass.data[DOMAIN][
            self._config_entry.entry_id
        ]
        await config_entry_data.heat_coordinator.async_request_refresh()

    async def async_heat_set(
        self, target: int, duration: int, sleep_stage: str
    ) -> None:
        """Handle eight sleep heat set calls."""
        if sleep_stage == "current":
            await self._generic_service_call(
                lambda: self._user_obj.set_heating_level(target, duration)
            )
        else:
            await self._generic_service_call(
                lambda: self._user_obj.set_smart_heating_level(target, sleep_stage)
            )

    async def async_heat_increment(self, target: int) -> None:
        """Handle eight sleep heat increment calls."""
        await self._generic_service_call(
            lambda: self._user_obj.increment_heating_level(target)
        )

    async def async_side_off(
        self,
    ) -> None:
        """Handle eight sleep side off calls."""
        await self._generic_service_call(self._user_obj.turn_off_side)

    async def async_side_on(
        self,
    ) -> None:
        """Handle eight sleep side on calls."""
        await self._generic_service_call(self._user_obj.turn_on_side)

    async def async_alarm_snooze(self, duration: int) -> None:
        """Handle eight sleep alarm snooze calls."""
        await self._generic_service_call(lambda: self._user_obj.alarm_snooze(duration))

    async def async_alarm_stop(self) -> None:
        """Handle eight sleep alarm stop calls."""
        await self._generic_service_call(self._user_obj.alarm_stop)

    async def async_start_away_mode(
        self,
    ) -> None:
        """Handle eight sleep start away mode calls."""
        await self._generic_service_call(lambda: self._user_obj.set_away_mode("start"))

    async def async_stop_away_mode(
        self,
    ) -> None:
        """Handle eight sleep start away mode calls."""
        await self._generic_service_call(lambda: self._user_obj.set_away_mode("end"))

    async def async_prime_pod(
        self,
    ) -> None:
        """Handle eight sleep prime pod calls."""
        await self._generic_service_call(self._user_obj.prime_pod)

    async def async_set_bed_side(self, bed_side_state: str) -> None:
        """Handle eight sleep set bide side state."""
        await self._generic_service_call(
            lambda: self._user_obj.set_bed_side(bed_side_state)
        )
