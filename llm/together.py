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

    async def ask_generic_question(self, system_prompt: str, question: str, temperature: float, *, grounded: bool = False) -> LLM.SimpleResponse:
        if grounded:
            # Together has tool calling but no first-party web search, so grounding
            # here would mean bolting on a third-party search API. That is buildable,
            # and it is deliberately not done: the grounded condition would then be
            # measuring our retrieval against Google's rather than model against
            # model, which is not the comparison anyone wants.
            #
            # Raising rather than answering ungrounded is the point. A silent
            # downgrade satisfies the type signature and corrupts any comparison that
            # assumed the request was honoured.
            raise NotImplementedError(
                "Together has no native grounding; wiring an external search provider "
                "would measure retrieval, not the model"
            )

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