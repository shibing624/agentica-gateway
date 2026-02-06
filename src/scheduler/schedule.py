"""Schedule calculation utilities.

Computes next run times for different schedule types using croniter for cron expressions.
"""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .types import (
    Schedule,
    AtSchedule,
    EverySchedule,
    CronSchedule,
)


def now_ms() -> int:
    """Get current timestamp in milliseconds."""
    return int(time.time() * 1000)


def compute_next_run_at_ms(
    schedule: Schedule,
    current_ms: int | None = None,
    last_run_at_ms: int | None = None,
) -> int | None:
    """Compute the next run time in milliseconds.

    Args:
        schedule: The schedule configuration
        current_ms: Current timestamp in ms (defaults to now)
        last_run_at_ms: Last run timestamp in ms (for interval schedules)

    Returns:
        Next run timestamp in milliseconds, or None if no more runs
    """
    if current_ms is None:
        current_ms = now_ms()

    if isinstance(schedule, AtSchedule):
        return _compute_at_next(schedule, current_ms)
    elif isinstance(schedule, EverySchedule):
        return _compute_every_next(schedule, current_ms, last_run_at_ms)
    elif isinstance(schedule, CronSchedule):
        return _compute_cron_next(schedule, current_ms)
    else:
        return None


def _compute_at_next(schedule: AtSchedule, current_ms: int) -> int | None:
    """Compute next run for at (one-time) schedule."""
    # One-time schedule: return the target time if it's in the future
    if schedule.at_ms > current_ms:
        return schedule.at_ms
    return None  # Already passed


def _compute_every_next(
    schedule: EverySchedule,
    current_ms: int,
    last_run_at_ms: int | None,
) -> int | None:
    """Compute next run for interval schedule."""
    if schedule.interval_ms <= 0:
        return None

    if last_run_at_ms is None:
        # First run: schedule immediately or at next interval
        return current_ms + schedule.interval_ms

    # Calculate next run based on last run
    next_run = last_run_at_ms + schedule.interval_ms

    # If we're past the next run, align to future
    while next_run <= current_ms:
        next_run += schedule.interval_ms

    return next_run


def _compute_cron_next(schedule: CronSchedule, current_ms: int) -> int | None:
    """Compute next run for cron schedule using croniter.
    
    Supports both 5-part (minute precision) and 6-part (second precision) formats.
    """
    try:
        from croniter import croniter
    except ImportError:
        # Fallback: try to parse common patterns manually
        return _compute_cron_fallback(schedule, current_ms)

    try:
        # Convert ms to datetime in the specified timezone
        tz = ZoneInfo(schedule.timezone)
        current_dt = datetime.fromtimestamp(current_ms / 1000, tz=tz)

        # Check if 6-part expression (with seconds)
        parts = schedule.expression.split()
        if len(parts) == 6:
            # croniter supports 6-part with second_at_beginning=True
            cron = croniter(schedule.expression, current_dt, second_at_beginning=True)
        else:
            # Standard 5-part
            cron = croniter(schedule.expression, current_dt)

        # Get next run time
        next_dt = cron.get_next(datetime)

        # Convert back to milliseconds
        return int(next_dt.timestamp() * 1000)

    except Exception:
        return _compute_cron_fallback(schedule, current_ms)


def _compute_cron_fallback(schedule: CronSchedule, current_ms: int) -> int | None:
    """Fallback cron calculation for simple patterns when croniter is unavailable.
    
    Supports both 5-part and 6-part (with seconds) formats.
    """
    # Parse the expression
    parts = schedule.expression.split()
    
    # Handle both 5-part and 6-part formats
    if len(parts) == 5:
        second = "0"
        minute, hour, day, month, weekday = parts
    elif len(parts) == 6:
        second, minute, hour, day, month, weekday = parts
    else:
        return None

    try:
        tz = ZoneInfo(schedule.timezone)
        current_dt = datetime.fromtimestamp(current_ms / 1000, tz=tz)

        # Handle simple daily patterns: "0 9 * * *" or "30 0 9 * * *"
        if day == "*" and month == "*" and weekday == "*":
            if minute.isdigit() and hour.isdigit() and (second.isdigit() or second == "0"):
                target_second = int(second) if second.isdigit() else 0
                target_minute = int(minute)
                target_hour = int(hour)

                # Calculate next occurrence
                next_dt = current_dt.replace(
                    hour=target_hour,
                    minute=target_minute,
                    second=target_second,
                    microsecond=0,
                )

                # If past today's time, schedule for tomorrow
                if next_dt <= current_dt:
                    next_dt = next_dt.replace(day=next_dt.day + 1)

                return int(next_dt.timestamp() * 1000)

        # Handle interval patterns: "*/30 * * * *" (every 30 minutes)
        if minute.startswith("*/") and hour == "*" and day == "*" and month == "*" and weekday == "*":
            interval_minutes = int(minute[2:])
            current_minute = current_dt.minute

            # Find next aligned minute
            next_minute = ((current_minute // interval_minutes) + 1) * interval_minutes
            if next_minute >= 60:
                next_dt = current_dt.replace(minute=next_minute % 60, second=0, microsecond=0)
                next_dt = next_dt.replace(hour=next_dt.hour + 1)
            else:
                next_dt = current_dt.replace(minute=next_minute, second=0, microsecond=0)

            return int(next_dt.timestamp() * 1000)
        
        # Handle second-level interval: "*/30 * * * * *" (every 30 seconds)
        if len(parts) == 6 and second.startswith("*/") and minute == "*" and hour == "*":
            interval_seconds = int(second[2:])
            current_second = current_dt.second

            next_second = ((current_second // interval_seconds) + 1) * interval_seconds
            if next_second >= 60:
                next_dt = current_dt.replace(second=next_second % 60, microsecond=0)
                # Add one minute
                from datetime import timedelta
                next_dt = next_dt + timedelta(minutes=1)
            else:
                next_dt = current_dt.replace(second=next_second, microsecond=0)

            return int(next_dt.timestamp() * 1000)

    except Exception:
        pass

    # Unable to compute
    return None


def validate_cron_expression(expression: str) -> bool:
    """Validate a cron expression.

    Args:
        expression: Cron expression to validate (5-part or 6-part)

    Returns:
        True if valid, False otherwise
    """
    parts = expression.split()
    if len(parts) not in (5, 6):
        return False

    # Try using croniter for validation
    try:
        from croniter import croniter
        if len(parts) == 6:
            croniter(expression, second_at_beginning=True)
        else:
            croniter(expression)
        return True
    except ImportError:
        pass
    except Exception:
        return False

    # Basic validation without croniter
    for part in parts:
        if part == "*":
            continue
        if part.startswith("*/"):
            try:
                int(part[2:])
                continue
            except ValueError:
                return False
        if "-" in part or "," in part:
            continue  # Range or list, assume valid
        try:
            int(part)
        except ValueError:
            return False

    return True


def cron_to_human(expression: str, timezone: str = "Asia/Shanghai") -> str:  # noqa: ARG001
    """Convert cron expression to human-readable description.

    Args:
        expression: Cron expression (5-part or 6-part)
        timezone: Timezone for display

    Returns:
        Human-readable description in Chinese
    """
    parts = expression.split()
    if len(parts) not in (5, 6):
        return f"Cron: {expression}"

    # Parse 5-part or 6-part format
    if len(parts) == 6:
        second, minute, hour, day, month, weekday = parts
        has_seconds = True
    else:
        second = "0"
        minute, hour, day, month, weekday = parts
        has_seconds = False

    # Common patterns
    if day == "*" and month == "*":
        time_str = ""
        if minute.isdigit() and hour.isdigit():
            if has_seconds and second.isdigit() and int(second) > 0:
                time_str = f"{hour}:{minute.zfill(2)}:{second.zfill(2)}"
            elif minute == "0":
                time_str = f"{hour}:00"
            else:
                time_str = f"{hour}:{minute.zfill(2)}"
        
        if weekday == "*":
            # Daily
            return f"每天 {time_str}" if time_str else f"Cron: {expression}"
        else:
            # Weekly
            weekday_names = {
                "0": "周日", "1": "周一", "2": "周二", "3": "周三",
                "4": "周四", "5": "周五", "6": "周六", "7": "周日",
                "1-5": "工作日", "0,6": "周末", "6,0": "周末",
            }
            wd = weekday_names.get(weekday, f"周{weekday}")
            return f"每{wd} {time_str}" if time_str else f"Cron: {expression}"

    # Second-level interval: "*/30 * * * * *"
    if has_seconds and second.startswith("*/") and minute == "*" and hour == "*":
        interval = second[2:]
        return f"每隔 {interval} 秒"

    # Minute-level interval: "*/30 * * * *"
    if minute.startswith("*/") and hour == "*" and day == "*" and month == "*" and weekday == "*":
        interval = minute[2:]
        return f"每隔 {interval} 分钟"

    # Hour-level interval
    if hour.startswith("*/") and minute == "0" and day == "*" and month == "*" and weekday == "*":
        interval = hour[2:]
        return f"每隔 {interval} 小时"

    return f"Cron: {expression}"


def interval_to_human(interval_ms: int) -> str:
    """Convert interval in milliseconds to human-readable description.

    Args:
        interval_ms: Interval in milliseconds

    Returns:
        Human-readable description in Chinese
    """
    seconds = interval_ms // 1000

    if seconds < 60:
        return f"每隔 {seconds} 秒"
    elif seconds < 3600:
        minutes = seconds // 60
        return f"每隔 {minutes} 分钟"
    elif seconds < 86400:
        hours = seconds // 3600
        return f"每隔 {hours} 小时"
    else:
        days = seconds // 86400
        return f"每隔 {days} 天"


def schedule_to_human(schedule: Schedule) -> str:
    """Convert schedule to human-readable description.

    Args:
        schedule: Schedule configuration

    Returns:
        Human-readable description in Chinese
    """
    if isinstance(schedule, AtSchedule):
        if schedule.at_ms > 0:
            dt = datetime.fromtimestamp(schedule.at_ms / 1000)
            return f"在 {dt.strftime('%Y-%m-%d %H:%M:%S')} 执行一次"
        return "未设置执行时间"
    elif isinstance(schedule, EverySchedule):
        return interval_to_human(schedule.interval_ms)
    elif isinstance(schedule, CronSchedule):
        return cron_to_human(schedule.expression, schedule.timezone)
    return "未知调度类型"
