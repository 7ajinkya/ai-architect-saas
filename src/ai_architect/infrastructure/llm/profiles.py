from enum import Enum


class LLMProfile(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"