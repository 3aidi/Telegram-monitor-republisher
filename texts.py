"""Backward-compatibility facade for core.texts."""

import sys
import core.texts as _impl

sys.modules[__name__] = _impl
