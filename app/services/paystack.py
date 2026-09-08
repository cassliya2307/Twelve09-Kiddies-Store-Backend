import hmac
import hashlib
import logging
from decimal import Decimal
from typing import Optional

import httpx
from fastapi import HTTPException, status

from app.core.database import settings

logger = logging.getLogger(__name__)


class PaystackError(Exception):
    """Base exception for Paystack errors."""

    def __init__(self, message: str, details: Optional[dict] = None):
        self.message = message
        self.details = details
        super().__init__(message)


class PaystackSignatureError(PaystackError):
    """Raised when webhook signature verification fails."""

    pass


class PaystackVerificationError(PaystackError):
    """Raised when transaction verification fails."""

    pass


class PaystackClient:
    """Client for interacting with Paystack API."""

    BASE_URL = "https://api.paystack.co"

    def __init__(self):
        self.secret_key = settings.PAYSTACK_SECRET_KEY
        self.webhook_secret = settings.PAYSTACK_WEBHOOK_SECRET
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.BASE_URL,
                headers={
                    "Authorization": f"Bearer {self.secret_key}",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
        return self._client

    async def close(self):
        if self._client:
            await self._client.aclose()
            self._client = None

    async def initialize_transaction(
        self,
        *,
        email: str,
        amount: Decimal,
        reference: str,
        callback_url: Optional[str] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        """
        Initialize a Paystack transaction.

        Returns the Paystack response containing authorization_url, access_code, and reference.
        """
        client = self._get_client()

        # Convert amount to kobo (smallest currency unit)
        amount_kobo = int(amount * 100)

        payload = {
            "email": email,
            "amount": amount_kobo,
            "reference": reference,
            "currency": "NGN",
        }

        if callback_url:
            payload["callback_url"] = callback_url
        if metadata:
            payload["metadata"] = metadata

        try:
            response = await client.post("/transaction/initialize", json=payload)
            response.raise_for_status()
            data = response.json()

            if not data.get("status"):
                raise PaystackError(
                    f"Paystack initialization failed: {data.get('message', 'Unknown error')}",
                    details=data,
                )

            return data["data"]

        except httpx.HTTPStatusError as e:
            logger.error(f"Paystack HTTP error: {e.response.status_code} - {e.response.text}")
            raise PaystackError(
                f"Paystack API error: {e.response.status_code}",
                details={"response": e.response.text},
            )
        except httpx.RequestError as e:
            logger.error(f"Paystack request error: {e}")
            raise PaystackError(
                "Failed to connect to Paystack",
                details={"error": str(e)},
            )

    async def verify_transaction(self, reference: str) -> dict:
        """
        Verify a Paystack transaction.

        Returns the transaction data if successful.
        """
        client = self._get_client()

        try:
            response = await client.get(f"/transaction/verify/{reference}")
            response.raise_for_status()
            data = response.json()

            if not data.get("status"):
                raise PaystackVerificationError(
                    f"Transaction verification failed: {data.get('message', 'Unknown error')}",
                    details=data,
                )

            return data["data"]

        except httpx.HTTPStatusError as e:
            logger.error(f"Paystack verification HTTP error: {e.response.status_code} - {e.response.text}")
            raise PaystackVerificationError(
                f"Paystack verification failed: {e.response.status_code}",
                details={"response": e.response.text},
            )
        except httpx.RequestError as e:
            logger.error(f"Paystack verification request error: {e}")
            raise PaystackVerificationError(
                "Failed to verify transaction with Paystack",
                details={"error": str(e)},
            )

    def verify_signature(self, body: bytes, signature: str) -> bool:
        """
        Verify Paystack webhook signature.

        Paystack uses HMAC SHA512 with the webhook secret as the key.
        """
        if not self.webhook_secret:
            logger.warning("Paystack webhook secret not configured")
            return False

        expected_signature = hmac.new(
            self.webhook_secret.encode("utf-8"),
            body,
            hashlib.sha512,
        ).hexdigest()

        return hmac.compare_digest(expected_signature, signature)


# Singleton instance
paystack_client = PaystackClient()