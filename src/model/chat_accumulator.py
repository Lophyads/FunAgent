"""Attempt-local Chat Completions fragments; final validation stays in Decode."""
from .protocols import ModelError, ProviderResponse


class ChatAccumulator:
    def __init__(self, adapter):
        self.adapter = adapter
        self.text, self.calls, self.usage = [], {}, None
        self.finish = None
        self.size = 0

    def _fragment(self, value):
        if not isinstance(value, str):
            raise ModelError("PROTOCOL")
        self.size += len(value)
        if self.size > 1_048_576:
            raise ModelError("PROTOCOL")
        return value

    def Add(self, response):
        try:
            data = response.payload
            if data.get('usage') is not None:
                if self.usage is not None:
                    raise ValueError
                self.usage = data['usage']
            choices = data['choices']
            if not isinstance(choices, list) or len(choices) > 1:
                raise ValueError
            if not choices:
                if self.finish is None or self.usage is None:
                    raise ValueError
                return ''
            choice = choices[0]
            if type(choice['index']) is not int or choice['index'] != 0 or self.finish is not None:
                raise ValueError
            delta = choice['delta']
            if not isinstance(delta, dict) or delta.get('role') not in (None, 'assistant'):
                raise ValueError
            text = delta.get('content')
            text = '' if text is None else self._fragment(text)
            self.text.append(text)
            calls = delta.get('tool_calls')
            if calls is not None and not isinstance(calls, list):
                raise ValueError
            for fragment in calls or ():
                index = fragment['index']
                if type(index) is not int or not 0 <= index < 128:
                    raise ValueError
                call = self.calls.setdefault(index, {'id': '', 'type': 'function',
                                                    'function': {'name': '', 'arguments': ''}})
                if fragment.get('type') not in (None, 'function'):
                    raise ValueError
                if fragment.get('id') is not None:
                    call['id'] += self._fragment(fragment['id'])
                function = fragment.get('function') or {}
                for key in ('name', 'arguments'):
                    if function.get(key) is not None:
                        call['function'][key] += self._fragment(function[key])
            self.finish = choice.get('finish_reason')
            return text
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ModelError('PROTOCOL') from None

    def Finish(self):
        if self.finish is None:
            raise ModelError('TRUNCATED')
        if sorted(self.calls) != list(range(len(self.calls))):
            raise ModelError('PROTOCOL')
        return self.adapter.Decode(ProviderResponse({'choices': [{'message': {
            'role': 'assistant', 'content': ''.join(self.text), 'tool_calls': list(self.calls.values())},
            'finish_reason': self.finish}], 'usage': self.usage}))

    def Snapshot(self):
        """Bounded partial wire content, without Finish/Decode or state changes."""
        return {'content': ''.join(self.text), 'tool_calls': [
            {'index': index, 'id': call['id'], 'type': call['type'], 'function': dict(call['function'])}
            for index, call in sorted(self.calls.items())], 'finish_reason': self.finish,
            'partial': self.finish is None}
