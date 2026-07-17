from playwright.sync_api import sync_playwright
class Browser:
    def __init__(self):
        self.p=None
        self.browser=None
        self.page=None
    def start(self):
        self.p=sync_playwright().start()
        self.browser=self.p.chromium.launch(
            headless=True
        )
        self.page=self.browser.new_page()
        return self.page
    def close(self):
        if self.browser:
            self.browser.close()
        if self.p:
            self.p.stop()