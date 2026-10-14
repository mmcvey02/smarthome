"""Built-in integrations. Importing a module registers its integration type.

Vendor libraries are imported lazily inside each driver's start(), so only the
ones you actually configure need to be installed.
"""

from . import cync, demo, mrcool, roomba  # noqa: F401
from .base import REGISTRY, Integration, IntegrationError, register  # noqa: F401
