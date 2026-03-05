import json
import logging
from pathlib import Path

from playwright.async_api import async_playwright, BrowserContext, Page

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
COOKIES_FILE = DATA_DIR / "twitter_session.json"


async def _load_context(playwright):
    browser = await playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900},
    )
    if COOKIES_FILE.exists():
        cookies = json.loads(COOKIES_FILE.read_text())
        await context.add_cookies(cookies)
    return browser, context


async def _save_cookies(context: BrowserContext):
    DATA_DIR.mkdir(exist_ok=True)
    cookies = await context.cookies()
    COOKIES_FILE.write_text(json.dumps(cookies))


async def _is_logged_in(page: Page) -> bool:
    await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)
    try:
        await page.locator('[data-testid="SideNav_NewTweet_Button"]').wait_for(
            state="visible", timeout=8000
        )
        return True
    except Exception:
        return False


async def post_thread(tweets_data: list[dict]) -> list[str]:
    """
    Post a thread via browser automation.
    Each item: {"text": str, "image_paths": list[str]}
    """
    async with async_playwright() as p:
        browser, context = await _load_context(p)
        page = await context.new_page()

        try:
            if not await _is_logged_in(page):
                raise RuntimeError("Not logged in. Re-run setup_cookies.py.")

            await page.goto("https://x.com/compose/tweet", wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            for i, tweet in enumerate(tweets_data):
                text = tweet.get("text") or ""
                image_paths = tweet.get("image_paths") or []

                if i > 0:
                    add_btn = page.locator('[data-testid="addButton"][role="button"]')
                    await add_btn.wait_for(state="visible", timeout=10000)
                    await add_btn.click(force=True)
                    await page.wait_for_timeout(800)

                textarea = page.get_by_role("textbox", name="Post text").last
                await textarea.wait_for(state="visible", timeout=10000)
                if text:
                    await textarea.press_sequentially(text, delay=10)

                for img_path in image_paths[:4]:
                    if not Path(img_path).exists():
                        logger.warning(f"Image not found: {img_path}")
                        continue

                    # Count current attachments before upload
                    before = await page.locator('[data-testid="attachments"]').count()

                    file_input = page.locator('input[data-testid="fileInput"]').last
                    await file_input.set_input_files(img_path)
                    logger.info(f"Uploading {img_path}, attachments before: {before}")

                    # Wait for a NEW attachments container to appear (count increases)
                    try:
                        await page.wait_for_function(
                            f'document.querySelectorAll(\'[data-testid="attachments"]\').length > {before}',
                            timeout=20000,
                        )
                        await page.wait_for_timeout(1500)
                        logger.info(f"Image upload confirmed (attachments now: {await page.locator('[data-testid=\"attachments\"]').count()})")
                    except Exception:
                        logger.warning("Attachment count didn't increase — waiting 8s as fallback")
                        await page.wait_for_timeout(8000)

            post_btn = page.locator('[data-testid="tweetButtonInline"], [data-testid="tweetButton"]').first
            await post_btn.wait_for(state="visible", timeout=10000)
            await page.wait_for_timeout(2000)
            await post_btn.click()
            await page.wait_for_timeout(5000)

            await _save_cookies(context)
            logger.info("Thread posted via browser")
            return ["browser_ok"] * len(tweets_data)

        finally:
            await browser.close()
