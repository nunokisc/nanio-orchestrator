"""Tests for Fernet encryption/decryption module."""

import os
import pytest
from unittest.mock import patch


@pytest.fixture(autouse=True)
def set_secret():
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    os.environ["NANIO_ORCHESTRATOR_SECRET"] = key
    import nanio_orchestrator.config as cfg_mod
    cfg_mod.settings = None
    import nanio_orchestrator.credentials as cred_mod
    cred_mod.reset_fernet()
    yield key
    os.environ.pop("NANIO_ORCHESTRATOR_SECRET", None)
    cfg_mod.settings = None
    cred_mod.reset_fernet()


class TestEncryption:
    def test_encrypt_decrypt_roundtrip(self):
        from nanio_orchestrator.credentials import encrypt, decrypt
        plaintext = "my-secret-access-key-12345"
        token = encrypt(plaintext)
        assert token != plaintext
        assert decrypt(token) == plaintext

    def test_different_plaintexts_different_tokens(self):
        from nanio_orchestrator.credentials import encrypt
        t1 = encrypt("key-a")
        t2 = encrypt("key-b")
        assert t1 != t2

    def test_same_plaintext_different_tokens(self):
        """Fernet uses random IV, so same plaintext gives different tokens."""
        from nanio_orchestrator.credentials import encrypt
        t1 = encrypt("same")
        t2 = encrypt("same")
        assert t1 != t2

    def test_decrypt_with_wrong_key_fails(self, set_secret):
        from nanio_orchestrator.credentials import encrypt, decrypt, reset_fernet
        token = encrypt("test-value")

        # Change the key
        from cryptography.fernet import Fernet
        new_key = Fernet.generate_key().decode()
        os.environ["NANIO_ORCHESTRATOR_SECRET"] = new_key
        import nanio_orchestrator.config as cfg_mod
        cfg_mod.settings = None
        reset_fernet()

        with pytest.raises(ValueError, match="decrypt"):
            decrypt(token)

    def test_no_secret_raises(self):
        from nanio_orchestrator.credentials import reset_fernet
        os.environ.pop("NANIO_ORCHESTRATOR_SECRET", None)
        import nanio_orchestrator.config as cfg_mod
        cfg_mod.settings = None
        reset_fernet()

        from nanio_orchestrator.credentials import encrypt
        with pytest.raises(RuntimeError, match="SECRET"):
            encrypt("test")
