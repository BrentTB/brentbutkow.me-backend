import hashlib

from hypothesis import given, settings
from hypothesis import strategies as st

from app.subscriptions.service import generate_confirmation_token


@given(st.integers())  # dummy strategy — generates a fresh token on each run
@settings(max_examples=200)
def test_confirmation_token_sha256_hash_property(dummy: int) -> None:
    raw, hashed = generate_confirmation_token()

    # Property: hash is the SHA-256 hex digest of the raw token
    assert hashed == hashlib.sha256(raw.encode()).hexdigest()

    # Property: raw token is never embedded in the hash
    assert raw not in hashed
