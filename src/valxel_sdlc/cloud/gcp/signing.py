"""Cloud KMS: RS256 with a key the tool never holds (signing: the GitHub Apps' JWTs).

The App's private key is imported into an asymmetric signing key of purpose ASYMMETRIC_SIGN and
algorithm RSA_SIGN_PKCS1_2048_SHA256 (or 3072, 4096); the setting names its key version,
`projects/<p>/locations/<l>/keyRings/<r>/cryptoKeys/<k>/cryptoKeyVersions/<n>`. Cloud KMS signs a
SHA-256 digest, which it checks against a CRC32C when the library is there."""

from __future__ import annotations

import hashlib
from typing import Any

PREFIX = "projects/"


class GcpKmsSigner:
    def __init__(self, key_version: str, *, client: Any = None) -> None:
        if not key_version.startswith(PREFIX) or "/cryptoKeyVersions/" not in key_version:
            raise ValueError("A Cloud KMS signing key is named by its key version: "
                             "projects/<p>/locations/<l>/keyRings/<r>/cryptoKeys/<k>/cryptoKeyVersions/<n>")
        if client is None:
            from google.cloud import kms  # the 'gcp' extra

            client = kms.KeyManagementServiceClient()
        self.key_version, self.client = key_version, client

    def sign(self, message: bytes) -> bytes:
        got = self.client.asymmetric_sign(request={"name": self.key_version,
                                                   "digest": {"sha256": hashlib.sha256(message).digest()}})
        if getattr(got, "name", self.key_version) != self.key_version:
            raise RuntimeError("Cloud KMS signed with another key version")
        return bytes(got.signature)
