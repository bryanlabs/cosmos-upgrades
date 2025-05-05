import threading
from datetime import datetime

class ProgressTracker:
    """Utility class to track progress of batch operations."""

    def __init__(self, total, logger=None, update_frequency=5, desc="Progress"):
        """
        Initialize a progress tracker.

        Args:
            total: Total number of items to process
            logger: Logger object to use for logging progress
            update_frequency: How often to log progress (e.g., every 5 items)
            desc: Description of the progress being tracked
        """
        self.total = total
        self.completed = 0
        self.start_time = datetime.now()
        self.logger = logger
        self.update_frequency = update_frequency
        self.desc = desc
        self.lock = threading.Lock()

    def update(self, increment=1):
        """
        Update progress counter.

        Args:
            increment: How many items to add to the completed count

        Returns:
            Current progress percentage
        """
        with self.lock:
            self.completed += increment
            percentage = (self.completed / self.total) * 100 if self.total > 0 else 100

            # Log if we hit the frequency threshold or completed all items
            should_log = (self.completed % self.update_frequency == 0 or
                          self.completed == self.total)

            if should_log and self.logger:
                elapsed = (datetime.now() - self.start_time).total_seconds()
                items_per_sec = self.completed / elapsed if elapsed > 0 else 0

                # Estimate time remaining
                if items_per_sec > 0 and self.completed < self.total:
                    remaining_items = self.total - self.completed
                    eta_seconds = remaining_items / items_per_sec
                    minutes, seconds = divmod(int(eta_seconds), 60)
                    eta_str = f", ETA: {minutes}m {seconds}s" if minutes > 0 else f", ETA: {seconds}s"
                else:
                    eta_str = ""

                self.logger.info(
                    f"{self.desc}: {self.completed}/{self.total} ({percentage:.1f}%){eta_str}"
                )

            return percentage

    def get_stats(self):
        """
        Get current statistics about the progress.

        Returns:
            Dict with progress statistics
        """
        elapsed = (datetime.now() - self.start_time).total_seconds()
        return {
            "completed": self.completed,
            "total": self.total,
            "percentage": (self.completed / self.total * 100) if self.total > 0 else 100,
            "elapsed_seconds": elapsed,
            "items_per_second": self.completed / elapsed if elapsed > 0 else 0
        }
