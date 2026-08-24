import os
from together import AsyncTogether
from together.types.chat.completion_create_params import MessageChatCompletionUserMessageParam, MessageChatCompletionSystemMessageParam

from .llm import LLM, FinishReason

_PROVIDER = "together"

_FINISH_REASONS = {
    "stop": FinishReason.STOP,
    "eos": FinishReason.STOP,
    "length": FinishReason.MAX_TOKENS,
    "tool_calls": FinishReason.OTHER,
}

class Together(LLM):
    def __init__(self):
        self.__client = AsyncTogether(api_key=os.getenv("TOGETHER_API_KEY"))
        self.__model = os.getenv("TOGETHER_MODEL")

    def parallelism(self):
        return 100

    async def ask_generic_question(self, system_prompt: str, question: str, temperature: float) -> LLM.SimpleResponse:
        response = await self.__client.chat.completions.create(
            model=self.__model,
            messages=[
                MessageChatCompletionUserMessageParam(role="user", content=question),
                MessageChatCompletionSystemMessageParam(role="system", content=system_prompt),
            ],
            logprobs=1,
            temperature=temperature,
        )

        choice = response.choices[0]
        raw_finish = getattr(choice, "finish_reason", None)
        finish_reason = _FINISH_REASONS.get(
            str(getattr(raw_finish, "value", raw_finish)).lower(), FinishReason.UNKNOWN
        )

        return LLM.SimpleResponse(
            answer=choice.message.content,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            finish_reason=finish_reason,
            model=self.__model,
            provider=_PROVIDER,
        )