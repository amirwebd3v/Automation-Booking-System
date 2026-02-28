"""
Decision Engine
───────────────
Simple threshold logic:
  If remaining data < threshold → book
  
The threshold is configurable (default 1.5 GB).
"""


class DecisionEngine:
    def __init__(self, threshold_gb: float = 1.5):
        self.threshold_gb = threshold_gb

    def should_book(self, remaining_gb: float) -> bool:
        """Returns True if remaining data is below the booking threshold."""
        result = remaining_gb < self.threshold_gb
        print(
            f"[DECISION] Remaining: {remaining_gb:.2f} GB | "
            f"Threshold: {self.threshold_gb} GB | "
            f"Book: {result}"
        )
        return result
