from ai_architect.infrastructure.llm.factory import create_llm
from ai_architect.infrastructure.llm.profiles import LLMProfile


def main() -> None:
    """Minimal connectivity check against the configured provider.

    Uses the active provider configuration, so the same command works
    for Ollama and OpenCode Zen without edits.
    """
    llm = create_llm(LLMProfile.LOW)

    response = llm.invoke(
        "Explain what an AWS EC2 instance is in one sentence."
    )

    print(response.content)


if __name__ == "__main__":
    main()
