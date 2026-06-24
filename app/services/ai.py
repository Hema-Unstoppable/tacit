from __future__ import annotations

import json
import re
from dataclasses import dataclass

from openai import OpenAI

from app.config import settings
from app.models import Insight, Post, Source, VoiceProfile


@dataclass
class ExtractedInsight:
    title: str
    insight_text: str
    angle: str
    audience: str
    confidence_score: int = 80


class AIClient:
    def extract_insights(self, source: Source, voice_profile: VoiceProfile | None) -> list[ExtractedInsight]:
        raise NotImplementedError

    def generate_posts(self, insight: Insight, voice_profile: VoiceProfile | None) -> list[str]:
        raise NotImplementedError

    def generate_marketing_posts(self, source: Source, voice_profile: VoiceProfile | None, post_count: int) -> list[str]:
        raise NotImplementedError

    def regenerate_post(self, post: Post, voice_profile: VoiceProfile | None) -> str:
        raise NotImplementedError

    def fingerprint_voice(self, voice_profile: VoiceProfile) -> str:
        raise NotImplementedError


class OpenAIClient(AIClient):
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.client = OpenAI(api_key=api_key or settings.openai_api_key)
        self.model = model or settings.openai_model

    def extract_insights(self, source: Source, voice_profile: VoiceProfile | None) -> list[ExtractedInsight]:
        profile_context = _voice_context(voice_profile)
        source_preferences = _source_preferences_context(source)
        prompt = f"""
You are Tacit, a knowledge-to-content assistant for LinkedIn.

Extract 5 publishable LinkedIn insights from the source material.
Prioritize practical lessons, frameworks, contrarian angles, mistakes, warnings, and expert observations.
Do not summarize the document. Find ideas worth posting.

Voice/profile context:
{profile_context}

Source-level generation preferences:
{source_preferences}

Source title: {source.title}
Source text:
{_trim(source.raw_text, 12000)}

Return JSON only with this shape:
{{
  "insights": [
    {{
      "title": "short title",
      "insight_text": "the core publishable idea",
      "angle": "educational | contrarian | checklist | story | framework | warning",
      "audience": "who this is for",
      "confidence_score": 80
    }}
  ]
}}
"""
        data = self._json_response(prompt)
        insights = data.get("insights", [])
        return [
            ExtractedInsight(
                title=item.get("title", "Untitled insight")[:255],
                insight_text=item.get("insight_text", ""),
                angle=item.get("angle", ""),
                audience=item.get("audience", ""),
                confidence_score=int(item.get("confidence_score", 80) or 80),
            )
            for item in insights
            if item.get("insight_text")
        ]

    def generate_posts(self, insight: Insight, voice_profile: VoiceProfile | None) -> list[str]:
        profile_context = _voice_context(voice_profile)
        source_preferences = _source_preferences_context(insight.source if insight else None)
        prompt = f"""
You are Tacit, a LinkedIn content operating system for experts and solo consultants.

Generate 3 distinct LinkedIn posts from the insight below.
Each post should be source-grounded, specific, non-hypey, and written for a professional audience.
Avoid generic AI phrases, fake metrics, fake stories, and fabricated client examples.
Follow the source-level generation preferences carefully, especially length, emoji use, tone, structure, and goal.
Emoji rules:
- no_emojis: do not use emojis.
- light_emojis: use 1 to 3 relevant emojis, preferably at the start of key lines or the final takeaway.
- emoji_friendly: use 5 to 8 relevant emojis as structural markers. Put emojis at the beginning of short lines, headings, takeaway lines, or bullet markers. Avoid random emojis in the middle of paragraphs.
Respect the requested post language.
If tags are provided, include 3 to 6 relevant hashtags at the end of each post. If tags are not provided, infer 3 to 5 specific professional hashtags from the source and audience. Avoid generic tag stuffing.

Voice/profile context:
{profile_context}

Source-level generation preferences:
{source_preferences}

Insight title: {insight.title}
Insight angle: {insight.angle}
Insight audience: {insight.audience}
Insight:
{insight.insight_text}

Return JSON only:
{{
  "posts": [
    "post 1",
    "post 2",
    "post 3"
  ]
}}
"""
        data = self._json_response(prompt)
        posts = [str(post).strip() for post in data.get("posts", []) if str(post).strip()]
        posts = _ensure_emoji_style(posts[:3], insight.source if insight else None)
        return _ensure_post_tags(posts, insight.source if insight else None)

    def generate_marketing_posts(self, source: Source, voice_profile: VoiceProfile | None, post_count: int) -> list[str]:
        profile_context = _voice_context(voice_profile)
        source_preferences = _source_preferences_context(source)
        arc_instructions = _campaign_arc_instructions(_source_preferences(source).get("campaign_arc", "launch_sequence"), post_count)
        prompt = f"""
You are Tacit, a LinkedIn marketing content strategist replacing a professional marketing team for a brand.

{arc_instructions}

Each post must be:
- Useful first, promotional second — no post should feel like a pure ad
- Grounded in the campaign brief — no fabricated metrics, fake testimonials, or unsupported claims
- Formatted for LinkedIn: short paragraphs of 1-3 sentences, white space between blocks, no dense walls of text
- Non-hypey — replace "game-changing / revolutionary / incredible" with specific, confident claims
- End with a soft or direct CTA appropriate to that post's role in the sequence

STYLE MATCHING (critical):
If the brief contains a [STYLE REFERENCE] section, study it carefully before writing a single word:
1. Count the average sentence length — match it exactly
2. Note how they open (question, statement, story, list?) — replicate the pattern
3. Note how they close (question, invitation, imperative?) — replicate the pattern
4. Note vocabulary level and industry terminology — match it
5. Note whether they use bullet lists or paragraphs — match the format
6. Do NOT copy phrases. Replicate the voice and rhythm, not the content.

Voice/profile context:
{profile_context}

Campaign generation preferences:
{source_preferences}

Campaign brief:
{_trim(source.raw_text, 10000)}

Return JSON only:
{{
  "posts": [
    "post 1 full text",
    "post 2 full text"
  ]
}}
"""
        data = self._json_response(prompt)
        posts = [str(post).strip() for post in data.get("posts", []) if str(post).strip()]
        posts = _ensure_emoji_style(posts[:post_count], source)
        return _ensure_post_tags(posts, source)

    def regenerate_post(self, post: Post, voice_profile: VoiceProfile | None) -> str:
        profile_context = _voice_context(voice_profile)
        insight = post.insight
        source = post.source
        source_preferences = _source_preferences_context(source)
        if insight:
            context_block = (
                f"Insight title: {insight.title}\n"
                f"Insight angle: {insight.angle}\n"
                f"Insight audience: {insight.audience}\n"
                f"Insight:\n{insight.insight_text}"
            )
        elif source:
            context_block = f"Source title: {source.title}\nSource text:\n{_trim(source.raw_text, 6000)}"
        else:
            context_block = "No source context available. Use the previous draft below as the only reference for subject matter."
        prompt = f"""
You are Tacit, a LinkedIn content operating system for experts and solo consultants.

Rewrite the LinkedIn post below with a genuinely different angle, opening, and structure than the previous draft.
Keep the same underlying subject matter and facts, but change the entry point — switch between story, contrarian take, checklist, framework, or direct lesson, whichever the previous draft did NOT use.
Do not reuse the previous draft's opening sentence, structure, or phrasing.
Follow the source-level generation preferences carefully, especially length, emoji use, tone, structure, and goal.
Emoji rules:
- no_emojis: do not use emojis.
- light_emojis: use 1 to 3 relevant emojis, preferably at the start of key lines or the final takeaway.
- emoji_friendly: use 5 to 8 relevant emojis as structural markers.
Respect the requested post language.
If tags are provided, include 3 to 6 relevant hashtags at the end. If not provided, infer 3 to 5 specific professional hashtags.

Voice/profile context:
{profile_context}

Source-level generation preferences:
{source_preferences}

{context_block}

Previous draft (write something genuinely different from this — do not reuse its opening or structure):
{_trim(post.draft_text, 3000)}

Return JSON only:
{{"post": "the new post text"}}
"""
        data = self._json_response(prompt)
        new_post = str(data.get("post", "")).strip()
        if not new_post:
            return post.draft_text
        new_post = _ensure_emoji_style([new_post], source)[0]
        return _ensure_post_tags([new_post], source)[0]

    def fingerprint_voice(self, voice_profile: VoiceProfile) -> str:
        prompt = f"""
Create a concise structured writing fingerprint for this LinkedIn voice profile.

Audience: {voice_profile.target_audience}
Tone notes: {voice_profile.tone_notes}
Banned phrases: {voice_profile.banned_phrases}
Examples:
{_trim(voice_profile.example_posts, 8000)}

Return JSON only with keys: tone, structure, vocabulary, opening_style, cta_style, avoid.
"""
        return json.dumps(self._json_response(prompt), indent=2)

    def _json_response(self, prompt: str) -> dict:
        response = self.client.responses.create(
            model=self.model,
            input=prompt,
            text={"format": {"type": "json_object"}, "verbosity": "low"},
            reasoning={"effort": "low"},
        )
        text = response.output_text
        return json.loads(text)


class FallbackAIClient(AIClient):
    def extract_insights(self, source: Source, voice_profile: VoiceProfile | None) -> list[ExtractedInsight]:
        sentences = _sentences(source.raw_text)
        chunks = sentences[:8] or [source.raw_text[:500]]
        insights: list[ExtractedInsight] = []
        for index, sentence in enumerate(chunks[:5], start=1):
            clean = sentence.strip()
            if len(clean) < 40:
                continue
            insights.append(
                ExtractedInsight(
                    title=f"Insight {index}: {clean[:70].rstrip('.')}",
                    insight_text=(
                        f"This source suggests a practical LinkedIn angle: {clean}. "
                        "The post can turn this into a clear lesson, warning, or framework for consultants."
                    ),
                    angle=["educational", "framework", "warning", "contrarian", "checklist"][index % 5],
                    audience=(voice_profile.target_audience if voice_profile else "solo consultants"),
                    confidence_score=70,
                )
            )
        if insights:
            return insights
        return [
            ExtractedInsight(
                title="Turn the source into a practical lesson",
                insight_text="The material contains expertise that can be reframed as a practical LinkedIn lesson for a professional audience.",
                angle="educational",
                audience="solo consultants",
                confidence_score=60,
            )
        ]

    def generate_posts(self, insight: Insight, voice_profile: VoiceProfile | None) -> list[str]:
        audience = voice_profile.target_audience if voice_profile else "solo consultants"
        tone = voice_profile.tone_notes if voice_profile else "clear, practical, professional"
        source_preferences = _source_preferences_context(insight.source if insight else None)
        posts = [
            (
                f"{insight.title}\n\n"
                f"{insight.insight_text}\n\n"
                f"For {audience}, the takeaway is simple: turn buried expertise into a clear operating principle before trying to turn it into content.\n\n"
                f"Preference guide: {source_preferences}\n\n"
                "What document or framework in your business could become a useful post?"
            ),
            (
                "A lot of LinkedIn content starts from a blank page.\n\n"
                "That is usually the wrong starting point.\n\n"
                f"The better starting point is the knowledge already sitting inside your work: {insight.insight_text}\n\n"
                f"Tone target: {tone}.\n\nPreference guide: {source_preferences}."
            ),
            (
                f"One useful way to think about this:\n\n"
                f"1. Find the real lesson.\n"
                f"2. Remove the internal context.\n"
                f"3. Turn it into a practical takeaway.\n"
                f"4. Write it for {audience}.\n\n"
                f"Source insight: {insight.insight_text}\n\nPreference guide: {source_preferences}"
            ),
        ]
        posts = _ensure_emoji_style(posts, insight.source if insight else None)
        return _ensure_post_tags(posts, insight.source if insight else None)

    def generate_marketing_posts(self, source: Source, voice_profile: VoiceProfile | None, post_count: int) -> list[str]:
        preferences = _source_preferences(source)
        tags = preferences.get("tags", "")
        posts = []
        for index in range(1, post_count + 1):
            posts.append(
                f"{source.title}\n\n"
                f"Here is a practical reason this matters to the target audience:\n\n"
                f"{_trim(source.raw_text, 700)}\n\n"
                "The useful marketing angle is simple: connect the offer to a real problem, make the value specific, and give people a clear next step.\n\n"
                f"Post angle {index}: educate first, then invite action.\n\n"
                f"{tags}"
            )
        posts = _ensure_emoji_style(posts, source)
        return _ensure_post_tags(posts, source)

    def regenerate_post(self, post: Post, voice_profile: VoiceProfile | None) -> str:
        audience = voice_profile.target_audience if voice_profile else "solo consultants"
        insight = post.insight
        source = post.source
        base_text = insight.insight_text if insight else (_trim(source.raw_text, 500) if source else post.draft_text)
        new_post = (
            "A different way to look at this:\n\n"
            f"{base_text}\n\n"
            f"For {audience}, the practical move is to test this idea in your own work this week rather than just agreeing with it.\n\n"
            "What's one place this applies to you right now?"
        )
        new_post = _ensure_emoji_style([new_post], source)[0]
        return _ensure_post_tags([new_post], source)[0]

    def fingerprint_voice(self, voice_profile: VoiceProfile) -> str:
        return json.dumps(
            {
                "tone": voice_profile.tone_notes or "clear, useful, specific",
                "structure": "short hook, practical body, clear takeaway",
                "vocabulary": "professional and direct",
                "opening_style": "start with a concrete observation",
                "cta_style": "soft question or practical next step",
                "avoid": voice_profile.banned_phrases or "generic AI phrasing",
            },
            indent=2,
        )


def get_ai_client(api_key: str | None = None, model: str | None = None) -> AIClient:
    effective_key = api_key or settings.openai_api_key
    if not effective_key:
        return FallbackAIClient()
    try:
        return OpenAIClient(api_key=effective_key, model=model or settings.openai_model)
    except Exception:
        return FallbackAIClient()


def _voice_context(voice_profile: VoiceProfile | None) -> str:
    if not voice_profile:
        return "No voice profile yet. Use a clear, practical voice for solo consultants."
    return f"""
Name: {voice_profile.name}
Audience: {voice_profile.target_audience}
Tone notes: {voice_profile.tone_notes}
Banned phrases: {voice_profile.banned_phrases}
Fingerprint: {voice_profile.fingerprint_json}
"""


def _source_preferences_context(source: Source | None) -> str:
    defaults = {
        "length": "medium",
        "emoji_style": "no_emojis",
        "structure": "hook_body_takeaway",
        "tone": "professional",
        "goal": "educate",
        "language": "english",
        "tags": "",
        "extra_instructions": "",
        "campaign_arc": "launch_sequence",
    }
    if source and source.generation_preferences_json:
        try:
            parsed = json.loads(source.generation_preferences_json)
            defaults.update({key: value for key, value in parsed.items() if value})
        except json.JSONDecodeError:
            pass
    return "\n".join(
        [
            f"Length: {defaults['length']}",
            f"Emoji style: {defaults['emoji_style']}",
            f"Emoji placement: {_emoji_instruction(str(defaults['emoji_style']))}",
            f"Structure: {defaults['structure']}",
            f"Tone: {defaults['tone']}",
            f"Goal: {defaults['goal']}",
            f"Post language: {defaults['language']}",
            f"Tags or hashtags: {defaults['tags'] or 'Infer 3 to 5 relevant professional hashtags'}",
            f"Extra instructions: {defaults['extra_instructions'] or 'None'}",
        ]
    )


def _emoji_instruction(style: str) -> str:
    if style == "emoji_friendly":
        return "Use 5 to 8 relevant emojis as line-start markers, list markers, or takeaway markers. Keep them aligned with text structure."
    if style == "light_emojis":
        return "Use 1 to 3 relevant emojis only at key line starts or the final takeaway."
    return "Do not use emojis."


def _trim(text: str, limit: int) -> str:
    return text[:limit] + ("\n[trimmed]" if len(text) > limit else "")


def _sentences(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text)
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", normalized) if part.strip()]


def _ensure_post_tags(posts: list[str], source: Source | None) -> list[str]:
    tags = _preferred_tags(source)
    fallback_tags = ["#LinkedInContent", "#ExpertContent", "#KnowledgeToContent"]
    for tag in fallback_tags:
        if len(tags) >= 3:
            break
        if tag.lower() not in {existing.lower() for existing in tags}:
            tags.append(tag)
    return [_append_missing_tags(post, tags) for post in posts]


def _ensure_emoji_style(posts: list[str], source: Source | None) -> list[str]:
    preferences = _source_preferences(source)
    style = preferences.get("emoji_style", "no_emojis")
    if style == "no_emojis":
        return [_strip_emojis(post) for post in posts]
    if style == "light_emojis":
        return [_add_structural_emojis(post, minimum=2, maximum=3) for post in posts]
    if style == "emoji_friendly":
        return [_add_structural_emojis(post, minimum=6, maximum=8) for post in posts]
    return posts


def _campaign_arc_instructions(arc: str, post_count: int) -> str:
    if arc == "awareness_series":
        base = [
            "Industry insight or trend — a real observation your audience should know about (no promotion)",
            "Expert perspective — your take on a common belief, mistake, or gap in the industry",
            "Practical post — a specific tip, framework, or checklist your audience can apply today",
            "Case study or story — a real outcome, lesson from field experience, or before/after",
            "Brand connection — bridge the expertise to what you offer, soft and earned",
        ]
    elif arc == "evergreen":
        posts_str = "\n".join(f"Post {i+1}: Standalone value post — cover a different angle, message, or benefit of the offer" for i in range(post_count))
        return f"Generate {post_count} standalone posts. Each should work independently on any day, in any order. Vary the angle, format, and entry point across posts.\n{posts_str}"
    else:
        base = [
            "Teaser / Curiosity hook — hint at something valuable without full reveal, create a reason to follow the thread",
            "Value / Educational post — share a key insight or lesson that stands alone, no pitch needed",
            "Social proof or story — a real outcome, field lesson, or before/after narrative grounded in the brief",
            "Offer details — what it is, who it is for, what they get, why it matters now",
            "Call to action — specific, low-friction, direct next step. No vague 'reach out', say exactly what to do",
        ]
    sequence = base[:post_count]
    if post_count > len(base):
        for i in range(len(base), post_count):
            sequence.append(f"Additional value post — reinforce a key message, address an objection, or add a new angle")
    lines = "\n".join(f"Post {i+1}: {role}" for i, role in enumerate(sequence))
    return f"Generate exactly {post_count} posts in this deliberate sequence:\n{lines}\n\nRespect this order — each post has a specific job in the campaign arc."


def _source_preferences(source: Source | None) -> dict[str, str]:
    defaults = {
        "length": "medium",
        "emoji_style": "no_emojis",
        "structure": "hook_body_takeaway",
        "tone": "professional",
        "goal": "educate",
        "language": "english",
        "tags": "",
        "extra_instructions": "",
        "campaign_arc": "launch_sequence",
    }
    if source and source.generation_preferences_json:
        try:
            parsed = json.loads(source.generation_preferences_json)
            defaults.update({key: str(value) for key, value in parsed.items() if value})
        except json.JSONDecodeError:
            pass
    return defaults


def _add_structural_emojis(post: str, minimum: int, maximum: int) -> str:
    post = _normalize_emoji_alignment(post)
    if _emoji_count(post) >= minimum:
        return post

    markers = ["🔍", "⚙️", "✅", "⚖️", "📌", "🧭", "🛠️", "📋"]
    lines = post.splitlines()
    marker_index = 0
    inserted = 0
    updated: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or _emoji_count(post) + inserted >= maximum:
            updated.append(line)
            continue
        if _line_has_emoji(stripped):
            updated.append(line)
            continue

        marker = markers[marker_index % len(markers)]
        marker_index += 1

        if stripped.startswith("• "):
            updated.append(line.replace("• ", f"{marker} ", 1))
            inserted += 1
        elif stripped.startswith("- "):
            updated.append(line.replace("- ", f"{marker} ", 1))
            inserted += 1
        elif re.match(r"^\d+\.\s+", stripped):
            updated.append(re.sub(r"^(\d+\.\s+)", rf"\1{marker} ", line, count=1))
            inserted += 1
        elif stripped.lower().startswith(("takeaway:", "lesson:", "practical point:", "the lesson:")):
            updated.append(f"{marker} {line}")
            inserted += 1
        elif _is_short_structural_line(stripped):
            updated.append(f"{marker} {line}")
            inserted += 1
        else:
            updated.append(line)

    result = "\n".join(updated)
    if _emoji_count(result) >= minimum:
        return result

    second_pass: list[str] = []
    for line in result.splitlines():
        stripped = line.strip()
        if (
            stripped
            and not _line_has_emoji(stripped)
            and _emoji_count("\n".join(second_pass + [line])) < minimum
            and _emoji_count("\n".join(second_pass)) < maximum
        ):
            marker = markers[marker_index % len(markers)]
            marker_index += 1
            second_pass.append(f"{marker} {line}")
        else:
            second_pass.append(line)
    return "\n".join(second_pass)


def _is_short_structural_line(line: str) -> bool:
    if len(line) > 95:
        return False
    if line.endswith("?"):
        return True
    lowercase = line.lower()
    return any(
        phrase in lowercase
        for phrase in [
            "too light",
            "too heavy",
            "the lesson",
            "the objective",
            "the common thread",
            "a practical",
            "useful questions",
            "the value",
        ]
    )


def _strip_emojis(text: str) -> str:
    return re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]\ufe0f?", "", text).replace("  ", " ").strip()


def _normalize_emoji_alignment(post: str) -> str:
    return "\n".join(_move_line_emojis_to_front(line) for line in post.splitlines())


def _move_line_emojis_to_front(line: str) -> str:
    stripped = line.lstrip()
    if not stripped or re.match(r"^[\U0001F300-\U0001FAFF\u2600-\u27BF]", stripped):
        return line
    emojis = re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]\ufe0f?", line)
    if not emojis:
        return line
    without_emojis = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]\ufe0f?", "", line)
    without_emojis = re.sub(r"\s{2,}", " ", without_emojis).rstrip()
    indentation = line[: len(line) - len(stripped)]
    return f"{indentation}{' '.join(emojis)} {without_emojis.strip()}"


def _emoji_count(text: str) -> int:
    return len(re.findall(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", text))


def _line_has_emoji(text: str) -> bool:
    return bool(re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", text))


def _preferred_tags(source: Source | None) -> list[str]:
    if not source or not source.generation_preferences_json:
        return []
    try:
        preferences = json.loads(source.generation_preferences_json)
    except json.JSONDecodeError:
        return []
    raw_tags = str(preferences.get("tags", "")).strip()
    if not raw_tags:
        return []
    tags: list[str] = []
    for item in re.split(r"[\s,;]+", raw_tags):
        clean = re.sub(r"[^A-Za-z0-9_#]", "", item.strip())
        if not clean:
            continue
        if not clean.startswith("#"):
            clean = f"#{clean}"
        if clean.lower() not in {tag.lower() for tag in tags}:
            tags.append(clean)
    return tags[:6]


def _append_missing_tags(post: str, tags: list[str]) -> str:
    existing = re.findall(r"#[A-Za-z0-9_]+", post)
    if len(existing) >= 3:
        return post
    existing_lower = {tag.lower() for tag in existing}
    missing = [tag for tag in tags if tag.lower() not in existing_lower]
    needed = max(0, 3 - len(existing))
    if not missing or needed == 0:
        return post
    return f"{post.rstrip()}\n\n{' '.join(missing[:needed])}"
