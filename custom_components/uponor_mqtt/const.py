DOMAIN = "uponor_mqtt"
HA_DISCOVERY_PREFIX = "homeassistant"

CONF_READ_TOPIC = "read_topic"
CONF_WRITE_TOPIC = "write_topic"
CONF_DISCOVERY_PREFIX = "discovery_prefix"
CONF_SYSTEM_ID = "system_id"

DEFAULT_NAME = "Uponor MQTT Bridge"
DEFAULT_READ_TOPIC = "uponor_read"
DEFAULT_WRITE_TOPIC = "uponor_write"

CONF_CAPTURE = "capture"
CAPTURE_FILE = "uponor_capture.jsonl"

# Experimental virtual thermostat
CONF_VIRTUAL_HOST = "virtual_tcp_host"
CONF_VIRTUAL_PORT = "virtual_tcp_port"
CONF_VIRTUAL_THERMOSTATS = "virtual_thermostats"
# Keys of each entry in CONF_VIRTUAL_THERMOSTATS
CONF_DEVICE_ID = "device_id"
CONF_TEMPERATURE_ENTITY = "temperature_entity"
CONF_HUMIDITY_ENTITY = "humidity_entity"
DEFAULT_VIRTUAL_PORT = 8899
