FONT_NAME = "Malgun Gothic"
APP_VERSION = "v1.5.2"
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


def has_hangul(text):
    """텍스트에 한글(완성형 및 자모)이 포함되어 있는지 확인합니다."""
    for ch in str(text):
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3:  # 완성형 한글
            return True
        if 0x3131 <= code <= 0x318E:  # 한글 자모
            return True
    return False


def has_non_latin(text, target_lang="한국어 (Korean)"):
    """텍스트가 대상 언어로 이미 번역되었는지 확인합니다 (기본: 한국어/한글 기준 검사).
    외산 모드팩의 러시아어(키릴)나 중국어 번역이 들어있어도 오판하지 않습니다."""
    if not text:
        return False
    if "한국어" in target_lang or "Korean" in target_lang:
        return has_hangul(text)
    elif "일본어" in target_lang or "Japanese" in target_lang:
        for ch in str(text):
            code = ord(ch)
            if 0x3040 <= code <= 0x30FF:  # Hiragana / Katakana
                return True
        return False
    elif "중국어" in target_lang or "Chinese" in target_lang:
        for ch in str(text):
            code = ord(ch)
            if 0x4E00 <= code <= 0x9FFF:  # CJK Unified
                return True
        return False
    elif "러시아어" in target_lang or "Russian" in target_lang:
        for ch in str(text):
            code = ord(ch)
            if 0x0400 <= code <= 0x04FF:  # Cyrillic
                return True
        return False
    return has_hangul(text)

GITHUB_REPO_URL = "https://github.com/TEiBA38/QuestTranslato"

# 공식 GitHub Mark 화이트 실루엣 투명 아이콘 (40x40 PNG Base64)
GITHUB_ICON_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAACgAAAAoCAYAAACM/rhtAAAE3UlEQVR4nO1YTWhcVRT+zkyaQkzVFqpRS8VWVLooqbXg"
    "HxqLVYqCRVwJ4sKNVBC60JWIIqgr3QkWf0AFqQW1ohRUrJEiBHTjRgUjNo1pa1ujTZuk6WQ+OZPvhMvN65sZM9mUHrjM"
    "m/vOPee759zzcx9wkS5wssUsJunrK5LDAtk+Vzez/N3SEskKyWob/FVfs+QWlMXMzOr6vxrAZgAbAawFcJlYTwEYAfAT"
    "gB/N7C/xO0guiUVTi5G8neRbJIdJTpKcITlNckpjWnOT4nmH5B1FsjpiQRdoZrMk3UovAdgBoBvAGQAzOmu5rJhzvksA"
    "nAOwD8BzZnYoZC4aYAJuO4A3AFwFYNwPP4BqCzIagSK+lQCOAnjKzL5oBWSlRXCPAdgL4FIAx6S0FXBOEenU2hUAPiL5"
    "uGSXuttaAPcQgA8BTMgSq/R7Wm4L5TFCbgznXQagV7x/a94D6lEz+6TMkoUAPdo8UkneAGAQQBeAWQF4G8DVALYBWC2g"
    "y8UTHnFQNQBnBewkgK8U2U/I+lXx3W1mv4TOlgHq8TMAW6XgcgA/mNk94rkOwC4ADwIYBjCqswnxrgFwPYD9AF4zs2Gt"
    "+xLAbQD+kTcGJQNFAAtdq98dJM+S/JPkmNLHu0q63Qn/yhJZ8+98jda+qTQ0Jtmu4+FUd7MgoSy4UykkrOy769VZqama"
    "VMysYTUpT4cn9PHg8zVa60ESljLp2CmeeinA5Bx4ZdiiPBfR6vN92qVr9xrr57SxAVeeDd9oo+pIZmy8LwFSlY5bAPRr"
    "TaXMgvH/XiXXPLJelhXm11lJ2crexeY90c8mnvHnHulcgCkHGDvbrCgMy3kkfm9m+2WVphUgJ6UsB3lAgbEiAVqTTuRu"
    "ngcY7pCJr1WOC4CeRg7KnS3X0QKqSMZBlcAoh65rbZLerCxIepREUzc4He9EF2JzMk5kSX1WVapnwY4KZHRp5GDcip2i"
    "5dnmmehtCnAmSy8hwKvHoolz7stlRbqZOS/AxH1TqghhxYoW9ut982x/for2/2bJjDreJZ1TefTnebCql8Mq8AHQc9UW"
    "kht1iBe4ohn5Gq3dAOBWyQyArmtYebBa5uJw65CiNXZS0/9XFO1eSbpUJco6IlNVcXAuw+lVncFIVdG6DWUYCgU2AJNc"
    "T/KYxlG17pOqxx+QvLKpyTIieQXJ9yVnLJEfY734yi9XCcg9AnSY5CMknxVYn/uZ5Au6m1yTNg+JnGUk15C8k+SLJH8t"
    "ADemuT0tgcu6GVc+QfI4yY/VjWwneUZz/s6fvyHZK3emw+e+VrfimzpZYDn/f9p1pbrbAblbwv3M7dPc87qxnfITTfJp"
    "zc8HTjyTfFKt1SFZPwU3Kjm7y8A1a1i9ofxWFyV34wNm9h3JbepARnVTm0jTQ3JdGADwud6n7qupvh8GMKBrQGsNaw6S"
    "5Ca5wt055KmmDQ/cpaNwJLGcN6njmtuUGaQ9ys7jH3L1CMm9JF8n+R7J/lxJsm5AZ+yIXDyqjY60fe5aAHkTyQMCOS3L"
    "OG0t+PKQAvxXmeAEyXMkB0ne2BFwBQo9kp8h+ZssUyd5XwnA+xtbmAu035WqujsKLlGautBb/10kPyW5ruB9nN914nHe"
    "viJZnQbZKF2LWF8tK40d+4CZdNb1ss9p8blOKaZxkfo/+i7SBU3/AcgrtX+64TJ1AAAAAElFTkSuQmCC"
)
