"""TEI wire format, with no client or per-request mutable state."""
from .contracts import RerankRequest, RerankResult
from .protocols import Adapter, ModelError, ProviderRequest
from .validation import ValidateRerankRequest, ValidateScores


class TEIRerankAdapter(Adapter[RerankRequest, RerankResult]):
    @staticmethod
    def Encode(request, profile):
        ValidateRerankRequest(request)
        return ProviderRequest(dict(query=request.question, texts=list(request.texts),
                                    raw_scores=False, truncate=False))

    @staticmethod
    def Decode(response):
        data = response.payload
        if not isinstance(data, list) or not data:
            raise ModelError('PROTOCOL')
        scores = [None] * len(data)
        for item in data:
            if (not isinstance(item, dict) or type(item.get('index')) is not int
                    or not 0 <= item['index'] < len(data) or scores[item['index']] is not None
                    or 'score' not in item):
                raise ModelError('PROTOCOL')
            # Validate before storing, so null never masquerades as an empty slot.
            ValidateScores((item['score'],))
            scores[item['index']] = item['score']
        return RerankResult(ValidateScores(tuple(scores)))
