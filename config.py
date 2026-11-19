import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

CHANNEL_1_URL = os.getenv("CHANNEL_1_URL")
CHANNEL_1_USERNAME = os.getenv("CHANNEL_1_USERNAME")

CHANNEL_2_URL = os.getenv("CHANNEL_2_URL")
CHANNEL_2_ID = os.getenv("CHANNEL_2_ID")

TERMS_URL = os.getenv("TERMS_URL")

CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN")