from pydantic import BaseModel
from typing import List, Optional

class EndpointInfo(BaseModel):
    path: str
    method: str
    description: Optional[str]

class ResearchOutput(BaseModel):
    base_url: str
    auth_method: str
    endpoints: List[EndpointInfo]
    rate_limits: Optional[str]
    example: str
    webhooks: List[str]
    api_versioning: List[str]