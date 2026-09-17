from dataclasses import dataclass

import boto3

from .settings import Settings


@dataclass(frozen=True)
class AwsClients:
    codecommit: object
    logs: object
    s3: object
    lambda_client: object | None = None
    secretsmanager: object | None = None


def create_clients(settings: Settings) -> AwsClients:
    session = boto3.Session(region_name=settings.region)
    return AwsClients(
        codecommit=session.client("codecommit"),
        logs=session.client("logs"),
        s3=session.client("s3"),
        lambda_client=session.client("lambda"),
        secretsmanager=session.client("secretsmanager"),
    )
