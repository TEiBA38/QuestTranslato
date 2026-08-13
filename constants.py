FONT_NAME = "Malgun Gothic"
TARGET_EXTENSIONS = ('.snbt', '.json', '.lang', '.hqm')
SCAN_IGNORE_DIRS = {
    '.git', '.venv', '__pycache__',
    'logs', 'saves', 'resourcepacks', 'shaderpacks',
    'screenshots', 'crash-reports', 'backups',
}

DEFAULT_GLOSSARY = {
    # Vanilla & General Tech
    "Minecraft": "마인크래프트",
    "Creeper": "크리퍼",
    "Zombie": "좀비",
    "Skeleton": "스켈레톤",
    "Pickaxe": "곡괭이",
    "Axe": "도끼",
    "Shovel": "삽",
    "Sword": "검",
    "Hoe": "괭이",
    "Ore": "광석",
    "Ingot": "주괴",
    "Dust": "가루",
    "Nugget": "조각",
    "Gear": "기어",
    "Plate": "판",
    "Wire": "전선",
    "Alloy": "합금",
    "Wrench": "렌치",
    "Machine": "기계",
    "Furnace": "화로",
    "Smelter": "제련기",

    # Create Mod
    "Cogwheel": "톱니바퀴",
    "Large Cogwheel": "큰 톱니바퀴",
    "Shaft": "축",
    "Mechanical Press": "기계식 프레스",
    "Mechanical Mixer": "기계식 혼합기",
    "Andesite Alloy": "안산암 합금",
    "Brass Casing": "황동 케이싱",
    "Andesite Casing": "안산암 케이싱",
    "Copper Casing": "구리 케이싱",
    "Gearbox": "기어박스",
    "Belt": "벨트",
    "Deployer": "전개기",
    "Crushing Wheel": "분쇄 휠",
    "Encased Fan": "케이싱된 선풍기",

    # Mekanism & Thermal & Industrial
    "Steel": "강철",
    "Lead": "납",
    "Silver": "은",
    "Tin": "주석",
    "Bronze": "청동",
    "Invar": "인바",
    "Electrum": "일렉트럼",
    "Uranium": "우라늄",
    "Osmium": "오스뮴",
    "Refined Glowstone": "정제된 발광석",
    "Refined Obsidian": "정제된 흑요석",
    "Machine Frame": "기계 프레임",
    "Redstone Flux": "레드스톤 플럭스",
    "Energy Cell": "에너지 셀",
    "Pulverizer": "분쇄기",
    "Induction Smelter": "유도 제련기",
    "Sawmill": "제재소",
    "Redstone Furnace": "레드스톤 화로",
}

MODELS_GEMINI_FREE = [
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]

MODELS_GEMINI_PAID = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite-preview-02-05",
]

MODELS_OPENAI = [
    "gpt-4o-mini",
    "gpt-4o",
    "gpt-4-turbo",
    "gpt-3.5-turbo",
]

SUPPORTED_LANGUAGES = [
    "한국어 (Korean)",
    "영어 (English)",
    "일본어 (Japanese)",
    "중국어 간체 (Chinese Simplified)",
    "중국어 번체 (Chinese Traditional)",
    "스페인어 (Spanish)",
    "러시아어 (Russian)",
    "독일어 (German)",
    "프랑스어 (French)",
]

# (DeepL code, Google code, LLM Prompt Name)
LANG_CODES = {
    "한국어 (Korean)": ("KO", "ko", "natural Korean"),
    "영어 (English)": ("EN", "en", "natural English"),
    "일본어 (Japanese)": ("JA", "ja", "natural Japanese"),
    "중국어 간체 (Chinese Simplified)": ("ZH", "zh-cn", "natural Simplified Chinese"),
    "중국어 번체 (Chinese Traditional)": ("ZH", "zh-tw", "natural Traditional Chinese"),
    "스페인어 (Spanish)": ("ES", "es", "natural Spanish"),
    "러시아어 (Russian)": ("RU", "ru", "natural Russian"),
    "독일어 (German)": ("DE", "de", "natural German"),
    "프랑스어 (French)": ("FR", "fr", "natural French"),
}


def has_non_latin(text):
    """텍스트에 비라틴 문자(한글, CJK, 키릴 등)가 포함되어 있는지 확인합니다.
    이미 번역된 텍스트를 감지하는 데 사용됩니다."""
    for ch in str(text):
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3:  # Hangul
            return True
        if 0x3040 <= code <= 0x30FF:  # Hiragana / Katakana
            return True
        if 0x4E00 <= code <= 0x9FFF:  # CJK Unified
            return True
        if 0x0400 <= code <= 0x04FF:  # Cyrillic
            return True
    return False


def has_hangul(text):
    """텍스트에 한글이 포함되어 있는지 확인합니다."""
    for ch in str(text):
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3:
            return True
    return False
