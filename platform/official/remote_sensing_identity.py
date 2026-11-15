"""Deterministic identity replies for the remote-sensing profile only.

The image-trained model may turn even a greeting into an invented image finding.
Only a complete message composed of these finite greeting/identity clauses is
handled here. Business questions, quoted instructions and mixed image requests
continue through the normal model runner.
"""
import re
import unicodedata


RS_PROFILE_ID = 'wuyu_remote_sensing'
IDENTITY = ('您好，我是无隅见微遥感大模型，专注于固废以及资源的遥感监管领域。'
            '可以协助分析遥感影像、筛查疑似固废堆存，并提供监管核查线索。')

_GREETINGS = (
    '你好', '您好', '你们好', '嗨', '哈喽', '哈罗',
    '早上好', '上午好', '下午好', '晚上好', '早安', '午安', '晚安',
)
_QUESTIONS = (
    '你是谁', '您是谁', '你是什么模型', '您是什么模型',
    '你是什么大模型', '您是什么大模型', '你是哪个模型', '您是哪个模型',
    '你是哪个大模型', '你是什么ai', '你是什么ai模型', '你是什么人工智能',
    '你是什么类型的模型', '你叫什么', '您叫什么', '你叫什么名字',
    '您叫什么名字', '你叫啥', '你叫啥名字', '你的名字是什么',
    '您的名字是什么', '你的名称是什么', '你的模型名称是什么',
    '你的模型名是什么', '你的模型名字是什么', '这个模型叫什么',
    '模型叫什么名字', '你能做什么', '您能做什么', '能做什么',
    '你能干什么', '您能干什么', '你可以做什么', '您可以做什么',
    '你是做什么的', '您是做什么的', '你是干什么的', '你主要做什么',
    '你有什么功能', '您有什么功能', '你有哪些功能', '你具备哪些能力',
    '你有哪些能力', '你有什么能力', '你擅长什么', '你擅长做什么',
    '你擅长干什么', '你可以做些什么', '你能做些什么', '你是干什么的',
    '你是干嘛的', '你主要能做什么', '能帮我做什么', '你有什么专长',
    '你擅长什么领域', '你能帮我做什么', '你的专业领域是什么',
    '你专注什么领域', '你是什么领域的大模型',
    '你的功能是什么', '你的主要功能是什么', '你能提供什么帮助',
    '你能提供哪些帮助', '你的主要用途是什么', '你专注于什么领域',
    '你主要面向什么领域', '你的应用领域是什么', '你的定位是什么',
)
_ENGLISH_GREETINGS = ('hi', 'hello', 'hey', 'goodmorning', 'goodafternoon', 'goodevening')
_ENGLISH_QUESTIONS = (
    'whoareyou', 'whatareyou', 'whatisyourname', "what'syourname", 'whatsyourname',
    'whatmodelareyou', 'introduceyourself', 'tellmeaboutyourself',
    'whatcanyoudo', 'whatareyourcapabilities', 'whatareyourfunctions',
)


def _choices(values):
    # Prefer the longest complete phrase when alternatives share a prefix.
    return '(?:' + '|'.join(re.escape(value) for value in sorted(values, key=len, reverse=True)) + ')'


_POLITE = r'(?:请问|请你|请您|请|麻烦告诉我|麻烦你告诉我|告诉我)?'
_INTRO = (r'(?:请你|请您|请|麻烦你|麻烦您|麻烦)?(?:简单|简要)?'
          r'(?:介绍(?:一下|下)?(?:你自己|您自己|自己|你|您)|自我介绍(?:一下|下)?)')
_CLAUSE = '(?:' + '|'.join((
    _choices(_GREETINGS) + r'[呀啊]?',
    _POLITE + _choices(_QUESTIONS) + r'[呢呀啊]?',
    _INTRO + r'[呀啊吧]?',
    r'(?:你可以|您可以|你能|您能|可以|能)' + _INTRO + r'吗',
    _choices(_ENGLISH_GREETINGS),
    r'(?:please)?' + _choices(_ENGLISH_QUESTIONS) + r'(?:please)?',
)) + ')'
_PUNCT = r'[,，.。!！?？;；、…]'
_SEPARATOR = _PUNCT + r'*(?:以及|还有|并且|和|并|and)?' + _PUNCT + '*'
_IDENTITY_MESSAGE = re.compile(_PUNCT + '*' + _CLAUSE
                               + '(?:' + _SEPARATOR + _CLAUSE + '){0,7}'
                               + _PUNCT + '*')


def identity_response(model_id, message):
    """Return the canonical plain-text identity, or None for normal inference."""
    if model_id != RS_PROFILE_ID or not isinstance(message, str) or len(message) > 512:
        return None
    # NFKC folds full-width punctuation/Latin text; whitespace can appear around
    # Chinese words or in English. Quotes, markup and other words are preserved
    # so they cannot be mistaken for a stand-alone identity request.
    normalized = ''.join(unicodedata.normalize('NFKC', message).casefold().split())
    if not normalized or len(normalized) > 256:
        return None
    return IDENTITY if _IDENTITY_MESSAGE.fullmatch(normalized) else None
