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
                or not _text(q.prompt, 4000)):
            _invalid("Invalid clarification question")
        if q.question_id in seen:
            _invalid("Duplicate clarification question ID")
        seen.add(q.question_id)
        if (len(q.options) > 20 or any(not _text(option, 4000) for option in q.options)
                or len(set(q.options)) != len(q.options)):
            _invalid("Questions allow up to twenty distinct text options")
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
        if not isinstance(value, Mapping) or set(value) != {"selected", "text"}:
            _invalid("Answers require selected options and free text")
        selected, text = value["selected"], value["text"]
        if (not isinstance(selected, (list, tuple)) or len(selected) > len(question.options)
                or any(not isinstance(v, str) or v not in question.options for v in selected)
                or len(set(selected)) != len(selected)):
            _invalid("Invalid or duplicate selected option")
        if not isinstance(text, str) or len(text) > 4000:
            _invalid("Answer text must be a string of at most 4000 characters")
        if not selected and not text.strip():
            _invalid("Select at least one option or provide text")
        values[identifier] = {"selected": [option for option in question.options if option in selected],
                              "text": text.strip()}
    return freeze({"answers": [{"question_id": q.question_id, "value": values[q.question_id]} for q in questions]})


def QuestionData(question):
    return {"id": question.question_id, "prompt": question.prompt, "options": list(question.options)}
