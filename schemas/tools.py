from pydantic import BaseModel, Field
from typing import List, Optional


class EndpointInfo(BaseModel):
    path: str = Field(description="The endpoint path, e.g. /v1/users/{id}")
    method: str = Field(description="HTTP method: GET, POST, PUT, PATCH, or DELETE")
    description: Optional[str] = Field(default=None, description="Short description of what this endpoint does")


class ResearchOutput(BaseModel):
    base_url: str = Field(description="The base URL of the API, e.g. https://api.example.com/v1")
    auth_method: str = Field(
        description="Authentication method used: API key, OAuth2, Bearer token, Basic auth, etc. "
                    "Include how credentials are passed (header name, query param name, etc.)"
    )
    endpoints: List[EndpointInfo] = Field(
        default_factory=list,
        description="List of all API endpoints found in the documentation"
    )
    rate_limits: Optional[str] = Field(
        default=None,
        description="Rate limiting details: requests per minute/hour/day, burst limits, 429 behaviour"
    )
    example: Optional[str] = Field(
        default=None,
        description="A concrete usage example: curl command or code snippet showing a real API call"
    )
    webhooks: Optional[List[str]] = Field(
        default=None,
        description="Webhook event names and payload details, or None if the API has no webhooks"
    )
    api_versioning: Optional[List[str]] = Field(
        default=None,
        description="API versioning scheme details (URL path, header, query param), or None if not documented"
    )