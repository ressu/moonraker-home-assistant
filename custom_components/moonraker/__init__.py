"""
Moonraker integration for Home Assistant
"""
import asyncio
from datetime import timedelta
import logging
import os.path

import async_timeout
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Config, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import MoonrakerApiClient
from .const import (
    CONF_API_KEY,
    CONF_PORT,
    CONF_PRINTER_NAME,
    CONF_TLS,
    CONF_URL,
    DOMAIN,
    HOSTNAME,
    METHODS,
    OBJ,
    PLATFORMS,
    TIMEOUT,
)
from .sensor import SENSORS

SCAN_INTERVAL = timedelta(seconds=30)

_LOGGER = logging.getLogger(__name__)

_LOGGER.debug("loading moonraker init")


async def async_setup(_hass: HomeAssistant, _config: Config):
    """Set up this integration using YAML is not supported."""
    return True


def get_user_name(hass: HomeAssistant, entry: ConfigEntry):
    device_registry = dr.async_get(hass)
    device_entries = dr.async_entries_for_config_entry(device_registry, entry.entry_id)

    if len(device_entries) < 1:
        return None

    return device_entries[0].name_by_user


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up this integration using UI."""
    if hass.data.get(DOMAIN) is None:
        hass.data.setdefault(DOMAIN, {})

    custom_name = get_user_name(hass, entry)

    url = entry.data.get(CONF_URL)
    port = entry.data.get(CONF_PORT)
    tls = entry.data.get(CONF_TLS)
    api_key = entry.data.get(CONF_API_KEY)
    printer_name = (
        entry.data.get(CONF_PRINTER_NAME) if custom_name is None else custom_name
    )

    api = MoonrakerApiClient(
        url,
        async_get_clientsession(hass, verify_ssl=False),
        port=port,
        api_key=api_key,
        tls=tls,
    )

    try:
        await api.start()
        async with async_timeout.timeout(TIMEOUT):
            printer_info = await api.client.call_method("printer.info")
            _LOGGER.debug(printer_info)

            if printer_name == "":
                api_device_name = printer_info[HOSTNAME]
            else:
                api_device_name = printer_name

            entry.title = api_device_name
    except Exception as exc:
        raise ConfigEntryNotReady(f"Error connecting to {url}:{port}") from exc

    coordinator = MoonrakerDataUpdateCoordinator(
        hass, client=api, config_entry=entry, api_device_name=api_device_name
    )

    await coordinator.async_refresh()

    if not coordinator.last_update_success:
        raise ConfigEntryNotReady

    hass.data[DOMAIN][entry.entry_id] = coordinator
    for platform in PLATFORMS:
        coordinator.platforms.append(platform)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


async def _printer_objects_updater(coordinator):
    return await coordinator._async_fetch_data(
        METHODS.PRINTER_OBJECTS_QUERY, coordinator.query_obj
    )


async def _printer_info_updater(coordinator):
    return {
        "printer.info": await coordinator._async_fetch_data(METHODS.PRINTER_INFO, None)
    }


async def _gcode_file_detail_updater(coordinator):
    data = await coordinator._async_fetch_data(
        METHODS.PRINTER_OBJECTS_QUERY, coordinator.query_obj
    )
    return await coordinator._async_get_gcode_file_detail(
        data["status"]["print_stats"]["filename"]
    )


class MoonrakerDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: MoonrakerApiClient,
        config_entry: ConfigEntry,
        api_device_name: str,
    ) -> None:
        """Initialize."""
        self.moonraker = client
        self.platforms = []
        self.updaters = [
            _printer_objects_updater,
            _printer_info_updater,
            _gcode_file_detail_updater,
        ]
        self.hass = hass
        self.config_entry = config_entry
        self.api_device_name = api_device_name
        self.query_obj = {OBJ: {}}
        self.load_sensor_data(SENSORS)

        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=SCAN_INTERVAL)

    async def _async_update_data(self):
        """Update data via library."""
        data = dict()

        for updater in self.updaters:
            data.update(await updater(self))

        return data

    async def _async_get_gcode_file_detail(self, gcode_filename):
        return_gcode = {
            "thumbnails_path": None,
            "estimated_time": 1,
            "filament_total": 1,
            "layer_count": None,
            "layer_height": None,
            "object_height": None,
            "first_layer_height": None,
        }
        if gcode_filename is None or gcode_filename == "":
            return return_gcode

        # Get prefix of the filename to get the appropriate thumbnail
        dirname = os.path.dirname(gcode_filename)

        query_object = {"filename": gcode_filename}
        gcode = await self._async_fetch_data(
            METHODS.SERVER_FILES_METADATA, query_object
        )
        return_gcode["estimated_time"] = (
            gcode["estimated_time"] if "estimated_time" in gcode else 0
        )
        return_gcode["object_height"] = (
            gcode["object_height"] if "object_height" in gcode else 0
        )
        return_gcode["filament_total"] = (
            gcode["filament_total"] if "filament_total" in gcode else 0
        )
        return_gcode["layer_count"] = (
            gcode["layer_count"] if "layer_count" in gcode else 0
        )
        return_gcode["layer_height"] = (
            gcode["layer_height"] if "layer_height" in gcode else 0
        )
        return_gcode["first_layer_height"] = (
            gcode["first_layer_height"] if "first_layer_height" in gcode else 0
        )

        try:
            # Keep last since this can fail but, we still want the other data
            path = gcode["thumbnails"][len(gcode["thumbnails"]) - 1]["relative_path"]

            return_gcode["thumbnails_path"] = os.path.join(dirname, path)
            return return_gcode
        except Exception as ex:
            _LOGGER.error("failed to get thumbnails  {%s}", ex)
            _LOGGER.error("Query Object {%s}", query_object)
            _LOGGER.error("gcode {%s}", gcode)
            return return_gcode

    async def _async_fetch_data(
        self, query_path: METHODS, query_object, quiet: bool = False
    ):
        if not self.moonraker.client.is_connected:
            _LOGGER.warning("connection to moonraker down, restarting")
            await self.moonraker.start()
        try:
            if query_object is None:
                result = await self.moonraker.client.call_method(query_path.value)
            else:
                result = await self.moonraker.client.call_method(
                    query_path.value, **query_object
                )
            if not quiet:
                _LOGGER.debug(result)
            return result
        except Exception as exception:
            raise UpdateFailed() from exception

    async def _async_send_data(self, query_path: METHODS, query_obj) -> None:
        if not self.moonraker.client.is_connected:
            _LOGGER.warning("connection to moonraker down, restarting")
            await self.moonraker.start()
        try:
            if query_obj is None:
                await self.moonraker.client.call_method(query_path.value)
            else:
                await self.moonraker.client.call_method(query_path.value, **query_obj)
        except Exception as exception:
            raise UpdateFailed() from exception

    async def async_fetch_data(
        self, query_path: METHODS, query_obj: dict[str:any] = None, quiet: bool = False
    ):
        """Fetch data from moonraker"""
        return await self._async_fetch_data(query_path, query_obj, quiet=quiet)

    async def async_send_data(
        self, query_path: METHODS, query_obj: dict[str:any] = None
    ):
        """Send data to moonraker"""
        return await self._async_send_data(query_path, query_obj)

    def add_data_updater(self, updater):
        self.updaters.append(updater)

    def load_sensor_data(self, sensor_list):
        """Loading sensor data, so we can poll the right object"""
        for sensor in sensor_list:
            for subscriptions in sensor.subscriptions:
                self.add_query_objects(subscriptions[0], subscriptions[1])

    def add_query_objects(self, query_object: str, result_key: str):
        """Building the list of object we want to retreive from the server"""
        if query_object not in self.query_obj[OBJ]:
            self.query_obj[OBJ][query_object] = []
        if result_key not in self.query_obj[OBJ][query_object]:
            self.query_obj[OBJ][query_object].append(result_key)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    coordinator = hass.data[DOMAIN][entry.entry_id]
    unloaded = all(
        await asyncio.gather(
            *[
                hass.config_entries.async_forward_entry_unload(entry, platform)
                for platform in PLATFORMS
                if platform in coordinator.platforms
            ]
        )
    )
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    hass.data[DOMAIN][entry.entry_id].config_entry = entry
    await hass.config_entries.async_reload(entry.entry_id)
