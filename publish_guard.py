"""Backward-compatibility facade for services.publish_guard."""

import sys
import services.publish_guard as _impl

sys.modules[__name__] = _impl