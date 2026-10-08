"""Backward-compatibility facade for storage.db."""

import sys
import storage.db as _impl

sys.modules[__name__] = _impl