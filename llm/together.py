import os
from together import AsyncTogether
from together.types.chat.completion_create_params import MessageChatCompletionUserMessageParam, MessageChatCompletionSystemMessageParam

from .llm import LLM

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

        return LLM.SimpleResponse(
            answer=response.choices[0].message.content,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
        )