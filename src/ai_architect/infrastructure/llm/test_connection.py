from ai_architect.infrastructure.llm.factory import create_llm


def main() -> None:
    llm = create_llm()

    response = llm.invoke(
        "Explain what an AWS EC2 instance is in one sentence."
    )

    print(response.content)


if __name__ == "__main__":
    main()
