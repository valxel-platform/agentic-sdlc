"""AWS KMS: RS256 with a sign-only key the tool never holds (signing)."""

from __future__ import annotations


class KmsSigner:
    def __init__(self, key_id: str, *, client=None, region: str = "") -> None:
        if client is None:
            import boto3  # pip install .[aws]

            client = boto3.client("kms", **({"region_name": region} if region else {}))
        self.key_id, self.client = key_id, client

    def sign(self, message: bytes) -> bytes:
        got = self.client.sign(KeyId=self.key_id, Message=message, MessageType="RAW",
                               SigningAlgorithm="RSASSA_PKCS1_V1_5_SHA_256")
        return got["Signature"]
