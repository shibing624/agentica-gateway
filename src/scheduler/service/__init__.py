"""Scheduler service package.

This package contains the core scheduler service components:
- state.py: State management and dependencies
- json_store.py: JSON file persistence layer
- ops.py: Core operations (add, remove, update, etc.)
- timer.py: Timer/scheduler logic
- events.py: Event system
"""
from .service import SchedulerService

__all__ = ["SchedulerService"]
