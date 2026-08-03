from pydantic import BaseModel, Field
from typing import List, Optional




class ParameterInfo(BaseModel):
    name: str = Field(description="Parameter name, e.g. 'page', 'limit', 'Authorization'")
    location: str = Field(description="Where the param is passed: 'query', 'path', 'header', or 'body'")
    type: str = Field(description="Data type: 'string', 'integer', 'boolean', 'object', 'array'")
    required: bool = Field(description="True if the parameter is required, False if optional")
    description: Optional[str] = Field(default=None, description="What the parameter does")


class ResponseSchema(BaseModel):
    status_code: int = Field(description="HTTP status code, e.g. 200, 201, 204")
    description: str = Field(description="What this response means, e.g. 'Success', 'Created'")
    example: Optional[str] = Field(
        default=None,
        description="A JSON example of the response body, as a string"
    )


class ErrorCode(BaseModel):
    code: str = Field(
        description="HTTP status code or API-specific error code string, e.g. '429', 'RATE_LIMIT_EXCEEDED'"
    )
    name: Optional[str] = Field(default=None, description="Short error name if documented, e.g. 'Unauthorized'")
    description: str = Field(description="What causes this error and how to handle it")


class PaginationInfo(BaseModel):
    type: str = Field(
        description="Pagination style: 'cursor', 'page-number', 'offset-limit', 'link-header', or 'none'"
    )
    parameter: Optional[str] = Field(
        default=None,
        description="Query parameter name used for pagination, e.g. 'page', 'cursor', 'offset'"
    )
    description: Optional[str] = Field(
        default=None,
        description="Additional details: max page size, default limit, how to get the next page, etc."
    )


class EndpointInfo(BaseModel):
    path: str = Field(description="The endpoint path, e.g. /v1/users/{id}")
    method: str = Field(description="HTTP method: GET, POST, PUT, PATCH, or DELETE")
    description: Optional[str] = Field(default=None, description="Short description of what this endpoint does")
    parameters: Optional[List[ParameterInfo]] = Field(
        default=None,
        description="List of parameters accepted by this endpoint"
    )
    response_schema: Optional[ResponseSchema] = Field(
        default=None,
        description="The primary success response for this endpoint"
    )




class ResearchOutput(BaseModel):
    base_url: str = Field(description="The base URL of the API, e.g. https://api.example.com/v1")
    auth_method: str = Field(
        description="Authentication method used: API key, OAuth2, Bearer token, Basic auth, etc. "
                    "Include how credentials are passed (header name, query param name, etc.)"
    )
    endpoints: List[EndpointInfo] = Field(
        default_factory=list,
        description="List of all API endpoints found, each with path, method, parameters, and response schema"
    )
    rate_limits: Optional[str] = Field(
        default=None,
        description="Rate limiting details: requests per minute/hour/day, burst limits, 429 behaviour"
    )
    example: Optional[str] = Field(
        default=None,
        description="A concrete usage example: curl command or code snippet showing a real API call"
    )
    pagination: Optional[PaginationInfo] = Field(
        default=None,
        description="How the API paginates results, or None if pagination is not documented"
    )
    error_codes: Optional[List[ErrorCode]] = Field(
        default=None,
        description="All documented error codes: HTTP status codes and/or API-specific error strings"
    )
    webhooks: Optional[List[str]] = Field(
        default=None,
        description="Webhook event names and payload details, or None if the API has no webhooks"
    )
    api_versioning: Optional[List[str]] = Field(
        default=None,
        description="API versioning scheme details (URL path, header, query param), or None if not documented"
    )

    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Completeness score 0.0–1.0 computed from how many key fields were populated. "
            "< 0.5 = insufficient for planning; >= 0.5 = ready to plan."
        ),
    )
    quality_flags: List[str] = Field(
        default_factory=list,
        description=(
            "List of missing or weak fields detected during research, "
            "e.g. ['no_endpoints', 'no_auth', 'no_rate_limits']. "
            "Empty list means research is complete."
        ),
    )




class Task(BaseModel):
    id: int = Field(description="Unique integer task ID, starting at 1")
    name: str = Field(description="Short, specific task name, e.g. 'Test GET /v1/users'")
    description: Optional[str] = Field(
        default=None,
        description="One-sentence description of what this task verifies"
    )
    tool: str = Field(
        description=(
            "Tool to execute this task. One of: "
            "auth_tester | endpoint_tester | rate_tester | error_tester | "
            "webhook_tester | sdk_generator | doc_writer"
        )
    )
    depends_on: List[int] = Field(
        default_factory=list,
        description=(
            "List of task IDs that must complete before this task can start. "
            "Empty list means this task has no dependencies and can run immediately."
        ),
    )
    priority: str = Field(
        description="Task priority: 'critical' | 'high' | 'medium' | 'low'"
    )
    inputs: dict = Field(
        default_factory=dict,
        description="Key/value inputs the tool needs to execute this task, derived from the research JSON"
    )


class ExecutionPlan(BaseModel):
    summary: str = Field(
        description=(
            "One-sentence summary of what this plan covers, "
            "e.g. 'Test plan for Stripe Payments API: 8 tasks across auth, 3 endpoints, rate limits, and SDK generation'"
        )
    )
    tasks: List[Task] = Field(
        description="Ordered list of tasks. Tasks with no depends_on can run in parallel."
    )
