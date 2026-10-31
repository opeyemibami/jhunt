"""Every workflow's graph versions, registered in one place.

Each workflow package adds its versions here as it lands; there are none yet.
This is the only module that imports both the workflows and the runtime.
"""

from jhunt.platform.runtime.registry import Registry


def build_registry() -> Registry:
    registry = Registry()
    # e.g. registry.register("capture", 1, capture.build_v1)  -- from week 2
    return registry
