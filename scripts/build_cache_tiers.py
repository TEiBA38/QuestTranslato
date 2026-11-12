"""
모든 캐시 파일(마스터 3종, 모드팩 캐시 등)을 전수 조사하여 캐시 티어를 구축하는 스크립트.
- Tier 0: 바닐라 마인크래프트 공식 번역 (Vanilla Minecraft Official)
- Tier 1: 모드 공식 한글패치 (Official Mod ko_kr) 및 사용자 직접 수정
- Tier 2: 고품질 AI (gemini-3.8-flash, claude-sonnet, gpt-6.1-terra 등)
- Tier 3: 표준/가성비 AI (gemini-3.5-flash-lite - 기본값)
"""
import os
import sys
import json
import gzip
import datetime
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

DIST_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dist")
TIERS_FILE = os.path.join(DIST_DIR, "cache_tiers.json")
KO_PACK_KEYS_FILE = os.path.join(DIST_DIR, "ko_pack_keys.json")

# 바닐라 마인크래프트 핵심 공식 어휘 세트 (블록, 아이템, 상태효과, 기본 인터페이스 등)
# 실제 바닐라 공식 en_us -> ko_kr 대표 항목들
VANILLA_OFFICIAL_ITEMS_BASE = {
    # Minerals & Items
    "Stone": "돌", "Cobblestone": "조약돌", "Granite": "화강암", "Diorite": "섬록암", "Andesite": "안산암",
    "Grass Block": "잔디 블록", "Dirt": "흙", "Coarse Dirt": "거친 흙", "Podzol": "회백토",
    "Bedrock": "기반암", "Sand": "모래", "Red Sand": "붉은 모래", "Gravel": "자갈",
    "Coal Ore": "석탄 광석", "Iron Ore": "철 광석", "Gold Ore": "금 광석", "Redstone Ore": "레드스톤 광석",
    "Diamond Ore": "다이아몬드 광석", "Lapis Lazuli Ore": "청금석 광석", "Emerald Ore": "에메랄드 광석", "Nether Quartz Ore": "네더 석영 광석",
    "Deepslate": "심층암", "Cobbled Deepslate": "심층암 조약돌", "Ancient Debris": "고대 잔해",
    "Coal": "석탄", "Charcoal": "목탄", "Diamond": "다이아몬드", "Iron Ingot": "철 주괴",
    "Gold Ingot": "금 주괴", "Copper Ingot": "구리 주괴", "Netherite Ingot": "네더라이트 주괴",
    "Netherite Scrap": "네더라이트 파편", "Stick": "막대기", "Bowl": "그릇", "String": "실",
    "Feather": "깃털", "Gunpowder": "화약", "Wheat Seeds": "밀 씨앗", "Wheat": "밀",
    "Bread": "빵", "Flint": "부싯돌", "Bucket": "양동이", "Water Bucket": "물 양동이",
    "Lava Bucket": "용암 양동이", "Milk Bucket": "우유 양동이", "Compass": "나침반", "Clock": "시계",
    "Glowstone Dust": "발광석 가루", "Bone": "뼈", "Sugar": "설탕", "Rotten Flesh": "썩은 살점",
    "Ender Pearl": "엔더 진주", "Blaze Rod": "블레이즈 막대", "Ghast Tear": "가스트의 눈물",
    "Nether Wart": "네더 와트", "Glass Bottle": "유리병", "Spider Eye": "거미 눈",
    "Fermented Spider Eye": "발효된 거미 눈", "Blaze Powder": "블레이즈 가루", "Magma Cream": "마그마 크림",
    "Brewing Stand": "양조기", "Cauldron": "가마솥", "Eye of Ender": "엔더의 눈",
    "Glistering Melon Slice": "반짝이는 수박 조각", "Experience Bottle": "경험치 병", "Fire Charge": "화염구",
    "Book and Quill": "책과 깃펜", "Written Book": "글이 쓰인 책", "Item Frame": "아이템 액자",
    "Flower Pot": "화분", "Carrot": "당근", "Potato": "감자", "Baked Potato": "구운 감자",
    "Poisonous Potato": "독이 든 감자", "Golden Carrot": "황금 당근", "Skeleton Skull": "스켈레톤 해골",
    "Wither Skeleton Skull": "위더 스켈레톤 해골", "Zombie Head": "좀비 머리", "Creeper Head": "크리퍼 머리",
    "Dragon Head": "드래곤 머리", "Nether Star": "네더의 별", "Pumpkin Pie": "호박 파이",
    "Enchanted Book": "마법이 부여된 책", "Nether Brick": "네더 벽돌", "Prismarine Shard": "프리즈머린 조각",
    "Prismarine Crystals": "프리즈머린 결정", "Armor Stand": "갑옷 거치대", "Iron Nugget": "철 조각",
    "Gold Nugget": "금 조각", "Raw Iron": "철 원석", "Raw Copper": "구리 원석", "Raw Gold": "금 원석",
    # Tools & Armor
    "Wooden Sword": "나무 검", "Wooden Shovel": "나무 삽", "Wooden Pickaxe": "나무 곡괭이", "Wooden Axe": "나무 도끼", "Wooden Hoe": "나무 괭이",
    "Stone Sword": "돌 검", "Stone Shovel": "돌 삽", "Stone Pickaxe": "돌 곡괭이", "Stone Axe": "돌 도끼", "Stone Hoe": "돌 괭이",
    "Golden Sword": "금 검", "Golden Shovel": "금 삽", "Golden Pickaxe": "금 곡괭이", "Golden Axe": "금 도끼", "Golden Hoe": "금 괭이",
    "Iron Sword": "철 검", "Iron Shovel": "철 삽", "Iron Pickaxe": "철 곡괭이", "Iron Axe": "철 도끼", "Iron Hoe": "철 괭이",
    "Diamond Sword": "다이아몬드 검", "Diamond Shovel": "다이아몬드 삽", "Diamond Pickaxe": "다이아몬드 곡괭이", "Diamond Axe": "다이아몬드 도끼", "Diamond Hoe": "다이아몬드 괭이",
    "Netherite Sword": "네더라이트 검", "Netherite Shovel": "네더라이트 삽", "Netherite Pickaxe": "네더라이트 곡괭이", "Netherite Axe": "네더라이트 도끼", "Netherite Hoe": "네더라이트 괭이",
    "Leather Cap": "가죽 모자", "Leather Tunic": "가죽 조끼", "Leather Pants": "가죽 바지", "Leather Boots": "가죽 장화",
    "Iron Helmet": "철 투구", "Iron Chestplate": "철 흉갑", "Iron Leggings": "철 레깅스", "Iron Boots": "철 부츠",
    "Diamond Helmet": "다이아몬드 투구", "Diamond Chestplate": "다이아몬드 흉갑", "Diamond Leggings": "다이아몬드 레깅스", "Diamond Boots": "다이아몬드 부츠",
    "Netherite Helmet": "네더라이트 투구", "Netherite Chestplate": "네더라이트 흉갑", "Netherite Leggings": "네더라이트 레깅스", "Netherite Boots": "네더라이트 부츠",
    "Shield": "방패", "Totem of Undying": "불사의 토템", "Elytra": "겉날개", "Bow": "활", "Crossbow": "쇠뇌", "Trident": "삼지창", "Arrow": "화살",
    # Blocks & Machines
    "Crafting Table": "제작대", "Furnace": "화로", "Blast Furnace": "용광로", "Smoker": "훈연기",
    "Chest": "상자", "Trapped Chest": "덫 상자", "Ender Chest": "엔더 상자", "Barrel": "통",
    "Anvil": "모루", "Chipped Anvil": "약간 손상된 모루", "Damaged Anvil": "심하게 손상된 모루",
    "Smithing Table": "대장장이 작업대", "Fletching Table": "화살 제조대", "Cartography Table": "지도 제작대",
    "Loom": "베틀", "Stonecutter": "석재 절단기", "Grindstone": "숫돌", "Enchanting Table": "마법 부여대",
    "Beacon": "신호기", "Conduit": "전달체", "Campfire": "모닥불", "Soul Campfire": "영혼 모닥불",
    "Shulker Box": "셜커 상자", "Hopper": "깔때기", "Dropper": "투하기", "Dispenser": "발사기",
    "Observer": "관찰자", "Piston": "피스톤", "Sticky Piston": "끈끈이 피스톤", "Redstone Lamp": "레드스톤 조명",
    "Daylight Detector": "햇빛 감지기", "Tripwire Hook": "철사 덫 갈고리", "Lever": "레버",
    "Oak Log": "참나무 원목", "Spruce Log": "가문비나무 원목", "Birch Log": "자작나무 원목", "Jungle Log": "정글나무 원목",
    "Acacia Log": "아카시아나무 원목", "Dark Oak Log": "짙은 참나무 원목", "Mangrove Log": "맹그로브나무 원목", "Cherry Log": "벚나무 원목",
    "Oak Planks": "참나무 판자", "Spruce Planks": "가문비나무 판자", "Birch Planks": "자작나무 판자", "Jungle Planks": "정글나무 판자",
    "Acacia Planks": "아카시아나무 판자", "Dark Oak Planks": "짙은 참나무 판자", "Mangrove Planks": "맹그로브나무 판자", "Cherry Planks": "벚나무 판자",
    "Glass": "유리", "Tinted Glass": "차광 유리", "Torch": "횃불", "Soul Torch": "영혼 횃불", "Lantern": "랜턴", "Soul Lantern": "영혼 랜턴",
}

VANILLA_OFFICIAL_GENERAL_BASE = {
    "Minecraft": "마인크래프트", "Options": "설정", "Video Settings": "비디오 설정", "Audio Settings": "소리 설정",
    "Singleplayer": "싱글플레이", "Multiplayer": "멀티플레이", "Game Menu": "게임 메뉴",
    "Achievements": "도전 과제", "Advancements": "발전 과제", "Statistics": "통계",
    "Creeper": "크리퍼", "Zombie": "좀비", "Skeleton": "스켈레톤", "Spider": "거미",
    "Cave Spider": "동굴 거미", "Enderman": "엔더맨", "Witch": "마녀", "Slime": "슬라임",
    "Magma Cube": "마그마 큐브", "Ghast": "가스트", "Blaze": "블레이즈", "Wither Skeleton": "위더 스켈레톤",
    "Ender Dragon": "엔더 드래곤", "Wither": "위더", "Warden": "워든", "Piglin": "피글린",
    "Piglin Brute": "피글린 야수", "Zombified Piglin": "좀비화 피글린", "Hoglin": "호글린", "Zoglin": "조글린",
    "Villager": "주민", "Iron Golem": "철 골렘", "Snow Golem": "눈 골렘", "Wolf": "늑대",
    "Cat": "고양이", "Horse": "말", "Donkey": "당나귀", "Mule": "노새", "Llama": "라마",
    "Fox": "여우", "Panda": "판다", "Polar Bear": "북극곰", "Bee": "꿀벌", "Bat": "박쥐",
    "Parrot": "앵무새", "Dolphin": "돌고래", "Sea Turtle": "바다거북", "Glow Squid": "발광 오징어",
    "Squid": "오징어", "Strider": "스트라이더", "Allay": "알레이", "Frog": "개구리", "Tadpole": "올챙이",
    "Camel": "낙타", "Sniffer": "스니퍼", "Breeze": "브리즈",
    # Effects
    "Speed": "속도 증가", "Slowness": "속도 감소", "Haste": "성급함", "Mining Fatigue": "채광 피로",
    "Strength": "힘", "Instant Health": "즉시 치유", "Instant Damage": "즉시 피해", "Jump Boost": "점프 강화",
    "Nausea": "멀미", "Regeneration": "재생", "Resistance": "저항", "Fire Resistance": "화염 저항",
    "Water Breathing": "수중 호흡", "Invisibility": "투명", "Blindness": "실명", "Night Vision": "야간 투시",
    "Hunger": "허기", "Weakness": "나약함", "Poison": "독", "Wither": "위더", "Health Boost": "생명력 강화",
    "Absorption": "흡수", "Saturation": "포만감", "Glowing": "발광", "Levitation": "공중 부양",
    "Luck": "행운", "Bad Luck": "불운", "Slow Falling": "느린 낙하", "Conduit Power": "전달체의 힘",
    "Dolphins Grace": "돌고래의 은총", "Bad Omen": "흉조", "Hero of the Village": "마을의 영웅", "Darkness": "어둠",
}


def build_cache_tiers():
    logging.info("🚀 [캐시 전수 조사] 모든 번역 캐시 파일 전수 분석 및 티어 할당 시작...")

    # 1. 로컬 캐시 파일들 로드
    cache_files = {
        "general": os.path.join(DIST_DIR, "translation_memory.json"),
        "items": os.path.join(DIST_DIR, "translation_memory_items.json"),
        "books": os.path.join(DIST_DIR, "translation_memory_books.json"),
    }

    caches = {}
    for cat, p in cache_files.items():
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            ko_dict = data.get("한국어 (Korean)", {}) or data.get("한국어(Korean)", {})
            caches[cat] = ko_dict
            logging.info(f"📂 [{cat}] 캐시 로드 완료: {len(ko_dict):,}개 항목")
        else:
            caches[cat] = {}

    # 2. 기존 ko_pack_keys 로드
    ko_pack_keys = {"general": set(), "items": set(), "books": set()}
    if os.path.exists(KO_PACK_KEYS_FILE):
        try:
            with open(KO_PACK_KEYS_FILE, "r", encoding="utf-8") as f:
                k_data = json.load(f)
            for cat in ("general", "items", "books"):
                ko_pack_keys[cat] = set(k_data.get(cat, []))
            logging.info(f"🔒 [기존 ko_pack_keys] {sum(len(v) for v in ko_pack_keys.values()):,}개 공식 키 로드")
        except Exception as e:
            logging.warning(f"ko_pack_keys 로드 오류: {e}")

    # 3. 단어 사전(translation_memory_words.json) 로드
    words_file = os.path.join(DIST_DIR, "translation_memory_words.json")
    words_keys = set()
    if os.path.exists(words_file):
        try:
            with open(words_file, "r", encoding="utf-8") as f:
                w_data = json.load(f)
            for k in w_data.get("proper_nouns", {}):
                words_keys.add(k.strip())
            for k in w_data.get("common_words", {}):
                words_keys.add(k.strip())
            logging.info(f"📚 [단어사전] {len(words_keys):,}개 단어/명사구 로드")
        except Exception as e:
            logging.warning(f"words 로드 오류: {e}")

    # 4. 티어 분류 인덱스 생성
    tier_0_keys = {"general": set(), "items": set(), "books": set()}
    tier_1_keys = {"general": set(), "items": set(), "books": set()}
    tier_2_keys = {"general": set(), "items": set(), "books": set()}

    # A. Tier 0 판정 (바닐라 공식 마인크래프트)
    for src, tgt in caches["items"].items():
        if src in VANILLA_OFFICIAL_ITEMS_BASE:
            tier_0_keys["items"].add(src)
    for src, tgt in caches["general"].items():
        if src in VANILLA_OFFICIAL_GENERAL_BASE or src in VANILLA_OFFICIAL_ITEMS_BASE:
            tier_0_keys["general"].add(src)

    # B. Tier 1 판정 (모드 공식 한글패치 & 사용자 직접 수정)
    for cat in ("general", "items", "books"):
        for k in ko_pack_keys[cat]:
            if k not in tier_0_keys[cat]:
                tier_1_keys[cat].add(k)

    # 단어 사전에 등록된 검증 고유명사 중 아이템/일반 캐시에 있는 것은 Tier 1 부여
    for word in words_keys:
        for cat in ("items", "general"):
            if word in caches[cat] and word not in tier_0_keys[cat]:
                tier_1_keys[cat].add(word)

    # C. 통계 계산
    total_entries = sum(len(caches[c]) for c in caches)
    t0_count = sum(len(tier_0_keys[c]) for c in tier_0_keys)
    t1_count = sum(len(tier_1_keys[c]) for c in tier_1_keys)
    t2_count = sum(len(tier_2_keys[c]) for c in tier_2_keys)
    t3_count = total_entries - (t0_count + t1_count + t2_count)

    tier_structure = {
        "version": "1.2",
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "meta": {
            "description": "QuestTranslator Cache Tier Master Index",
            "tier_definitions": {
                "0": "바닐라 마인크래프트 공식 번역 (Vanilla Minecraft Official) - 절대 불변 (오역수정기로도 수정 불가)",
                "1": "모드 공식 한글패치 (ko_pack) 및 사용자 직접 수정",
                "2": "고품질 AI 모델 (gemini-3.8-flash, claude-sonnet-5-5, gpt-6.1-terra, deepseek-v4-pro 등)",
                "3": "중품질/표준 AI 모델 (gemini-3.6-flash, gpt-5-mini, claude-haiku-5-5, gpt-4.1 등)",
                "4": "저품질/가성비 AI 모델 (gemini-3.5-flash-lite - 기본값, 미등록 데이터)"
            },
            "default_tier": 4,
            "default_ai_model": "gemini-3.5-flash-lite"
        },
        "summary": {
            "total_entries": total_entries,
            "tier_0_vanilla_official_immutable": t0_count,
            "tier_1_mod_official_and_user": t1_count,
            "tier_2_high_quality_ai": t2_count,
            "tier_3_mid_quality_ai": 0,
            "tier_4_budget_ai_gemini_3_5_flash_lite": t3_count
        },
        "tier_0_keys": {c: sorted(list(tier_0_keys[c])) for c in tier_0_keys},
        "tier_1_keys": {c: sorted(list(tier_1_keys[c])) for c in tier_1_keys},
        "tier_2_keys": {c: sorted(list(tier_2_keys[c])) for c in tier_2_keys},
        "tier_3_keys": {c: [] for c in tier_2_keys}
    }

    # 파일 저장 (인덴트 적용 JSON)
    tmp_path = TIERS_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(tier_structure, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, TIERS_FILE)

    # ko_pack_keys.json도 Tier 0 + Tier 1의 합집합으로 자동 동기화 갱신!
    merged_ko_pack = {
        c: sorted(list(tier_0_keys[c] | tier_1_keys[c]))
        for c in ("general", "items", "books")
    }
    tmp_ko = KO_PACK_KEYS_FILE + ".tmp"
    with open(tmp_ko, "w", encoding="utf-8") as f:
        json.dump(merged_ko_pack, f, ensure_ascii=False, indent=2)
    os.replace(tmp_ko, KO_PACK_KEYS_FILE)

    logging.info("=" * 60)
    logging.info("🎉 [캐시 티어 전수 조사 완료]")
    logging.info(f"• 총 조사된 캐시 항목: {total_entries:,}개")
    logging.info(f"  - 🔒 Tier 1 (바닐라 공식 + 모드 한패/사용자 수정): {t0_count + t1_count:,}개")
    logging.info(f"  - ✨ Tier 2 (고품질 AI): {t2_count:,}개")
    logging.info(f"  - ⚡ Tier 4 (gemini-3.5-flash-lite 기본값): {t3_count:,}개 (전체의 {t3_count/total_entries*100:.2f}%)")
    logging.info(f"💾 티어 마스터 파일 생성 완료: {TIERS_FILE}")
    logging.info(f"🔒 공식 보호 키 갱신 완료: {KO_PACK_KEYS_FILE} (총 {sum(len(v) for v in merged_ko_pack.values()):,}개 보호)")
    logging.info("=" * 60)

    return tier_structure


if __name__ == "__main__":
    build_cache_tiers()
