"""
config.py — LLM provider configuration.

Selects and returns a LangChain chat model based on the LLM_PROVIDER
environment variable. Adding a new provider only requires a new elif branch.

Environment variables:
  LLM_PROVIDER    anthropic | openai | gemini (default: anthropic)
  LLM_MODEL       model name — defaults depend on provider (see below)
  ANTHROPIC_API_KEY
  OPENAI_API_KEY
  GOOGLE_API_KEY
"""

import os


def get_llm():
    """
    Return a configured LangChain chat model instance.
    Raises ValueError for unknown providers so misconfiguration fails fast.
    """
    provider = os.environ.get("LLM_PROVIDER", "anthropic").lower().strip()

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic  # noqa: PLC0415

        return ChatAnthropic(
            model=os.environ.get("LLM_MODEL", "claude-sonnet-4-6"),
            api_key=os.environ.get("ANTHROPIC_API_KEY"),
            max_tokens=4096,
        )

    if provider == "openai":
        from langchain_openai import ChatOpenAI  # noqa: PLC0415

        return ChatOpenAI(
            model=os.environ.get("LLM_MODEL", "gpt-4o"),
            api_key=os.environ.get("OPENAI_API_KEY"),
        )

    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI  # noqa: PLC0415

        return ChatGoogleGenerativeAI(
            model=os.environ.get("LLM_MODEL", "gemini-2.0-flash-lite"),
            google_api_key=os.environ.get("GOOGLE_API_KEY"),
            max_output_tokens=4096,
            temperature=0,
        )

    raise ValueError(
        f"Unsupported LLM_PROVIDER '{provider}'. "
        "Supported values: anthropic, openai, gemini"
    )
