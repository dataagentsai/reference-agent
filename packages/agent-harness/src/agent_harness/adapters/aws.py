"""What the AWS adapters share: an AWS client, and a request signed with the task's role.

Only the AWS adapters import this module, and it loads boto3 and botocore only
inside its functions (`importlib`, as they ship no type hints), so
`agent-harness` without the `[aws]` extra never needs them.

    client(service, wiring)   an AWS SDK client for one service. The hook `clients`
                              (service name -> client) replaces it, which is how the
                              contract tests run with no network and no account.
    SigV4                     an httpx auth that signs each request with AWS
                              Signature Version 4: the ECS task's IAM role, so no key
                              is stored anywhere. The hook `credentials` replaces
                              the role's (a test's fixed key).
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Generator
from typing import Any

import httpx2 as httpx

from agent_harness.adapters import OverlayRefused, Wiring

AGENTCORE_SERVICE = "bedrock-agentcore"
"""The signing name AgentCore Gateway takes (`awscurl --service bedrock-agentcore`,
gateway inference provider targets, AgentCore developer guide, 3 Nov 2026)."""

_GATEWAY_HOST = re.compile(r"\.gateway\.bedrock-agentcore\.([a-z0-9-]+)\.amazonaws\.com$")


def client(service: str, wiring: Wiring) -> Any:
    given = (wiring.hooks.get("clients") or {}).get(service)
    if given is not None:
        return given
    boto3 = importlib.import_module("boto3")
    region = wiring.settings.get("region")
    return boto3.client(service, region_name=str(region) if region else None)


def region_of_gateway(url: str) -> str | None:
    """`https://gw-1.gateway.bedrock-agentcore.ap-south-1.amazonaws.com/...` -> ap-south-1."""
    host = httpx.URL(url).host
    found = _GATEWAY_HOST.search(host)
    return found.group(1) if found else None


def role_credentials() -> Any:
    """The task role's credentials, refreshed by botocore as they near expiry."""
    found = importlib.import_module("boto3").Session().get_credentials()
    if found is None:
        raise OverlayRefused("no AWS credentials here (no task role, profile or environment)")
    return found


class SigV4(httpx.Auth):
    """Sign every request with SigV4 for one service and region.

    The body is part of the signature, so httpx reads it first
    (`requires_request_body`). Only `host`, `content-type` and the `x-amz-*`
    headers botocore adds are signed; the SDK's other headers travel unsigned,
    which SigV4 allows."""

    requires_request_body = True

    def __init__(self, credentials: Any, region: str, service: str) -> None:
        self.credentials, self.region, self.service = credentials, region, service

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        sigv4 = importlib.import_module("botocore.auth").SigV4Auth
        aws_request = importlib.import_module("botocore.awsrequest").AWSRequest
        signed = {"host": request.url.netloc.decode()}
        if "content-type" in request.headers:
            signed["content-type"] = request.headers["content-type"]
        aws = aws_request(
            method=request.method, url=str(request.url), data=request.content, headers=signed
        )
        sigv4(self.credentials, self.service, self.region).add_auth(aws)
        for name, value in aws.headers.items():
            request.headers[name] = value
        yield request


__all__ = ["AGENTCORE_SERVICE", "SigV4", "client", "region_of_gateway", "role_credentials"]
