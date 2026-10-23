"""可插拔的 LLM 后端抽象层。

支持：
- Anthropic 官方 API（anthropic SDK）
- OpenAI 兼容接口（OpenAI / DeepSeek / Moonshot / 智谱GLM / Ollama / vLLM 等，
  只要暴露 /chat/completions 风格的接口都能用，通过 base_url 切换）

用法：
  llm = get_backend()                      # 按环境变量自动选择
  llm = get_backend(provider="openai")     # 显式指定
  text = llm.complete(system, user)        # 统一入口，返回纯文本

环境变量：
  LLM_PROVIDER            "anthropic"(默认) 或 "openai"
  ANTHROPIC_API_KEY       Anthropic 官方 key
  OPENAI_API_KEY          OpenAI 兼容接口 key（Ollama 等本地服务可填任意非空值）
  OPENAI_BASE_URL         OpenAI 兼容接口地址，默认 https://api.openai.com/v1
  DISTILL_MODEL           模型名，缺省时按 provider 用各自默认值
"""
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# 各 provider 的默认模型（可被 DISTILL_MODEL 或构造参数覆盖）
DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-5",
    "openai": "gpt-4o-mini",
}


class LLMError(Exception):
    pass


class LLMBackend:
    """所有后端的公共接口。子类实现 complete()。"""

    provider = "base"

    def __init__(self, model: Optional[str] = None, max_output_tokens: int = 8192):
        self.model = model or os.environ.get("DISTILL_MODEL") or DEFAULT_MODELS[self.provider]
        self.max_output_tokens = max_output_tokens

    def complete(self, system: str, user: str) -> str:
        raise NotImplementedError


class AnthropicBackend(LLMBackend):
    provider = "anthropic"

    def __init__(self, model: Optional[str] = None, max_output_tokens: int = 8192):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise LLMError("缺少 ANTHROPIC_API_KEY 环境变量（provider=anthropic）")
        super().__init__(model, max_output_tokens)
        import anthropic
        self.client = anthropic.Anthropic(api_key=api_key)

    def complete(self, system: str, user: str) -> str:
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_output_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as e:
            raise LLMError(f"Anthropic API 调用失败: {e}") from e
        return "".join(block.text for block in resp.content if block.type == "text")


class OpenAIBackend(LLMBackend):
    """OpenAI 兼容 /chat/completions 接口。通过 base_url 适配各家/本地服务。"""

    provider = "openai"

    def __init__(
        self,
        model: Optional[str] = None,
        max_output_tokens: int = 8192,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        super().__init__(model, max_output_tokens)
        self.base_url = base_url or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            # 本地服务（Ollama 等）不校验 key，给个占位值
            self.api_key = "not-needed"
            logger.warning("OPENAI_API_KEY 未设置，使用占位 key（适用于本地服务）")
        import openai
        self.client = openai.OpenAI(api_key=self.api_key, base_url=self.base_url)

    def complete(self, system: str, user: str) -> str:
        try:
            resp = self.client.chat.completions.create(
                model=self.model,
                max_tokens=self.max_output_tokens,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except Exception as e:
            raise LLMError(f"OpenAI 兼容接口调用失败 (base_url={self.base_url}): {e}") from e
        return resp.choices[0].message.content or ""


def get_backend(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    max_output_tokens: int = 8192,
) -> LLMBackend:
    provider = (provider or os.environ.get("LLM_PROVIDER") or "anthropic").lower()
    if provider == "anthropic":
        return AnthropicBackend(model=model, max_output_tokens=max_output_tokens)
    if provider == "openai":
        return OpenAIBackend(model=model, max_output_tokens=max_output_tokens)
    raise LLMError(f"未知 LLM provider: {provider!r}（可选: anthropic, openai）")
