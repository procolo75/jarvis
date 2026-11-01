"""Constants for the JARVIS conversation agent."""

DOMAIN = "jarvis"
CONF_URL = "url"
CONF_TOKEN = "token"
# JARVIS may run a few tool rounds and, on a rate-limited provider, wait for the
# minute to roll over; a voice answer that late is still better than none.
TIMEOUT = 90
