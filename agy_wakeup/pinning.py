"""Opt-in check that the agy process that was primed is still the same process."""
from typing import Optional

import psutil


class AgyProcessPin:
    # A pid alone can be reused by an unrelated process; pid plus start time cannot.
    @staticmethod
    def started_at(pid: int) -> Optional[float]:
        try:
            return psutil.Process(pid).create_time()
        except psutil.Error:
            return None

    @classmethod
    def still_running(cls, pid: int, started: float) -> bool:
        current = cls.started_at(pid)
        return current is not None and abs(current - started) < 0.01
