from playwright.sync_api import sync_playwright


class Browser:

    def __init__(self, headless: bool = True):
        self.p = None
        self.browser = None
        self.context = None
        self.page = None
        self.headless = headless

    def start(self):
        self.p = sync_playwright().start()
        self.browser = self.p.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )

        self.context = self.browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
        )
        self.page = self.context.new_page()
        return self.page

    def close(self):
        try:
            if self.context:
                try:
                    self.context.clear_cookies()
                except Exception:
                    pass
                try:
                    self.context.close()
                except Exception:
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
        return False
