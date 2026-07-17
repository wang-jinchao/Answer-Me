from playwright.sync_api import sync_playwright

class Browser:
    """Context-managed wrapper around Playwright sync API.

    Usage:
        with Browser() as page:
            page.goto(url)
    """

    def __init__(self, headless: bool = True):
        self.p = None
        self.browser = None
        self.page = None
        self.headless = headless

    def start(self):
        # Start Playwright and launch a new browser instance.
        self.p = sync_playwright().start()
        self.browser = self.p.chromium.launch(headless=self.headless)
        self.page = self.browser.new_page()
        return self.page

    def close(self):
        # Close browser and stop Playwright. Safe to call multiple times.
        try:
            if self.page:
                try:
                    self.page.close()
                except Exception:
                    # best-effort close
                    pass
            if self.browser:
                try:
                    self.browser.close()
                except Exception:
                    pass
        finally:
            if self.p:
                try:
                    self.p.stop()
                except Exception:
                    pass

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.close()
        # Do not suppress exceptions
        return False