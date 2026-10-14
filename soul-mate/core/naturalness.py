"""发送前的少量真人感硬规则检查。

这里只拦截 Owner 已明确否定、且可以可靠识别的模式。语气是否合适仍由
隐藏决策器和渲染器判断，不能把真人表达退化成关键词替换器。
"""
from __future__ import annotations

import re
from collections.abc import Mapping


_LAUGHTER_RE = re.compile(r"(?:哈{2,}|笑死|hhh+)", re.IGNORECASE)
_NOT_BUT_RE = re.compile(r"不是[^\n。！？]{0,40}而是")
_DETACHED_LOCATION_ACK_RE = re.compile(r"^嗯[，,\s]+(?:在)?哪(?:里|儿)?(?:呀|啊|呢)?[？?]?")
_EMPTY_FOLLOWUP_RE = re.compile(r"^(?:嗯[？?！!\s]*)?(?:怎么啦|怎么了|在干嘛|你呢|在哪(?:呀|呢)?|什么呀)[？?！!\s]*$")
_AMBIGUOUS_STUDY_DURATION_RE = re.compile(r"(?:^|\n)\s*你(?:复习|学习|学|背书|做题)多久了[？?]?\s*(?:$|\n)")
_USER_CLARIFICATION_RE = re.compile(r"你(?:是)?(?:刚才)?(?:说|问|指的).{0,18}吗")
_DIRECT_CLARIFICATION_ANSWER_RE = re.compile(
    r"(?:^|\n)\s*(?:对|嗯[，,\s]*对|不是|我(?:刚才)?(?:是)?(?:想问|问的是|说的是)|刚才(?:问|说)的是)"
)
_SHORT_QUESTION_ONLY_RE = re.compile(r"^[^。！？!?\n]{1,18}[？?]$")
_UNSUPPORTED_INTIMACY_RE = re.compile(
    r"(?:^|[\n ])(?:我)?(?:真的|有点|已经)?(?:喜欢你|爱你|想和你在一起)(?:[。！!？? ]|$)"
)
_EXPLICIT_ROMANTIC_USER_RE = re.compile(r"(?:喜欢你|爱你|在一起|女朋友|表白)")
_META_EVASION_RE = re.compile(r"(?:这个话题对我来说有点突然|不知道怎么接|这话题有点突然)")
_SERIOUS_MARKERS = (
    "崩了",
    "没考好",
    "失败",
    "难受",
    "害怕",
    "焦虑",
    "死亡",
    "怕死",
    "吵架",
    "分手",
    "伤人",
    "自责",
    "身体焦虑",
    "别来烦",
    "不想活",
)
_AI_PHRASES = (
    "我理解你的感受",
    "听起来你现在",
    "你的感受是完全合理的",
    "我们可以从以下",
    "谢谢你愿意和我分享",
)


def find_reply_violations(
    *,
    user_message: str,
    reply_text: str,
    decision: Mapping[str, object] | None = None,
    relationship_stage: str = "",
    intimacy: int | float = 0,
) -> list[str]:
    """返回需要定向重写的原因；不修改原回复。"""
    reply = str(reply_text or "").strip()
    if not reply:
        return ["回复为空"]
    constraints = decision.get("reply_constraints") if isinstance(decision, Mapping) else None
    avoid = constraints.get("avoid", []) if isinstance(constraints, Mapping) else []
    normalized_avoid = {str(item).strip().lower() for item in avoid if str(item).strip()}
    serious_context = any(marker in str(user_message or "") for marker in _SERIOUS_MARKERS)
    violations: list[str] = []
    if _LAUGHTER_RE.search(reply) and (serious_context or "laughter" in normalized_avoid):
        violations.append("严肃或负面场景中出现了笑声")
    if _NOT_BUT_RE.search(reply):
        violations.append("使用了模板化的‘不是……而是……’句式")
    if _DETACHED_LOCATION_ACK_RE.search(reply):
        violations.append("普通地点追问前加了显得疏离的‘嗯’")
    if len(str(user_message or "").strip()) >= 12 and _EMPTY_FOLLOWUP_RE.fullmatch(reply):
        violations.append("只留下空泛反问，没有回应对方刚刚提供的具体内容")
    if (
        _SHORT_QUESTION_ONLY_RE.fullmatch(reply)
        and (serious_context or len(str(user_message or "").strip()) >= 18)
    ):
        violations.append("只提出一个很短的问题，没有先给出对当前内容的实质回应")
    if _AMBIGUOUS_STUDY_DURATION_RE.search(reply):
        violations.append("学习时长追问的时间范围不清楚，没有说清是今天、当前一次学习还是整个备考阶段")
    if _USER_CLARIFICATION_RE.search(str(user_message or "")) and not _DIRECT_CLARIFICATION_ANSWER_RE.search(reply):
        violations.append("没有直接回答对方对上一问范围的确认，可能弄反了说话人或话题")
    if any(phrase in reply for phrase in _AI_PHRASES):
        violations.append("使用了客服式或心理咨询式套话")
    if (
        str(relationship_stage or "").strip() in {"陌生", "初识", "试探"}
        and float(intimacy or 0) <= 18
        and _UNSUPPORTED_INTIMACY_RE.search(reply)
        and not _EXPLICIT_ROMANTIC_USER_RE.search(str(user_message or ""))
        and not re.search(r"喜欢你(?:的画|的想法|这个分享|这句话)", reply)
    ):
        violations.append("初识阶段无当前语境依据地推进亲密关系")
    if serious_context and _META_EVASION_RE.search(reply) and not re.search(
        r"(?:难受|害怕|焦虑|死亡|累|压力|听着|陪你|先缓一缓)", reply
    ):
        violations.append("严肃话题被元话语带过，没有回应对方真正难受的内容")
    requested_structure = any(
        marker in str(user_message or "")
        for marker in ("列个", "列一下", "列出", "清单", "分步骤", "具体步骤", "几点建议")
    )
    if (
        re.search(r"(?:^|\n)\s*[一二三四五六七八九十123456789][、.．]", reply)
        and not requested_structure
    ):
        violations.append("普通聊天被整理成了编号总结")
    return list(dict.fromkeys(violations))


def retry_instruction(violations: list[str]) -> str:
    joined = "；".join(violations)
    return (
        "上一版候选回复没有发送，因为出现了以下明确问题："
        f"{joined}。请重新独立生成一次，只输出最终微信文本。"
        "不要解释修改过程，不要机械替换单个词；重新判断场景、情绪、关系和完整语义。"
    )
