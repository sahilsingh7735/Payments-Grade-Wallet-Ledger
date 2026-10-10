import json
import re
from dataclasses import dataclass
from typing import Protocol

import httpx


class StatementParser(Protocol):
    def parse(self, question: str) -> dict:
        """Return validated statement query JSON, never SQL."""


class RiskExplainer(Protocol):
    def explain(self, rule: str, transfers: list[dict]) -> dict:
        """Explain a rule decision using transfer data treated as untrusted data."""


@dataclass
class MockStatementParser:
    response: str = '{"type": 4, "from": "1970-01-01", "to": "2999-12-31"}'

    def parse(self, question: str) -> dict:
        value = json.loads(self.response)
        if not isinstance(value, dict):
            raise ValueError("parser response must be an object")
        return value


@dataclass
class MockRiskExplainer:
    label: str = "review"

    def explain(self, rule: str, transfers: list[dict]) -> dict:
        return {
            "label": self.label,
            "explanation": f"Rule {rule} was triggered by the supplied transaction data.",
        }


class RuleBasedStatementParser:
    """Offline parser used by default; it has the same contract as an LLM adapter."""

    def parse(self, question: str) -> dict:
        normalized = question.lower()
        dates = re.findall(r"\d{4}-\d{2}-\d{2}", normalized)
        if len(dates) != 2:
            raise ValueError("two ISO dates are required")
        if "largest" in normalized:
            kind = 3
        elif "how many" in normalized or "number" in normalized or "count" in normalized:
            kind = 4
        elif "received" in normalized or "receive" in normalized:
            kind = 2
        elif "sent" in normalized or "send" in normalized:
            kind = 1
        else:
            raise ValueError("unsupported question")
        result = {"type": kind, "from": dates[0], "to": dates[1]}
        names = re.findall(r"to ([a-z][a-z ]+?)(?: between| from| last| on|$)", normalized)
        if names:
            result["counterparty"] = names[0].strip()
        return result


@dataclass
class OpenAICompatibleStatementParser:
    endpoint: str
    api_key: str
    model: str

    def parse(self, question: str) -> dict:
        response = httpx.post(
            self.endpoint,
            headers={"Authorization": f"Bearer {self.api_key}"},
            json={
                "model": self.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": "Return only statement query JSON. Never return SQL."},
                    {"role": "user", "content": question},
                ],
            },
            timeout=15,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        value = json.loads(content)
        if not isinstance(value, dict):
            raise ValueError("parser response must be an object")
        return value
