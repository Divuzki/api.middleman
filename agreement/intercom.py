import hmac
import hashlib
import logging

import requests
from django.conf import settings
from middleman_api.exceptions import GatewayError

logger = logging.getLogger(__name__)


class IntercomClient:
    BASE_URL = "https://api.intercom.io"
    API_VERSION = "2.14"

    def __init__(self):
        self.access_token = settings.INTERCOM_ACCESS_TOKEN
        self.headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Intercom-Version": self.API_VERSION,
        }

    def _request(self, method, endpoint, payload=None):
        url = f"{self.BASE_URL}{endpoint}"
        try:
            method = (method or "GET").upper()
            request_kwargs = {
                "headers": self.headers,
                "timeout": 30,
            }
            if method in {"GET", "DELETE"}:
                request_kwargs["params"] = payload
            else:
                request_kwargs["json"] = payload

            response = requests.request(method, url, **request_kwargs)
            response.raise_for_status()
            if not response.content:
                return None
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Intercom API error ({endpoint}): {str(e)}")
            msg = str(e)
            if e.response is not None:
                logger.error(f"Intercom response: {e.response.text}")
                try:
                    resp_data = e.response.json()
                    if "errors" in resp_data and resp_data["errors"]:
                        msg = resp_data["errors"][0].get("message", msg)
                except ValueError:
                    pass
            raise GatewayError(f"Intercom Error: {msg}")

    def create_dispute_ticket(self, agreement, user, reason, category, role):
        ticket_type_id = settings.INTERCOM_DISPUTE_TICKET_TYPE_ID
        if not ticket_type_id:
            logger.warning("INTERCOM_DISPUTE_TICKET_TYPE_ID not configured, skipping ticket creation")
            return None

        payload = {
            "ticket_type_id": ticket_type_id,
            "contacts": [{"external_id": user.firebase_uid}],
            "ticket_attributes": {
                "_default_title_": f"Dispute: {agreement.title}",
                "_default_description_": reason,
                "agreement_id": agreement.id,
                "agreement_title": agreement.title,
                "amount": str(agreement.amount or ""),
                "currency": agreement.currency or "",
                "dispute_reason": reason,
                "dispute_category": category,
                "reporter_role": role,
            },
        }
        return self._request("POST", "/tickets", payload)

    def get_ticket(self, ticket_id):
        return self._request("GET", f"/tickets/{ticket_id}")

    @staticmethod
    def generate_identity_hash(user_id):
        secret = settings.INTERCOM_IDENTITY_SECRET
        if not secret:
            return None
        return hmac.new(
            key=secret.encode("utf-8"),
            msg=str(user_id).encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()
