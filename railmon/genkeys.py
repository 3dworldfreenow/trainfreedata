"""Print fresh secrets for .env:  python -m railmon.genkeys"""
import base64
import os
import secrets

if __name__ == "__main__":
    print(f"SECRET_KEY={secrets.token_urlsafe(48)}")
    print(f"DATA_ENCRYPTION_KEY={base64.urlsafe_b64encode(os.urandom(32)).decode()}")
    print(f"PNR_HASH_KEY={secrets.token_urlsafe(32)}")
    print(f"INTERNAL_API_TOKEN={secrets.token_urlsafe(32)}")
