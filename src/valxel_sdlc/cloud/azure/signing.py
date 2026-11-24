"""Azure Key Vault keys: RS256 with a key the tool never holds (signing: the GitHub Apps' JWTs).

The App's private key is imported into Key Vault as an RSA-HSM key (a Premium vault or Managed
HSM), not exportable, with only the `sign` operation; the setting names the key's version,
`https://<vault>.vault.azure.net/keys/<name>/<version>`. Key Vault signs a SHA-256 digest with
RS256 (RSASSA-PKCS1-v1_5)."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from . import credential

KEY = re.compile(r"^https://[a-z0-9-]+\.(vault\.azure\.net|managedhsm\.azure\.net)/keys/[A-Za-z0-9-]+/[0-9a-f]{32}$")


class AzureKeyVaultSigner:
    def __init__(self, key_id: str, *, client: Any = None) -> None:
        if not KEY.match(key_id):
            raise ValueError("An Azure Key Vault signing key is named by its version: "
                             "https://<vault>.vault.azure.net/keys/<name>/<version>")
        if client is None:
            from azure.keyvault.keys.crypto import CryptographyClient  # the 'azure' extra

            client = CryptographyClient(key_id, credential=credential())
        self.key_id, self.client = key_id, client

    def sign(self, message: bytes) -> bytes:
        try:
            from azure.keyvault.keys.crypto import SignatureAlgorithm  # the 'azure' extra

            rs256: Any = SignatureAlgorithm.rs256
        except ImportError:  # a fake client in tests
            rs256 = "RS256"
        got = self.client.sign(rs256, hashlib.sha256(message).digest())
        if getattr(got, "key_id", self.key_id) != self.key_id:
            raise RuntimeError("Key Vault signed with another key version")
        return bytes(got.signature)
