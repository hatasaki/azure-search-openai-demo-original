from typing import Any, Sequence

import openai
import tiktoken
from azure.search.documents import SearchClient
from azure.search.documents.models import QueryType
from approaches.approach import Approach
from core.ichatgptproxy import IChatGptProxy
from core.messagebuilder import MessageBuilder
from text import nonewlines

class ChatReadRetrieveReadApproach(Approach):
    # Chat roles
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"

    """
    Simple retrieve-then-read implementation, using the Cognitive Search and OpenAI APIs directly. It first retrieves
    top documents from search, then constructs a prompt with them, and then uses OpenAI to generate an completion
    (answer) with that prompt.
    """
    system_message_chat_conversation = """Assistant helps the company employees with their healthcare plan questions, and questions about the employee handbook. Be brief in your answers.
Answer ONLY with the facts listed in the list of sources below. If there isn't enough information below, say you don't know. Do not generate answers that don't use the sources below. If asking a clarifying question to the user would help, ask the question.
For tabular information return it as an html table. Do not return markdown format.
Each source has a name followed by colon and the actual information, always include the source name for each fact you use in the response. Use square brackets to reference the source, e.g. [info1.txt]. Don't combine sources, list each source separately, e.g. [info1.txt][info2.pdf].
{follow_up_questions_prompt}
{injected_prompt}
"""
    follow_up_questions_prompt_content = """Generate three very brief follow-up questions that the user would likely ask next about their healthcare plan and employee handbook. 
    Use double angle brackets to reference the questions, e.g. <<Are there exclusions for prescriptions?>>.
    Try not to repeat questions that have already been asked.
    Only generate questions and do not generate any text before or after the questions, such as 'Next Questions'"""

    query_prompt_template = """Assistant is intelligent bot that helps to generate a search query.
    Below is a history of the conversation so far, and a new question asked by the user that needs to be answered by searching in a knowledge base about employee healthcare plans and the employee handbook.
    Generate a SEARCH QUERY based on the previous chat history and the new question. 
    Do not include cited source filenames and document names e.g info.txt or doc.pdf in the search query terms.
    Do not include any text inside [] or <<>> in the search query terms.
    Do not include special characters like '+' extra
    If the question is not in English, translate the question to English before generating the search query.

Search Query:
"""
    query_prompt_few_shots = [
        {'role' : USER, 'content' : 'What is included in my Northwind Health Plus plan that is not in standard?' },
        {'role' : ASSISTANT, 'content' : 'Northwind Health Plus plan benefits comparison standard' },
        {'role' : USER, 'content' : 'does my plan cover eye exam' },
        {'role' : ASSISTANT, 'content' : 'Northwind Health Plus eye exam coverage' }
    ]

    def __init__(self, search_client: SearchClient, chatgpt_proxy: IChatGptProxy, sourcepage_field: str, content_field: str):
        self.search_client = search_client
        self.chatgpt_proxy = chatgpt_proxy
        self.sourcepage_field = sourcepage_field
        self.content_field = content_field

    def run(self, history: Sequence[dict[str, str]], overrides: dict[str, Any]) -> Any:
        use_semantic_captions = True if overrides.get("semantic_captions") else False
        top = overrides.get("top") or 3
        exclude_category = overrides.get("exclude_category") or None
        filter = "category ne '{}'".format(exclude_category.replace("'", "''")) if exclude_category else None

        user_q = 'Generate search query for: ' + history[-1]["user"] + ' Chat History:\n{chat_history}'.format(chat_history=self.get_chat_history_as_text(history, False, self.chatgpt_proxy.get_token_limit() - len(history[-1]["user"])))

        # STEP 1: Generate an optimized keyword search query based on the chat history and the last question
        messages = self.get_messages_from_history(
            self.query_prompt_template,
            self.chatgpt_proxy.get_model_name(),
            history,
            user_q,
            self.query_prompt_few_shots
            )

        q = self.chatgpt_proxy.chat_completion(messages, 0.0, max_tokens=32, n=1)

        # STEP 2: Retrieve relevant documents from the search index with the GPT optimized query
        if overrides.get("semantic_ranker"):
            r = self.search_client.search(q, 
                                          filter=filter,
                                          query_type=QueryType.SEMANTIC, 
                                          query_language="en-us", 
                                          query_speller="lexicon", 
                                          semantic_configuration_name="default", 
                                          top=top, 
                                          query_caption="extractive|highlight-false" if use_semantic_captions else None)
        else:
            r = self.search_client.search(q, filter=filter, top=top)
        if use_semantic_captions:
            results = [doc[self.sourcepage_field] + ": " + nonewlines(" . ".join([c.text for c in doc['@search.captions']])) for doc in r]
        else:
            results = [doc[self.sourcepage_field] + ": " + nonewlines(doc[self.content_field]) for doc in r]
        content = "\n".join(results)

        follow_up_questions_prompt = self.follow_up_questions_prompt_content if overrides.get("suggest_followup_questions") else ""
        
        # STEP 3: Generate a contextual and content specific answer using the search results and chat history

        # Allow client to replace the entire prompt, or to inject into the exiting prompt using >>>
        prompt_override = overrides.get("prompt_override")
        if prompt_override is None:
            system_message = self.system_message_chat_conversation.format(injected_prompt="", follow_up_questions_prompt=follow_up_questions_prompt)
        elif prompt_override.startswith(">>>"):
            system_message = self.system_message_chat_conversation.format(injected_prompt=prompt_override[3:] + "\n", follow_up_questions_prompt=follow_up_questions_prompt)
        else:
            system_message = prompt_override.format(follow_up_questions_prompt=follow_up_questions_prompt)
        
        # latest conversation
        user_content = history[-1]["user"] + " \nSources:" + content

        messages = self.get_messages_from_history(
        system_message,
        self.chatgpt_proxy.get_model_name(),
        history,
        user_content)

        chat_content = self.chatgpt_proxy.chat_completion(messages, overrides.get("temperature") or 0.7, max_tokens=1024, n=1)

        msg_to_display = '\n\n'.join([str(message) for message in messages])

        return {"data_points": results, "answer": chat_content, "thoughts": f"Searched for:<br>{q}<br><br>Conversations:<br>" + msg_to_display.replace('\n', '<br>')}
    
    def get_chat_history_as_text(self, history: Sequence[dict[str, str]], include_last_turn: bool=True, max_tokens: int = 4000) -> str:
        history_text = ""
        for h in reversed(history if include_last_turn else history[:-1]):
            history_text = """<|im_start|>user""" + "\n" + h["user"] + "\n" + """<|im_end|>""" + "\n" + """<|im_start|>assistant""" + "\n" + (h.get("bot", "") + """<|im_end|>""" if h.get("bot") else "") + "\n" + history_text
            if len(history_text) > max_tokens:
                break    
        return history_text

    def get_messages_from_history(self, system_prompt: str, modelid: str, history: Sequence[dict[str, str]], user_conv: str, few_shots = []) -> []:
        message_builder = MessageBuilder(system_prompt, modelid)

        #add shots
        print(few_shots)
        if len(few_shots) > 0:
            for shot in few_shots:
                print(shot)
                message_builder.append_message(shot.get('role'), shot.get('content'))

        user_content = user_conv
        append_index = len(few_shots) + 1

        message_builder.append_message(self.USER, user_content, index=append_index)

        for h in reversed(history[:-1]):
            if h.get("bot"):
                message_builder.append_message(self.ASSISTANT, h.get('bot'), index=append_index)
            message_builder.append_message(self.USER, h.get('user'), index=append_index)
            if message_builder.token_length() > self.chatgpt_proxy.get_token_limit():
                break
        
        messages = message_builder.to_messages()
        return messages