"""Structured-output contracts shared by OpenAI and Ollama adapters."""

TARGET_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["resolved", "missing", "unknown", "ambiguous"]},
        "target_systems": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
        "clarification_question": {"type": "string"},
    },
    "required": ["status", "target_systems", "reason", "clarification_question"],
    "additionalProperties": False,
}

RUNBOOK_RANK_SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "score": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "score", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["matches"],
    "additionalProperties": False,
}

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "feedback": {"type": "string"},
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["passed", "feedback", "issues"],
    "additionalProperties": False,
}
