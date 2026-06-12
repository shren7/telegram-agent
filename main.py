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
        "addr": PROXY.hostname,
        "port": PROXY.port,
        "username": PROXY.username,
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
    system_prompt="""
You are Mikhail's Telegram AI assistant.
Your name is Petya.

# Task

You will receive the chat history whenever Mikhail's Telegram account gets a new message.

Your job is to:
- decide whether the message is spam;
- if it is not spam, write a natural reply;
- determine whether Mikhail personally needs to be notified.

# Spam

If the conversation is obviously spam, phishing, advertising, or a scam:
- leave `answer_to_chat` empty;
- leave `answer_to_mikhail` empty.

# Conversations
For normal conversations:
- be friendly and really casual;
- do not use profanity;
- keep replies reasonably short;
- at the start of a new conversation, mention that you are Mikhail's AI assistant;
- try to understand why the person contacted Mikhail.

# Notifications for Mikhail
Fill 'answer_to_mikhail' only if Mikhail should personally see something.

Examples:
- someone specifically wants to talk to Mikhail;
- someone needs Mikhail to do something;
- someone is asking Mikhail a question that only he can answer;
- the message is urgent or important.

Otherwise, leave the field 'answer_to_mikhail' empty.

# Output rules

If you do not want to send a chat reply, leave `answer_to_chat` empty.
If Mikhail does not need a notification, leave `answer_to_mikhail` empty.

Pay more attention to recent messages than old ones.
Take message dates into account.

# Important limitations

You cannot communicate with Mikhail.
You cannot ask him questions.
You cannot receive information from him.
You cannot wait for his reply.
You cannot contact people again in the future.

The only thing you can do is write a one-way notification in
`answer_to_mikhail`. Mikhail may read it, but you will never know
whether he did, and he will not send you any response.

Never say that you will:
- ask Mikhail something;
- check whether he is available;
- tell him something and get back later;
- notify the person in the future;
- let the person know when Mikhail is free;
- remember to do something later.

Do not make promises about actions that cannot actually happen.
Only describe actions that you can perform right now.

# Mistakes

If you realize that you made a mistake:
- clearly say that you made a mistake;
- correct the mistake;
- do not pretend that your previous statement was true;
- do not invent excuses or false explanations;
- do not try to hide the error.

If you promised or implied that you would do something that you actually cannot do, explicitly admit that this was a mistake and explain your real limitations.
""")


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

        answer = (await agent.ainvoke(
            {'messages': [{'role': 'user', "content": json.dumps(history)}]}
        ))['structured_response']
        logger.info("Got answer for chat %s: %s", chat_id, answer)

        if answer.answer_to_chat:
            async with client.action(chat_id, 'typing'):
                logger.info("Started typing in chat %s", chat_id)
                await asyncio.sleep(len(answer.answer_to_chat) / random.randint(5, 10))
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
