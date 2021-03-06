from collections import defaultdict
import asyncio
from functools import partial
import logging

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import Entity
from homeassistant.components.sensor import (PLATFORM_SCHEMA, DOMAIN)
from homeassistant.const import (CONF_NAME, CONF_HOST, CONF_TOKEN,
                                 ATTR_ENTITY_ID, )
from homeassistant.exceptions import PlatformNotReady

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = 'Xiaomi Miio Device'
DATA_KEY = 'sensor.xiaomi_miio_raw'

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_HOST): cv.string,
    vol.Required(CONF_TOKEN): vol.All(cv.string, vol.Length(min=32, max=32)),
    vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
})

REQUIREMENTS = ['python-miio>=0.3.7']

ATTR_MODEL = 'model'
ATTR_FIRMWARE_VERSION = 'firmware_version'
ATTR_HARDWARE_VERSION = 'hardware_version'
ATTR_PROPERTIES = 'properties'
ATTR_METHOD = 'method'
ATTR_PARAMS = 'params'

SUCCESS = ['ok']

SERVICE_SCHEMA = vol.Schema({
    vol.Optional(ATTR_ENTITY_ID): cv.entity_ids,
})

SERVICE_SCHEMA_SET_PROPERTIES = SERVICE_SCHEMA.extend({
    vol.Optional(ATTR_PROPERTIES, default=['power']):
        vol.All(cv.ensure_list, [cv.string]),
})

SERVICE_SCHEMA_COMMAND = SERVICE_SCHEMA.extend({
    vol.Required(ATTR_METHOD): cv.string,
    vol.Optional(ATTR_PARAMS, default=[]):
        vol.All(cv.ensure_list),
})

SERVICE_CUSTOM_TURN_ON = 'xiaomi_miio_raw_turn_on'
SERVICE_CUSTOM_TURN_OFF = 'xiaomi_miio_raw_turn_off'
SERVICE_SET_PROPERTIES = 'xiaomi_miio_raw_set_properties'
SERVICE_COMMAND = 'xiaomi_miio_raw_command'

SERVICE_TO_METHOD = {
    SERVICE_CUSTOM_TURN_ON: {'method': 'async_turn_on'},
    SERVICE_CUSTOM_TURN_OFF: {'method': 'async_turn_off'},
    SERVICE_SET_PROPERTIES: {'method': 'async_set_properties',
                             'schema': SERVICE_SCHEMA_SET_PROPERTIES},
    SERVICE_COMMAND: {'method': 'async_command',
                      'schema': SERVICE_SCHEMA_COMMAND},
}


# pylint: disable=unused-argument
@asyncio.coroutine
def async_setup_platform(hass, config, async_add_devices, discovery_info=None):
    """Set up the sensor from config."""
    from miio import Device, DeviceException
    if DATA_KEY not in hass.data:
        hass.data[DATA_KEY] = {}

    host = config.get(CONF_HOST)
    name = config.get(CONF_NAME)
    token = config.get(CONF_TOKEN)

    _LOGGER.info("Initializing with host %s (token %s...)", host, token[:5])

    try:
        miio_device = Device(host, token)
        device_info = miio_device.info()
        model = device_info.model
        _LOGGER.info("%s %s %s detected",
                     model,
                     device_info.firmware_version,
                     device_info.hardware_version)

        device = XiaomiMiioGenericDevice(name, miio_device, device_info)
    except DeviceException:
        raise PlatformNotReady

    hass.data[DATA_KEY][host] = device
    async_add_devices([device], update_before_add=True)

    @asyncio.coroutine
    def async_service_handler(service):
        """Map services to methods on XiaomiMiioDevice."""
        method = SERVICE_TO_METHOD.get(service.service)
        params = {key: value for key, value in service.data.items()
                  if key != ATTR_ENTITY_ID}
        entity_ids = service.data.get(ATTR_ENTITY_ID)
        if entity_ids:
            devices = [device for device in hass.data[DATA_KEY].values() if
                       device.entity_id in entity_ids]
        else:
            devices = hass.data[DATA_KEY].values()

        update_tasks = []
        for device in devices:
            yield from getattr(device, method['method'])(**params)
            update_tasks.append(device.async_update_ha_state(True))

        if update_tasks:
            yield from asyncio.wait(update_tasks, loop=hass.loop)

    for service in SERVICE_TO_METHOD:
        schema = SERVICE_TO_METHOD[service].get('schema', SERVICE_SCHEMA)
        hass.services.async_register(
            DOMAIN, service, async_service_handler, schema=schema)


class XiaomiMiioGenericDevice(Entity):
    """Representation of a Xiaomi Air Quality Monitor."""

    def __init__(self, name, device, device_info):
        """Initialize the entity."""
        self._name = name
        self._device = device
        self._model = device_info.model
        self._unique_id = "{}-{}".format(device_info.model,
                                         device_info.mac_address)
        self._icon = 'mdi:flask-outline'
        self._unit_of_measurement = 'dBm'

        self._properties = ['power']
        self._available = None
        self._state = None
        self._state_attrs = {
            ATTR_MODEL: self._model,
            ATTR_FIRMWARE_VERSION: device_info.firmware_version,
            ATTR_HARDWARE_VERSION: device_info.hardware_version,
            ATTR_PROPERTIES: self._properties
        }

    @property
    def should_poll(self):
        """Poll the miio device."""
        return True

    @property
    def unique_id(self):
        """Return an unique ID."""
        return self._unique_id

    @property
    def name(self):
        """Return the name of this entity, if any."""
        return self._name

    @property
    def unit_of_measurement(self):
        """Return the unit of measurement of this entity, if any."""
        return self._unit_of_measurement

    @property
    def icon(self):
        """Return the icon to use for device if any."""
        return self._icon

    @property
    def available(self):
        """Return true when state is known."""
        return self._available

    @property
    def state(self):
        """Return the state of the device."""
        return self._state

    @property
    def device_state_attributes(self):
        """Return the state attributes of the device."""
        return self._state_attrs

    async def _try_command(self, mask_error, func, *args, **kwargs):
        """Call a device command handling error messages."""
        from miio import DeviceException
        try:
            result = await self.hass.async_add_job(
                partial(func, *args, **kwargs))

            _LOGGER.info("Response received from miio device: %s", result)

            return result == SUCCESS
        except DeviceException as exc:
            _LOGGER.error(mask_error, exc)
            return False

    async def async_update(self):
        """Fetch state from the miio device."""
        from miio import DeviceException

        try:
            values = await self.hass.async_add_job(self._device.send,
                                                   'get_prop',
                                                   self._properties)
            _LOGGER.debug("Response of the get properties call: %s", values)

            properties_count = len(self._properties)
            values_count = len(values)
            if properties_count != values_count:
                _LOGGER.debug(
                    "Count (%s) of requested properties does not match the "
                    "count (%s) of received values.",
                    properties_count, values_count)

            state = dict(
                defaultdict(lambda: None, zip(self._properties, values)))

            _LOGGER.info("New state: %s", state)

            self._available = True
            self._state_attrs.update(state)

        except DeviceException as ex:
            self._available = False
            _LOGGER.error("Got exception while fetching the state: %s", ex)

        try:
            device_info = await self.hass.async_add_job(self._device.info)
            self._state = device_info.accesspoint.get('rssi', self._model)

        except DeviceException as ex:
            self._available = False
            _LOGGER.error("Got exception while fetching device info: %s", ex)

    async def async_turn_on(self, **kwargs):
        """Turn the miio device on."""
        await self._try_command(
            "Turning the miio device on failed.", self._device.send,
            'set_power', ['on'])

    async def async_turn_off(self, **kwargs):
        """Turn the miio device off."""
        await self._try_command(
            "Turning the miio device off failed.", self._device.send,
            'set_power', ['off'])

    async def async_set_properties(self, properties: list):
        """Set properties. Will be retrieved on next update."""
        self._properties = properties
        self._state_attrs.update({
            ATTR_PROPERTIES: self._properties
        })

    async def async_command(self, method: str, params):
        """Send a raw command to the device."""
        _LOGGER.info("Sending command: %s %s" % (method, params))
        await self._try_command(
            "Turning the miio device on failed.", self._device.send,
            method, params)
