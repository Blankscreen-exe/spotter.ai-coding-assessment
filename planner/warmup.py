"""Do the slow one-off work when the server starts, not on the first request.

Two things are slow exactly once per process: loading the station index from
the database (about 50 ms) and the first TLS handshake with the routing
provider (several hundred ms; the connection is then kept open and reused).
"""

import logging

from django.db import DatabaseError, connections

from . import conf
from .providers import get_provider
from .services import server_settings
from .services.stations import get_index

logger = logging.getLogger(__name__)


def warm_up():
    try:
        stations = len(get_index().stations)
        provider = get_provider(server_settings.load()[conf.ROUTING_PROVIDER])
    except DatabaseError:
        # Fresh database: the server still starts, and the index loads on first use.
        logger.warning('Warm-up skipped: the database is not migrated or loaded yet.')
        return
    finally:
        connections.close_all()
    connected = provider.connect()
    logger.info('Warm-up: %d stations in memory, %s connection %s.',
                stations, provider.label, 'open' if connected else 'not available')
