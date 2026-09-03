from functools import wraps

from strings import get_string


def LanguageStart(func):
    @wraps(func)
    async def wrapper(client, message, *args, **kwargs):
        from server.utils.database import get_user_lang
        user_id = message.from_user.id if message.from_user else None
        lang = await get_user_lang(user_id) if user_id else "en"
        _ = get_string(lang)
        return await func(client, message, _, *args, **kwargs)
    return wrapper


def Language(func):
    @wraps(func)
    async def wrapper(client, callback_query, *args, **kwargs):
        from server.utils.database import get_user_lang
        user_id = callback_query.from_user.id if callback_query.from_user else None
        lang = await get_user_lang(user_id) if user_id else "en"
        _ = get_string(lang)
        return await func(client, callback_query, _, *args, **kwargs)
    return wrapper
