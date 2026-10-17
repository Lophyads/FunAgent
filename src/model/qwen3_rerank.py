"""Qwen3 relevance probabilities over chat completions with assistant prefill.

The model's yes/no logprobs are normalized together as in the upstream example:
https://huggingface.co/Qwen/Qwen3-Reranker-8B
LM Studio exposes pre-sampling logprobs. Two forced-token probes recover both
labels even when one is absent from top-20. Shared logprobs must agree; a server
returning post-bias probabilities is rejected. Both probes share Rank's deadline.
"""
from math import exp, isfinite

from .contracts import RerankRequest, RerankResult
from .protocols import Adapter, ModelError, ProviderRequest
from .validation import ValidateRerankRequest


SYSTEM = ('Judge whether the Document meets the requirements based on the Query and the Instruct provided. '
          'Note that the answer can only be "yes" or "no".')
INSTRUCTION = 'Given a web search query, retrieve relevant passages that answer the query'
PREFILL = '<think>\n\n</think>\n\n'


def _probabilities(payload, label):
    try:
        if not isinstance(payload, dict) or 'error' in payload or len(payload['choices']) != 1:
            raise ValueError
        choice = payload['choices'][0]
        # One token is intentionally requested, so length is a valid finish reason.
        if (type(choice['index']) is not int or choice['index'] != 0 or choice['finish_reason'] not in {'stop', 'length'} or
                choice['message']['content'] != label or
                choice['message'].get('tool_calls') or choice['message'].get('reasoning_content')):
            raise ValueError
        tokens = choice['logprobs']['content']
        if len(tokens) != 1 or tokens[0]['token'] != choice['message']['content']:
            raise ValueError
        values = {}
        for item in tokens[0]['top_logprobs']:
            token, logprob = item['token'], item['logprob']
            if type(logprob) not in (int, float) or not isfinite(logprob) or logprob > 0:
                raise ValueError
            # Match exact tokens; case and whitespace variants differ.
            if not isinstance(token, str) or token in values:
                raise ValueError
            values[token] = logprob
        selected = tokens[0]['logprob']
        if type(selected) not in (int, float) or not isfinite(selected) or selected != values[label]:
            raise ValueError
        return values
    except (KeyError, TypeError, ValueError, IndexError, OverflowError, AttributeError):
        raise ModelError('PROTOCOL') from None


class Qwen3RerankAdapter(Adapter[RerankRequest, RerankResult]):
    batch_size = 1

    @staticmethod
    def Encode(request, profile):
        ValidateRerankRequest(request)
        if len(request.texts) != 1:
            raise ModelError('INVALID_REQUEST')
        payload = dict(model=profile.model, messages=[
            dict(role='system', content=SYSTEM),
            dict(role='user', content=f'<Instruct>: {INSTRUCTION}\n<Query>: {request.question}\n<Document>: {request.texts[0]}'),
            dict(role='assistant', content=PREFILL)],
            max_tokens=1, temperature=0, logprobs=True, top_logprobs=20, stream=False)
        # Qwen's canonical tokenizer IDs, verified against its tokenizer.json.
        return ProviderRequest(tuple(dict(payload, logit_bias={str(token): 100})
                                     for token in (9693, 2152)), batch=True)

    @staticmethod
    def Decode(response):
        if not isinstance(response.payload, (tuple, list)) or len(response.payload) != 2:
            raise ModelError('PROTOCOL')
        yes = _probabilities(response.payload[0], 'yes')
        no = _probabilities(response.payload[1], 'no')
        common = yes.keys() & no.keys()
        # Sampling bias must not contaminate the model probabilities used to rank.
        if not common & {'yes', 'no'} or any(abs(yes[t] - no[t]) > .1 for t in common):
            raise ModelError('PROTOCOL')
        scale = max(yes['yes'], no['no'])
        positive, negative = exp(yes['yes'] - scale), exp(no['no'] - scale)
        return RerankResult((positive / (positive + negative),))
