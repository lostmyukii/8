"""Persistent platform services for the maze control dashboard."""

from .auth import AuthService, SessionPrincipal
from .config import PlatformConfig
from .control_lease import ControlLeaseService
from .database import Database
from .event_store import EventConflictError, EventStore
from .map_goal_resolver import (
    MapGoalResolutionError,
    MapGoalResolver,
    ResolvedMapGoal,
)

__all__ = [
    "AuthService",
    "ControlLeaseService",
    "Database",
    "EventConflictError",
    "EventStore",
    "MapGoalResolutionError",
    "MapGoalResolver",
    "PlatformConfig",
    "ResolvedMapGoal",
    "SessionPrincipal",
]
