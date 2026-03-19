# Compatibility shim:
# Canonical lifecycle primitive lives in xyn-platform.
# Keep this package only for narrow xyn-core object integrations.
from core.lifecycle.definitions import LifecycleDefinition, get_lifecycle_definition, supported_lifecycles
from core.lifecycle.service import (
    InvalidTransitionError,
    LifecycleError,
    MissingStateError,
    UnknownLifecycleError,
    apply_transition,
    transition_model_status,
)

__all__ = [
    "LifecycleDefinition",
    "LifecycleError",
    "UnknownLifecycleError",
    "MissingStateError",
    "InvalidTransitionError",
    "get_lifecycle_definition",
    "supported_lifecycles",
    "apply_transition",
    "transition_model_status",
]
