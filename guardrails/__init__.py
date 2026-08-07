"""
guardrails/__init__.py
Public surface: import `run_guardrail` and `GuardrailResult`.
"""
from guardrails.guardrail import GuardrailResult, run_guardrail

__all__ = ["GuardrailResult", "run_guardrail"]
