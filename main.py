import asyncio
import json
import pathlib
import random
import traceback
import logging
import urllib.parse

import telethon
import smolagents

from config import config


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(config.LOGS_PATH)],
)

logger = logging.getLogger(__name__)
loop: asyncio.AbstractEventLoop | None = None

waiting: dict[int, asyncio.Task] = {}
working: set[int] = set()

client = telethon.TelegramClient(
    config.SESSION_PATH, config.APP_ID, config.APP_HASH, proxy=config.PROXY
)


class SendMessageToChatTool(smolagents.Tool):
    name = "send_message_to_chat"
    description = """
    This is a tool that will send message to the given chat.
    It returns status of whether sending was successful."""

    inputs = {
        "text": {"type": "string", "description": "Text of message to send"},
        "chat_id": {
            "type": "integer",
            "description": "Id of chat to send message",
        },
    }
    output_type = "string"

    async def _forward(self, text: str, chat_id: int) -> str:
        try:
            async with client.action(chat_id, "typing"):  # ty: ignore[invalid-context-manager]
                logger.info("Started typing in chat %s", chat_id)
                await asyncio.sleep(len(text) / random.uniform(5, 10))
                await client.send_message(chat_id, text)

            logger.info("Reply sent to chat %s.", chat_id)
            return "Message was successfully sent!"
        except Exception:
            message = (
                "Given error while tried to send message:\n"
                + traceback.format_exc()
            )

            logger.error(message)
            return message

    def forward(self, text: str, chat_id: int) -> str:
        logger.info("Tool SendMessageTool ran")
        return asyncio.run_coroutine_threadsafe(
            self._forward(text=text, chat_id=chat_id),
            loop,  # ty: ignore[invalid-argument-type]
        ).result()


class SendMessageToOwnerTool(smolagents.Tool):
    name = "send_message_to_owner"
    description = """
    This is a tool that will send message to the owner of account.
    It returns status of whether sending was successful."""

    inputs = {
        "text": {"type": "string", "description": "Text of message to send"}
    }
    output_type = "string"

    def forward(self, text: str) -> str:
        try:
            # You should change it as you like more
            NOTIFICATIONS_PATH = pathlib.Path.cwd() / "NOTIFICATIONS.md"
            mode = "a" if NOTIFICATIONS_PATH.exists() else "w"

            with NOTIFICATIONS_PATH.open(mode) as file:
                if mode == "a":
                    file.write("\n\n")
                file.write(text)

            logger.info("Notification for account owner written.")
            return "Message for account owner was written!"
        except:
            message = (
                "Given error while tried to send message:\n"
                + traceback.format_exc()
            )

            logger.error(message)
            return message


async def handle_new_message(chat_id: int):
    agent = smolagents.CodeAgent(
        model=smolagents.OpenAIModel(
            model_id=config.MODEL,
            api_base=config.BASE_URL,
            api_key=config.API_KEY,
        ),
        tools=[SendMessageToChatTool(), SendMessageToOwnerTool()],
    )

    logger.info("Handling chat %s", chat_id)

    history = [
        {
            "date": message.date.strftime("%Y-%m-%d %H:%M:%S"),
            "sender": (
                (message.sender.first_name or "")
                + " "
                + (message.sender.last_name or "")
            ).strip(),
            "text": message.text,
            "is_message_from_account_owner": message.out,
        }
        for message in reversed(await client.get_messages(chat_id, limit=100))  # ty: ignore[no-matching-overload]
        if message.text
    ]
    logger.info("Chat history for %s loaded", chat_id)

    await client.send_read_acknowledge(chat_id)
    logger.info("Chat %s set as read", chat_id)

    await asyncio.get_event_loop().run_in_executor(
        None,
        agent.run,
        (
            config.PROMPT
            + "\n\n # *CHAT ID*: "
            + str(chat_id)
            + "\n\n# Message history\n\n"
            + json.dumps(history)
        ),
    )

    logger.info("Chat %s handled", chat_id)


async def debounce(chat_id: int):
    try:
        logger.info(
            "Waiting %s seconds before handling chat %s",
            config.WAIT_SECONDS,
            chat_id,
        )
        await asyncio.sleep(config.WAIT_SECONDS)
    except asyncio.CancelledError:
        logger.info("Debounce cancelled for chat %s", chat_id)
        raise

    try:
        logger.info("Waiting ended for chat %s", chat_id)
        working.add(chat_id)
        await handle_new_message(chat_id=chat_id)
    finally:
        working.discard(chat_id)
        waiting.pop(chat_id, None)


@client.on(telethon.events.NewMessage())
async def encounter_new_message(event):
    logger.info("Received new message in chat %s", event.chat_id)

    if not event.is_private:
        logger.info("Chat %s is not private; skipping", event.chat_id)
        return

    if event.out:
        logger.info(
            "Message in chat %s was sent by owner account; skipping",
            event.chat_id,
        )
        return

    if event.chat_id in waiting:
        logger.info(
            "Chat %s already waiting or being handled",
            event.chat_id,
        )

        is_chat_in_working = event.chat_id in working

        if is_chat_in_working:
            logger.info(
                "Chat %s already being handled; waiting for completion",
                event.chat_id,
            )

            while event.chat_id in working:
                await asyncio.sleep(1)

            logger.info("Handling finished for chat %s", event.chat_id)

            waiting[event.chat_id] = asyncio.create_task(
                debounce(event.chat_id)
            )
        else:
            logger.info(
                "Chat %s is not being handled yet; reloading timer",
                event.chat_id,
            )

            try:
                waiting[event.chat_id].cancel()
                await waiting[event.chat_id]
            finally:
                waiting[event.chat_id] = asyncio.create_task(
                    debounce(event.chat_id)
                )
    else:
        waiting[event.chat_id] = asyncio.create_task(debounce(event.chat_id))


async def main():
    global loop
    loop = asyncio.get_event_loop()

    logger.info("Start main().")
    await client.start()  # ty: ignore[invalid-await]

    logger.info("Scanning existing dialogs.")
    async for dialog in client.iter_dialogs():
        if dialog.is_user and dialog.unread_count > 0:
            waiting[dialog.id] = asyncio.create_task(debounce(dialog.id))
            logger.info("Found dialog %s with unread messages.", dialog.id)
    logger.info("Finished scanning existing dialogs.")

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
