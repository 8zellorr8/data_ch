"""Check Gemini configuration; --live evaluates one synthetic conversation only."""

import argparse
import asyncio
import json
from importlib.resources import files
from pathlib import Path

from dotenv import load_dotenv

from backend.errors import DomainError
from backend.evaluator import EvaluationContext
from backend.models import ChecklistVersion, Conversation, Message
from backend.scoring import calculate, validate_result

from .app import build_evaluator


def synthetic_context() -> EvaluationContext:
    definition = json.loads(files("backend").joinpath("default_checklist.json").read_text("utf-8"))
    checklist = ChecklistVersion.model_validate(
        {
            **definition,
            "checklist_id": "freedom-broker",
            "version": 1,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    )
    conversation = Conversation(
        id="connection-check",
        dataset_id="synthetic",
        conversation_id="synthetic-connection",
        language="RU",
        channel="agent chat",
        audio_url=None,
        transcript=[
            Message(id="m1", timestamp="00:00", speaker="client", text="Где найти справку?"),
            Message(
                id="m2",
                timestamp="00:10",
                speaker="operator",
                text="Здравствуйте, меня зовут Анна. Откройте раздел «Помощь» в меню.",
            ),
            Message(id="m3", timestamp="00:20", speaker="client", text="Нашёл, спасибо."),
            Message(
                id="m4",
                timestamp="00:25",
                speaker="operator",
                text="Рада, что справка нашлась. Если понадобится помощь, обращайтесь. До свидания!",
            ),
        ],
    )
    return EvaluationContext(conversation, checklist)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file", type=Path, help="Load a local environment file; never print it"
    )
    parser.add_argument(
        "--live", action="store_true", help="Make real, potentially billed API calls"
    )
    args = parser.parse_args()
    if args.env_file:
        if not args.env_file.is_file():
            parser.error("Environment file does not exist")
        load_dotenv(args.env_file, override=False)
    try:
        evaluator = build_evaluator()
    except ValueError:
        print(json.dumps({"error_code": "AI_CONFIGURATION_ERROR", "live_checked": False}))
        return 2
    if not evaluator.configured:
        print(json.dumps({"error_code": "AI_NOT_CONFIGURED", "live_checked": False}))
        return 2
    if not args.live:
        print(json.dumps({"ai_configured": True, "model": evaluator.model, "live_checked": False}))
        return 0
    context = synthetic_context()
    try:
        output = asyncio.run(evaluator.evaluate(context))
        validate_result(context.checklist, context.conversation, output.result)
    except DomainError as exc:
        print(json.dumps({"error_code": exc.code, "message": exc.message, "live_checked": True}))
        return 1
    print(
        json.dumps(
            {
                "live_checked": True,
                "synthetic_contract_check": "passed",
                "model": output.model,
                "calls": output.calls,
                "criteria": len(output.result.answers),
                "score": calculate(context.checklist, output.result).total,
                "input_tokens": output.input_tokens,
                "output_tokens": output.output_tokens,
                "accuracy_measured": False,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
