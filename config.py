import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

TERMS_URL = os.getenv("TERMS_URL")

CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN")