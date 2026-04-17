"""
Data Checker
────────────
Reads data usage from the sim24 data usage page.

Primary method: ARIA attributes (machine-readable KB values)
  aria-valuenow  = used KB
  aria-valuemax  = total KB

Fallback method: Text span parsing
  "98,30 GB" / "von 100,00 GB"
"""

import re
from playwright.async_api import Page
from typing import Tuple, Optional


class DataChecker:
    def __init__(self, page: Page):
        self.page = page

    async def get_usage(self) -> Tuple[Optional[int], Optional[int]]:
        """
        Returns (used_kb, total_kb) or (None, None) on failure.
        Values are in kilobytes.
        """
        # ── Method A: ARIA attributes (most reliable) ─────────────────────
        try:
            progressbar = await self.page.query_selector(
                ".e-data_usage_meter-data_total[role='progressbar']"
            )
            if progressbar:
                used_kb  = await progressbar.get_attribute("aria-valuenow")
                total_kb = await progressbar.get_attribute("aria-valuemax")

                if used_kb and total_kb:
                    print(f"[DATA] ARIA method — used: {used_kb} KB, total: {total_kb} KB")
                    return int(float(used_kb)), int(float(total_kb))
        except Exception as e:
            print(f"[DATA] ARIA method failed: {e}")

        # ── Method B: Text span parsing (fallback) ─────────────────────────
        try:
            used_text  = await self.page.inner_text(".font-weight-bold.pr-1")
            total_text = await self.page.inner_text(".l-txt-small.pr-2")

            used_gb  = self._parse_german_gb(used_text)
            total_gb = self._parse_german_gb(total_text)

            if used_gb is not None and total_gb is not None:
                # Convert GB to KB for consistency
                used_kb  = int(used_gb  * 1024 * 1024)
                total_kb = int(total_gb * 1024 * 1024)
                print(f"[DATA] Text method — used: {used_gb} GB, total: {total_gb} GB")
                return used_kb, total_kb
        except Exception as e:
            print(f"[DATA] Text method failed: {e}")

        return None, None

    @staticmethod
    def _parse_german_gb(text: str) -> Optional[float]:
        """
        Parses German-formatted GB strings.
        Handles: "98,30 GB", "von 100,00 GB", " 1,50 GB" etc.
        German locale uses comma as decimal separator.
        """
        text = text.strip()
        match = re.search(r"([\d]+[,.][\d]+)\s*GB", text)
        if match:
            number_str = match.group(1).replace(",", ".")
            return float(number_str)
        return None
