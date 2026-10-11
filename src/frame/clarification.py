"""Shared structured question and answer validation for all human frontends."""
from collections.abc import Mapping
from .contracts import ClarificationQuestion, DomainError, freeze


def _text(value, maximum):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def _invalid(message):
    raise DomainError("INVALID_REQUEST", message)


def ValidateQuestions(questions):
    if not isinstance(questions, (list, tuple)) or not 1 <= len(questions) <= 5:
        _invalid("Clarification requires one to five questions")
    seen = set()
    for q in questions:
        if (not isinstance(q, ClarificationQuestion) or not _text(q.question_id, 128)
                or not _text(q.prompt, 4000) or not isinstance(q.kind, str)
                or q.kind not in {"single", "multiple", "text"}):
            _invalid("Invalid clarification question")
        if q.question_id in seen:
            _invalid("Duplicate clarification question ID")
        seen.add(q.question_id)
        if q.kind == "text":
            if q.options:
                _invalid("Text questions cannot contain options")
        elif (not 2 <= len(q.options) <= 20 or any(not _text(option, 4000) for option in q.options)
              or len(set(q.options)) != len(q.options)):
            _invalid("Choice questions require distinct text options")
    return tuple(questions)


def ValidateAnswers(questions, answer):
    questions = ValidateQuestions(questions)
    if not isinstance(answer, Mapping) or set(answer) != {"answers"}:
        _invalid("Expected structured clarification answers")
    rows = answer["answers"]
    if not isinstance(rows, (list, tuple)) or len(rows) != len(questions):
        _invalid("Answer every clarification question exactly once")
    by_id = {q.question_id: q for q in questions}
    values = {}
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != {"question_id", "value"}:
            _invalid("Invalid clarification answer")
        identifier, value = row["question_id"], row["value"]
        if not isinstance(identifier, str) or identifier not in by_id or identifier in values:
            _invalid("Unknown or duplicate answer question ID")
        question = by_id[identifier]
        if question.kind == "text":
            valid = _text(value, 4000)
        elif question.kind == "single":
            valid = isinstance(value, str) and value in question.options
        else:
            valid = (isinstance(value, (list, tuple)) and 1 <= len(value) <= len(question.options)
                     and all(isinstance(v, str) and v in question.options for v in value)
                     and len(set(value)) == len(value))
        if not valid:
            _invalid("Invalid answer type or option")
        values[identifier] = value
    return freeze({"answers": [{"question_id": q.question_id, "value": values[q.question_id]} for q in questions]})


def QuestionData(question):
    return {"id": question.question_id, "prompt": question.prompt, "kind": question.kind,
            **({"options": list(question.options)} if question.options else {})}
