"""Internal execution boundary for transparent aspects (first stage)."""

from .fabric import CallContext, ExecutionFabric, Observation

__all__ = ["CallContext", "ExecutionFabric", "Observation"]
