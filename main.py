import asyncio
import json
import pathlib
import random
import logging
import os
import urllib.parse

import dotenv
import telethon
import pydantic

import langchain_openai
import langchain.agents


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
dotenv.load_dotenv()

logger = logging.getLogger(__name__)


class ResponseFormat(pydantic.BaseModel):
    answer_to_chat: str
    answer_to_mikhail: str


with open(pathlib.Path.cwd() / 'PROMPT.md') as file:
    PROMPT = file.read()

try:
    APP_ID = int(os.environ["APP_ID"])
    APP_HASH = os.environ["APP_HASH"]

    # Must be OpenAI-compatible
    BASE_URL = os.environ["BASE_URL"]
    API_KEY = os.environ["API_KEY"]
    MODEL = os.environ["MODEL"]

    PROXY = os.environ.get("PROXY", "")
    WAIT_SECONDS = int(os.environ.get("WAIT_SECONDS", 30))
except KeyError:
    logger.critical("You have forgot add some variables to .env! Here is error")
    raise

if PROXY:
    PROXY = urllib.parse.urlparse(PROXY)
    PROXY = {
        "proxy_type": PROXY.scheme,
        "addr": PROXY.hostname, "port": PROXY.port, "username": PROXY.username,
        "password": PROXY.password
    }
else:
    PROXY = {}

NOTIFICATIONS_PATH = pathlib.Path.home() / 'NOTIFICATIONS.md'
SESSION_PATH = pathlib.Path.home() / 'telegram.session'

waiting: dict[int, asyncio.Task] = {}
working: set[int] = set()

client = telethon.TelegramClient(
    SESSION_PATH,
    APP_ID,
    APP_HASH,
    proxy=PROXY
)

agent = langchain.agents.create_agent(
    model=langchain_openai.ChatOpenAI(
        base_url=BASE_URL,
        api_key=API_KEY,
        model=MODEL
    ),
    response_format=ResponseFormat,
    system_prompt=PROMPT
)

async def handle_new_message(chat_id: int):
    logger.info("Handling chat %s", chat_id)
    working.add(chat_id)

    try:
        history = [
            {
                'date': message.date.strftime("%Y-%m-%d %H:%M:%S"),
                'sender': (
                    (message.sender.first_name or '')
                    + ' '
                    + (message.sender.last_name or '')
                ).strip(),
                'text': message.text or ''
            } for message in reversed(
                await client.get_messages(chat_id, limit=100)
            )
        ]
        logger.info("Chat history for %s loaded", chat_id)
    
        await client.send_read_acknowledge(chat_id)
        logger.info("Chat %s set as read", chat_id)

        async with client.action(chat_id, 'typing'):
            logger.info("Started typing in chat %s", chat_id)
            answer = (await agent.ainvoke(
                {'messages': [{'role': 'user', "content": json.dumps(history)}]}
            ))['structured_response']
            logger.info("Got answer for chat %s: %s", chat_id, answer)

            if answer.answer_to_chat:
                await client.send_message(chat_id, answer.answer_to_chat)
                logger.info("Reply sent to chat %s.", chat_id)

        if answer.answer_to_mikhail:
            mode = 'a' if NOTIFICATIONS_PATH.exists() else 'w'
            with NOTIFICATIONS_PATH.open(mode) as file:
                if mode == 'a':
                    file.write('\n\n')
                file.write(answer.answer_to_mikhail)

            logger.info("Notification for Mikhail written for chat %s.", chat_id)

    finally:
        working.discard(chat_id)


async def debounce(chat_id: int):
    try:
        logger.info('Waiting %s seconds before processing chat %s', WAIT_SECONDS, chat_id)
        await asyncio.sleep(WAIT_SECONDS)
    except asyncio.CancelledError:
        logger.info('Debounce cancelled for chat %s', chat_id)
        raise

    waiting.pop(chat_id, None)
    await handle_new_message(chat_id=chat_id)


@client.on(telethon.events.NewMessage)
async def encounter_new_message(event):
    logger.info("Received new message in chat %s", event.chat_id)

    if not event.is_private:
        logger.info("Chat %s is not private; skipping", event.chat_id)
        return

    if event.out:
        logger.info("Message in chat %s was sent by me; skipping", event.chat_id)
        return

    if event.chat_id in waiting:
        logger.info("Chat %s already waiting; restarting the debounce timer", event.chat_id)

        is_chat_in_working = event.chat_id in working

        if is_chat_in_working:
            logger.info('Chat %s already being processed; waiting for completion', event.chat_id)

        while event.chat_id in working:
            await asyncio.sleep(1)

        if is_chat_in_working:
            logger.info("Processing finished for chat %s", event.chat_id)

        try:
            waiting[event.chat_id].cancel()
            await waiting[event.chat_id]
        finally:
            waiting[event.chat_id] = asyncio.create_task(debounce(event.chat_id))
    else:
        waiting[event.chat_id] = asyncio.create_task(debounce(event.chat_id))
        

async def main():
    logger.info("Starting main().")
    await client.start()

    logger.info("Scanning existing dialogs")
    async for dialog in client.iter_dialogs():
        if dialog.is_user and dialog.unread_count > 0:
            waiting[dialog.id] = asyncio.create_task(debounce(dialog.id))
            logger.info("Found dialog %s with unread messages", dialog.id)
    logger.info("Finished scanning existing dialogs")
    await client.run_until_disconnected()


if __name__ == '__main__':
    asyncio.run(main())
