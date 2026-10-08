"""Backward-compatibility facade for pipeline.parser."""

import sys
import pipeline.parser as _impl

sys.modules[__name__] = _impl