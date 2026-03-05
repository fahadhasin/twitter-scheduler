import asyncio
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


async def _wait_for_attachment(page: Page, before: int, timeout_s: int = 30) -> bool:
    """Poll until attachment count exceeds `before`. Returns True if confirmed."""
    for _ in range(timeout_s * 2):
        try:
            count = await page.locator('[data-testid="attachments"]').count()
            if count > before:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False


async def _verify_compose(page: Page, tweets_data: list[dict]) -> list[str]:
    """
    Verify the compose modal is correct before posting.
    Returns a list of error strings (empty = all good).
    """
    errors = []

    # 1. Check tweet slot count
    textareas = page.get_by_role("textbox", name="Post text")
    actual_slots = await textareas.count()
    if actual_slots != len(tweets_data):
        errors.append(f"Tweet slots: expected {len(tweets_data)}, found {actual_slots}")

    # 2. Check each textarea has the expected text
    for i, tweet in enumerate(tweets_data):
        expected = (tweet.get("text") or "").strip()
        if not expected:
            continue
        if i >= actual_slots:
            errors.append(f"Tweet {i+1}: slot missing entirely")
            continue
        try:
            actual = (await textareas.nth(i).inner_text()).strip()
            if not actual:
                errors.append(f"Tweet {i+1}: textarea is empty (expected text)")
            elif actual != expected:
                # Log mismatch but only hard-fail if dramatically different
                if len(actual) < len(expected) * 0.8:
                    errors.append(
                        f"Tweet {i+1}: text too short ({len(actual)} chars, expected ~{len(expected)})"
                    )
                else:
                    logger.warning(f"Tweet {i+1}: minor text mismatch (len {len(actual)} vs {len(expected)})")
        except Exception as e:
            errors.append(f"Tweet {i+1}: could not read textarea — {e}")

    # 3. Check attachment count matches expected images
    expected_images = sum(
        len([p for p in (t.get("image_paths") or []) if Path(p).exists()])
        for t in tweets_data
    )
    actual_images = await page.locator('[data-testid="attachments"]').count()
    if actual_images != expected_images:
        errors.append(
            f"Images: expected {expected_images} attachments, found {actual_images}"
        )

    # 4. Check no uploads still in progress
    uploading = await page.evaluate("""() => {
        // Look for progress bars or loading indicators inside the compose area
        return document.querySelectorAll(
            '[data-testid="progressBar"], [aria-label="Image loading"]'
        ).length;
    }""")
    if uploading > 0:
        errors.append(f"{uploading} image(s) still uploading — not ready to post")

    return errors


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

                    before = await page.locator('[data-testid="attachments"]').count()
                    logger.info(f"Uploading image for tweet {i} (attachments before: {before})")

                    async with page.expect_file_chooser(timeout=10000) as fc_info:
                        await page.evaluate("""() => {
                            const btns = document.querySelectorAll('[aria-label="Add photos or video"]');
                            btns[btns.length - 1].click();
                        }""")
                    fc = await fc_info.value
                    await fc.set_files(img_path)

                    confirmed = await _wait_for_attachment(page, before, timeout_s=30)
                    if confirmed:
                        logger.info(f"Image upload confirmed for tweet {i}")
                    else:
                        raise RuntimeError(
                            f"Image upload failed for tweet {i}: attachment did not appear after 30s"
                        )

            # ── Pre-post verification ─────────────────────────────────────────
            logger.info("Verifying compose state before posting...")
            errors = await _verify_compose(page, tweets_data)

            if errors:
                screenshot_path = DATA_DIR / "compose_error.png"
                await page.screenshot(path=str(screenshot_path))
                raise RuntimeError(
                    "Pre-post verification failed — NOT posting.\n"
                    + "\n".join(f"  • {e}" for e in errors)
                    + f"\nScreenshot saved: {screenshot_path}"
                )

            logger.info("Verification passed — posting thread")
            # ─────────────────────────────────────────────────────────────────

            post_btn = page.locator('[data-testid="tweetButtonInline"], [data-testid="tweetButton"]').first
            await post_btn.wait_for(state="visible", timeout=10000)
            await page.wait_for_timeout(1000)
            await post_btn.click()
            await page.wait_for_timeout(5000)

            await _save_cookies(context)
            logger.info("Thread posted via browser")
            return ["browser_ok"] * len(tweets_data)

        finally:
            await browser.close()
