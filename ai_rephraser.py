"""Backward-compatibility facade for services.ai_rephraser."""

import sys
import services.ai_rephraser as _impl

sys.modules[__name__] = _impl