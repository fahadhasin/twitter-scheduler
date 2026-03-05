import asyncio
import json
import logging
from telegram import Bot
from db import get_pending_threads, get_tweets, mark_thread_posted, mark_thread_failed, update_tweet_id
from twitter import post_thread

logger = logging.getLogger(__name__)

POLL_INTERVAL = 30  # seconds


async def run_scheduler(bot: Bot, allowed_user_id: int):
    """Background task: poll every 30s and post due threads."""
    while True:
        try:
            await _check_and_post(bot, allowed_user_id)
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
        await asyncio.sleep(POLL_INTERVAL)


async def _check_and_post(bot: Bot, allowed_user_id: int):
    pending = get_pending_threads()
    for thread in pending:
        thread_id = thread["id"]
        logger.info(f"Posting thread {thread_id}")
        try:
            tweets = get_tweets(thread_id)
            tweets_data = [
                {
                    "text": t["text"] or "",
                    "image_paths": json.loads(t["image_paths"] or "[]"),
                }
                for t in tweets
            ]

            posted_ids = await post_thread(tweets_data)

            for i, tweet_id in enumerate(posted_ids):
                update_tweet_id(thread_id, i, tweet_id)

            mark_thread_posted(thread_id)
            logger.info(f"Thread {thread_id} posted successfully")

            await bot.send_message(
                chat_id=allowed_user_id,
                text=f"Thread #{thread_id} posted! Check your X profile.",
            )

        except Exception as e:
            err = str(e)
            logger.error(f"Failed to post thread {thread_id}: {err}")
            mark_thread_failed(thread_id, err)
            await bot.send_message(
                chat_id=allowed_user_id,
                text=f"Failed to post thread #{thread_id}:\n{err}",
            )
