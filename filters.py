"""Backward-compatibility facade for pipeline.filters."""

import sys
import pipeline.filters as _impl

sys.modules[__name__] = _impl