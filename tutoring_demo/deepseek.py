"""DeepSeek JSON extraction, with lazy client creation and no import-time networking."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from .consultation import ConsultationState, ExtractionError, ExtractionPatch, _next_question, _summary, validate_patch


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ExtractionError("model JSON contains duplicate keys")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ExtractionError("model JSON contains a non-standard numeric constant")


def _contains_raw_score(segment: str, score: str) -> bool:
    if any(character.isdigit() for character in score):
        # A raw 70 or 70分 must not match the tail of 170 or 170分; likewise,
        # ratios and decimals must not become part of a different number.
        return re.search(r"(?<![\d./%+\-])" + re.escape(score) + r"(?![\d./%+\-])", segment) is not None
    return score in segment


def _normalize_reply(raw_reply: Any) -> str:
    if not isinstance(raw_reply, str):
        raise ExtractionError("model reply must be a string")
    if any(ord(c) < 32 and c not in "\n\r" for c in raw_reply):
        raise ExtractionError("model reply contains control characters")
    reply = re.sub(r"[\r\n]+", " ", raw_reply).strip()
    if not reply or len(reply) > 800:
        raise ExtractionError("model reply is empty or too long")
    return reply


def _reply_preserves_intent(before: ConsultationState, after: ConsultationState, required: str, reply: str) -> bool:
    """Conservative guard: questionable rewrites fall back to the program's reply."""
    if before.grade is not None and after.grade is None and any(subject in reply and subject not in after.subjects for subject in before.subjects):
        return False
    if before.introduced and re.search(r"老师的咨询助理|我是.{0,15}(?:老师|咨询|助理)|介绍一下", reply):
        return False
    if "我是老师的咨询助理" in required and "咨询助理" not in reply:
        return False
    if re.search(r"免费|很便宜|优惠|折扣|包过|保提分|保证|承诺|一定(?:能|会)|确保(?:考|提高|提升)|提高到|提升到|安排了|安排在|我来安排|可以安排|明天|后天|周[一二三四五六日天]|星期", reply):
        return False
    if re.search(r"\d\s*(?:元|小时|分钟|课时)|人民币|课时费|价格是|收费是|费用是", reply):
        return False
    allowed_text = json.dumps(after.to_dict(), ensure_ascii=False) + required
    if set(re.findall(r"\d+(?:\.\d+)?", reply)) - set(re.findall(r"\d+(?:\.\d+)?", allowed_text)):
        return False
    for subject in ("语文", "数学", "英语", "物理", "化学", "生物", "历史", "地理", "政治", "道德与法治"):
        if subject in reply and subject not in before.subjects and subject not in after.subjects and subject not in required:
            return False
    for grade in re.findall(r"(?:小学|初中|高中)?[一二三四五六七八九十1-9]{1,2}年级|[初高][一二三123]", reply):
        if grade not in (after.grade or "") and grade not in required:
            return False
    referral = "这个问题需要老师进一步确认。"
    if referral in required and referral not in reply:
        return False
    question = _next_question(after)
    question_count = reply.count("？") + reply.count("?")
    if question is not None and question in required:
        if question_count != 1:
            return False
        if after.grade is None:
            return "年级" in reply and not re.search(r"补哪|哪(?:一|几)?科|哪门|成绩|分数", reply)
        if not after.subjects:
            return ("科" in reply and "补" in reply and not re.search(r"几年级|什么年级|哪个年级|成绩|分数", reply))
        missing_subject = next(subject for subject in after.subjects if subject not in after.scores)
        if missing_subject not in reply or "期末" not in reply or not re.search(r"成绩|分数", reply):
            return False
        if re.search(r"几年级|什么年级|哪个年级|补哪|哪(?:一|几)?科|哪门", reply):
            return False
        # A question about one subject cannot turn into asking several subjects.
        return not any(subject != missing_subject and re.search(re.escape(subject) + r".{0,12}(?:多少|怎么样|如何|呢[？?])", reply) for subject in after.subjects)
    if after.completed and _summary(after) in required:
        if question_count or after.grade not in reply:
            return False
        subject_pattern = "|".join(re.escape(subject) for subject in sorted(after.subjects, key=len, reverse=True))
        for subject in after.subjects:
            valid_pair = False
            for match in re.finditer(re.escape(subject), reply):
                tail = reply[match.end():]
                next_subject = re.search(subject_pattern, tail)
                segment = tail[:next_subject.start()] if next_subject else tail
                score = after.scores[subject]
                if (score is None and re.search(r"不清楚|不知道|未知|待确认|暂未了解|尚未了解", segment)) or (score is not None and _contains_raw_score(segment, score)):
                    valid_pair = True
                    break
            if not valid_pair:
                return False
        return True
    # Clarification/referral is not a standard next question: keep its exact
    # wording, allowing only a short acknowledgement around it.
    return required in reply and question_count == required.count("？") + required.count("?")


@dataclass(frozen=True)
class TurnAnalysis:
    patch: ExtractionPatch
    reply_candidate: str | None


def choose_reply(
    analysis: TurnAnalysis,
    before: ConsultationState,
    after: ConsultationState,
    required: str | None,
) -> str | None:
    """Choose locally; a model candidate never determines the intake workflow."""
    if required is None:
        return None
    before = ConsultationState.from_dict(before.to_dict())
    after = ConsultationState.from_dict(after.to_dict())
    if not isinstance(required, str) or not required.strip() or len(required) > 1200 or any(ord(c) < 32 for c in required):
        raise ExtractionError("required reply must be valid single-line text")
    patch = validate_patch(analysis.patch, before)
    if patch.start_new_consultation:
        # required was produced by apply_patch from the newly cleared state.
        # Never use a candidate generated while the old state was visible.
        body = required.removeprefix("您好，")
        return "您好，可以的。" + body
    if analysis.reply_candidate is None:
        return required
    candidate = _normalize_reply(analysis.reply_candidate)
    return candidate if _reply_preserves_intent(before, after, required, candidate) else required


class DeepSeekExtractor:
    def __init__(self, api_key: str, model: str = "deepseek-flash", client: Any = None):
        if not isinstance(api_key, str) or not api_key.strip():
            raise ExtractionError("a local DeepSeek API key is required")
        if not isinstance(model, str) or not model.strip() or len(model) > 100:
            raise ExtractionError("a valid model name is required")
        if model.strip() != "deepseek-flash":
            raise ExtractionError("this demo only allows deepseek-flash")
        self._api_key = api_key.strip()
        self.model = model.strip()
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self._api_key,
                base_url="https://api.deepseek.com",
                timeout=30.0,
                max_retries=1,
            )
        return self._client

    def analyze_turn(self, state: ConsultationState, last_question: str | None, message: str) -> TurnAnalysis:
        """One Flash request supplies extraction and an optional, untrusted reply."""
        state = ConsultationState.from_dict(state.to_dict())
        if not isinstance(message, str) or not message.strip() or len(message) > 4000:
            raise ExtractionError("parent message must contain 1 to 4000 characters")
        if last_question is not None and (not isinstance(last_question, str) or len(last_question) > 1000):
            raise ExtractionError("last question must be a string of at most 1000 characters")
        prompt = (Path(__file__).resolve().parent.parent / "prompts" / "teacher_turn.md").read_text(encoding="utf-8")
        payload = {"current_state": state.to_dict(), "last_assistant_question": last_question, "parent_message": message}
        try:
            response = self._get_client().chat.completions.create(
                model="deepseek-flash",
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
                max_tokens=2048,
                extra_body={"thinking": {"type": "disabled"}},
            )
        except Exception:
            raise ExtractionError("DeepSeek turn request failed; consultation must pause") from None
        try:
            choice = response.choices[0]
            if getattr(choice, "finish_reason", None) not in (None, "stop"):
                raise ExtractionError("model turn did not finish normally")
            content = choice.message.content
            if not isinstance(content, str) or not content.strip() or len(content) > 20000:
                raise ExtractionError("model turn JSON is empty or too long")
            value = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            if not isinstance(value, dict) or set(value) != {"extraction", "reply_candidate"}:
                raise ExtractionError("model turn must contain exactly extraction and reply_candidate")
            extraction = value["extraction"]
            fields = {"grade", "subjects", "scores", "clarification", "off_topic", "start_new_consultation"}
            if not isinstance(extraction, dict) or set(extraction) != fields:
                raise ExtractionError("model turn extraction must contain exactly six fields")
            patch = validate_patch(extraction, state)
            candidate = None if value["reply_candidate"] is None else _normalize_reply(value["reply_candidate"])
            return TurnAnalysis(patch, candidate)
        except ExtractionError:
            raise
        except (ValueError, TypeError, AttributeError, IndexError, KeyError):
            raise ExtractionError("DeepSeek returned invalid turn JSON") from None

    def extract(self, state: ConsultationState, last_question: str | None, message: str) -> ExtractionPatch:
        state = ConsultationState.from_dict(state.to_dict())
        if not isinstance(message, str) or not message.strip() or len(message) > 4000:
            raise ExtractionError("parent message must contain 1 to 4000 characters")
        if last_question is not None and (not isinstance(last_question, str) or len(last_question) > 1000):
            raise ExtractionError("last question must be a string of at most 1000 characters")
        prompt = (Path(__file__).resolve().parent.parent / "prompts" / "teacher_demo.md").read_text(encoding="utf-8")
        payload = {
            "current_state": state.to_dict(),
            "last_assistant_question": last_question,
            "parent_message": message,
        }
        try:
            response = self._get_client().chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                response_format={"type": "json_object"},
                temperature=0.2,
                max_tokens=2048,
                # 字段提取不需要思考链；默认思考会先耗尽输出额度，留下空JSON。
                extra_body={"thinking": {"type": "disabled"}},
            )
        except Exception:
            # Do not expose provider errors, which can contain request text or credentials.
            raise ExtractionError("DeepSeek request failed; consultation must pause") from None
        try:
            choice = response.choices[0]
            if getattr(choice, "finish_reason", None) not in (None, "stop"):
                raise ExtractionError("model response did not finish normally")
            content = choice.message.content
            if not isinstance(content, str) or not content.strip() or len(content) > 20000:
                raise ExtractionError("model response is empty or too long")
            value = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            return validate_patch(value, state)
        except ExtractionError:
            raise
        except (ValueError, TypeError, AttributeError, IndexError, KeyError) as exc:
            raise ExtractionError("DeepSeek returned invalid JSON extraction") from exc

    def compose_reply(
        self,
        state_before: ConsultationState,
        state_after: ConsultationState,
        last_question: str | None,
        parent_message: str,
        required_reply: str,
    ) -> str:
        """Warmly phrase a deterministic question/summary without changing its facts."""
        before = ConsultationState.from_dict(state_before.to_dict())
        after = ConsultationState.from_dict(state_after.to_dict())
        if not isinstance(parent_message, str) or not parent_message.strip() or len(parent_message) > 4000:
            raise ExtractionError("parent message must contain 1 to 4000 characters")
        if last_question is not None and (not isinstance(last_question, str) or len(last_question) > 1000):
            raise ExtractionError("last question must be a string of at most 1000 characters")
        if not isinstance(required_reply, str) or not required_reply.strip() or len(required_reply) > 1200:
            raise ExtractionError("required reply must contain 1 to 1200 characters")
        prompt = (Path(__file__).resolve().parent.parent / "prompts" / "teacher_reply.md").read_text(encoding="utf-8")
        payload = {
            "state_before": before.to_dict(),
            "state_after": after.to_dict(),
            "last_assistant_question": last_question,
            "parent_message": parent_message,
            "required_reply": required_reply,
        }
        try:
            response = self._get_client().chat.completions.create(
                model="deepseek-flash",
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                response_format={"type": "json_object"},
                temperature=0.5,
                max_tokens=1024,
                extra_body={"thinking": {"type": "disabled"}},
            )
        except Exception:
            raise ExtractionError("DeepSeek reply request failed; consultation must pause") from None
        try:
            choice = response.choices[0]
            if getattr(choice, "finish_reason", None) not in (None, "stop"):
                raise ExtractionError("model reply did not finish normally")
            content = choice.message.content
            if not isinstance(content, str) or not content.strip() or len(content) > 6000:
                raise ExtractionError("model reply JSON is empty or too long")
            value = json.loads(content, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
            if not isinstance(value, dict) or set(value) != {"reply"} or not isinstance(value["reply"], str):
                raise ExtractionError("model reply must contain exactly one string field: reply")
            reply = _normalize_reply(value["reply"])
        except ExtractionError:
            raise
        except (ValueError, TypeError, AttributeError, IndexError, KeyError):
            raise ExtractionError("DeepSeek returned invalid reply JSON") from None
        return reply if _reply_preserves_intent(before, after, required_reply, reply) else required_reply
