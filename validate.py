"""Validation script: navigates to example.com, extracts the title, saves a screenshot."""

from playwright.sync_api import sync_playwright


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("https://example.com")

        title = page.title()
        print(f"Extracted title: {title}")

        page.screenshot(path="/output/screenshot.png", full_page=True)
        print("Screenshot saved to /output/screenshot.png")

        # Save DOM snapshot
        html = page.content()
        with open("/output/dom_snapshot.html", "w") as f:
            f.write(html)
        print("DOM snapshot saved to /output/dom_snapshot.html")

        browser.close()


if __name__ == "__main__":
    main()
