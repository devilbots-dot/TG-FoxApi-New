import os
import yaml

LANGUAGES = {
    "en": "🇬🇧 English",
    "ar": "🇸🇦 العربية",
    "ar_eg": "🇪🇬 مصري",
    "ru": "🇷🇺 Русский",
    "uz": "🇺🇿 O'zbekcha",
    "es": "🇪🇸 Español",
    "tr": "🇹🇷 Türkçe",
    "zh": "🇨🇳 中文",
    "bn": "🇧🇩 বাংলা",
    "fa": "🇮🇷 فارسی",
}

DEFAULT_LANGUAGE = "en"

_cache: dict[str, dict] = {}


def get_string(lang: str = "en") -> dict:
    if lang in _cache:
        return _cache[lang]

    path = os.path.join(
        os.path.dirname(__file__),
        "langs",
        f"{lang}.yml",
    )

    if not os.path.exists(path):
        lang = DEFAULT_LANGUAGE
        path = os.path.join(os.path.dirname(__file__), "langs", f"{lang}.yml")

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    # Merge English defaults so every key is always resolvable
    if lang != DEFAULT_LANGUAGE:
        en_path = os.path.join(os.path.dirname(__file__), "langs", "en.yml")
        if en_path not in _cache:
            with open(en_path, "r", encoding="utf-8") as f:
                _cache[DEFAULT_LANGUAGE] = yaml.safe_load(f) or {}
        merged = {**_cache[DEFAULT_LANGUAGE], **data}
        _cache[lang] = merged
        return merged

    _cache[lang] = data
    return data


def clear_cache() -> None:
    """Force reload of all language files on next access."""
    _cache.clear()
