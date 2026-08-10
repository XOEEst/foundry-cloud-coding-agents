from __future__ import annotations

import base64
from dataclasses import dataclass
import json
from typing import ClassVar

from foundry_opt.security import reject_secret_content


@dataclass(frozen=True)
class OpenAIStringCheckGrader:
    input: str
    operation: str
    reference: str

    PREFIX: ClassVar[str] = "openai-grader:string-check:"
    MAX_REMOTE_ID_LENGTH: ClassVar[int] = 2048
    OPERATIONS: ClassVar[frozenset[str]] = frozenset(
        {"eq", "ne", "like", "ilike"}
    )

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.input, "input"),
            (self.reference, "reference"),
        ):
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 4096
                or any(character in "\r\n\x00" for character in value)
            ):
                raise ValueError(
                    f"string-check grader {field_name} is invalid"
                )
            reject_secret_content(value)
        if self.operation not in self.OPERATIONS:
            raise ValueError("string-check grader operation is invalid")
        if len(self.remote_id) > self.MAX_REMOTE_ID_LENGTH:
            raise ValueError(
                "string-check grader remote identity exceeds 2048 characters"
            )

    @property
    def remote_id(self) -> str:
        payload = json.dumps(
            {
                "input": self.input,
                "operation": self.operation,
                "reference": self.reference,
            },
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        return f"{self.PREFIX}{encoded}"

    @classmethod
    def from_remote_id(
        cls,
        remote_id: str,
    ) -> OpenAIStringCheckGrader | None:
        if not isinstance(remote_id, str) or not remote_id.startswith(
            cls.PREFIX
        ):
            return None
        if len(remote_id) > cls.MAX_REMOTE_ID_LENGTH:
            raise ValueError(
                "string-check grader remote identity is invalid"
            )
        encoded = remote_id[len(cls.PREFIX) :]
        try:
            padding = "=" * (-len(encoded) % 4)
            payload = json.loads(
                base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
            )
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError(
                "string-check grader remote identity is invalid"
            ) from error
        if not isinstance(payload, dict) or set(payload) != {
            "input",
            "operation",
            "reference",
        }:
            raise ValueError("string-check grader remote identity is invalid")
        grader = cls(
            input=payload["input"],
            operation=payload["operation"],
            reference=payload["reference"],
        )
        if remote_id != grader.remote_id:
            raise ValueError(
                "string-check grader remote identity is invalid"
            )
        return grader
