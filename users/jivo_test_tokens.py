"""
Real Jivo access tokens for tests, signed with a throwaway key, so the whole
JivoJWTAuthentication path runs without calling auth.jivo.in.

    from users.jivo_test_tokens import make_access_token, use_test_keys

    @use_test_keys
    class OrderAPITests(APITestCase):
        def test_mapped_user_sees_their_orders(self):
            user = User.objects.create(username="alice", email="alice@jivo.in", auth_id=uuid.uuid4())
            token = make_access_token(user_id=user.auth_id, email=user.email)
            response = self.client.get("/api/orders/", HTTP_AUTHORIZATION=f"Bearer {token}")
            ...

APP and URL match settings.JIVO_AUTH (OMS/settings.py).
"""

import time
import uuid

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.test import override_settings


APP = "oms"
URL = "https://auth.jivo.in"

_TEST_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

TEST_PUBLIC_KEY = _TEST_KEY.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
).decode()


def make_access_token(user_id=None, email="tester@jivo.in", apps=(APP,), **claims):
    now = int(time.time())
    payload = {
        "token_type": "access",
        "sub": str(user_id or uuid.uuid4()),
        "email": email,
        "apps": list(apps),
        "iss": URL,
        "iat": now,
        "exp": now + 900,
        "jti": uuid.uuid4().hex,
        **claims,
    }
    return jwt.encode(payload, _TEST_KEY, algorithm="RS256")


# Verify with the test key instead of fetching Jivo Auth's.
use_test_keys = override_settings(
    JIVO_AUTH={
        **getattr(settings, "JIVO_AUTH", {}),
        "URL": URL,
        "APP": APP,
        "PUBLIC_KEY": TEST_PUBLIC_KEY,
    },
)
