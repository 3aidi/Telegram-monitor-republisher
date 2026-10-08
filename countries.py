"""Backward-compatibility facade for domain.countries."""

import sys
import domain.countries as _impl

sys.modules[__name__] = _impl