"""AWS STS: reaching a customer's account through the roles its stack made."""

from __future__ import annotations


def sts_assume(role_arn: str, *, external_id: str, session: str) -> dict:
    """Assume the role for 15 minutes and say which account answered."""
    import boto3  # the 'aws' extra

    creds = boto3.client("sts").assume_role(RoleArn=role_arn, RoleSessionName=session[:64], ExternalId=external_id,
                                            DurationSeconds=900)["Credentials"]
    return boto3.client("sts", aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                        aws_session_token=creds["SessionToken"]).get_caller_identity()
