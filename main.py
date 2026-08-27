import os
import json
import random
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import List, Tuple, Optional
import tcod
import tcod.bsp
import tcod.map

# --- Constants & Configuration ---
MAP_WIDTH = 25
MAP_HEIGHT = 12
FOV_RADIUS = 4

# --- [1. D&D 5e Rule Engine] ---
def calc_modifier(stat: int) -> int:
    return (stat - 10) // 2

def roll_dice(sides: int, count: int = 1) -> int:
    return sum(random.randint(1, sides) for _ in range(count))

@dataclass
class Entity:
    name: str
    char: str
    x: int
    y: int
    hp: int
    max_hp: int
    ac: int
    str: int
    dex: int
    con: int
    int_: int
    wis: int
    cha: int

    @property
    def str_mod(self) -> int:
        return calc_modifier(self.str)
    
    @property
    def dex_mod(self) -> int:
        return calc_modifier(self.dex)
    
    @property
    def is_dead(self) -> bool:
        return self.hp <= 0

def resolve_melee_attack(attacker: Entity, defender: Entity) -> Tuple[bool, int, str]:
    """근접 공격 판정: 반환값 (명중여부, 데미지, 로그메시지)"""
    roll = roll_dice(20)
    total_attack = roll + attacker.str_mod
    
    if roll == 20: # 크리티컬 히트
        damage = roll_dice(8, 2) + attacker.str_mod # 1d8 무기 기준으로 크리티컬 2d8 
        return True, damage, "CRITICAL HIT!"
    elif roll == 1: # 크리티컬 미스
        return False, 0, "CRITICAL MISS!"
    elif total_attack >= defender.ac: # 일반 명중
        damage = max(1, roll_dice(8) + attacker.str_mod)
        return True, damage, "HIT"
    else: # 빗나감
        return False, 0, "MISS"

# --- [2. Procedural Map Generator] ---
class GameMap:
    def __init__(self, width: int, height: int):
        self.width = width
        self.height = height
        # 화면 렌더링용 배열: '#' 벽, '.' 바닥
        self.tiles = [['#' for _ in range(width)] for _ in range(height)]
        
        # tcod 시야(FOV) 연산용 객체 (False = 시야/이동 불가)
        self.tcod_map = tcod.map.Map(width, height)
        self.tcod_map.transparent[:] = False
        self.tcod_map.walkable[:] = False
    
    def carve_room(self, x: int, y: int, w: int, h: int):
        for i in range(x, x + w):
            for j in range(y, y + h):
                self.tiles[j][i] = '.'
                self.tcod_map.transparent[j, i] = True
                self.tcod_map.walkable[j, i] = True
                
    def carve_h_tunnel(self, x1: int, x2: int, y: int):
        for x in range(min(x1, x2), max(x1, x2) + 1):
            self.tiles[y][x] = '.'
            self.tcod_map.transparent[y, x] = True
            self.tcod_map.walkable[y, x] = True
            
    def carve_v_tunnel(self, y1: int, y2: int, x: int):
        for y in range(min(y1, y2), max(y1, y2) + 1):
            self.tiles[y][x] = '.'
            self.tcod_map.transparent[y, x] = True
            self.tcod_map.walkable[y, x] = True

def generate_map(width: int, height: int) -> Tuple[GameMap, Tuple[int, int], List[Entity]]:
    """tcod BSP를 이용해 방을 나누고 L자 복도로 연결"""
    game_map = GameMap(width, height)
    # BSP 트리 생성 및 분할
    bsp = tcod.bsp.BSP(x=1, y=1, width=width-2, height=height-2)
    bsp.split_recursive(depth=2, min_width=6, min_height=4, max_horizontal_ratio=1.5, max_vertical_ratio=1.5)
    
    centers = []
    
    # 잎(Leaf) 노드에만 방 생성
    for node in bsp.pre_order():
        if node.children:
            continue
        
        # 각 구역(Node) 내에 랜덤 크기 방 배치
        room_w = random.randint(3, node.width)
        room_h = random.randint(3, node.height)
        room_x = node.x + random.randint(0, node.width - room_w)
        room_y = node.y + random.randint(0, node.height - room_h)
        
        center_x = room_x + room_w // 2
        center_y = room_y + room_h // 2
        centers.append((center_x, center_y))
        
        game_map.carve_room(room_x, room_y, room_w, room_h)

    # 중심점들을 직각 복도(L자)로 연결
    for i in range(1, len(centers)):
        prev_x, prev_y = centers[i-1]
        curr_x, curr_y = centers[i]
        
        if random.random() < 0.5:
            game_map.carve_h_tunnel(prev_x, curr_x, prev_y)
            game_map.carve_v_tunnel(prev_y, curr_y, curr_x)
        else:
            game_map.carve_v_tunnel(prev_y, curr_y, prev_x)
            game_map.carve_h_tunnel(prev_x, curr_x, curr_y)
            
    # 첫번째 방 중심을 플레이어 스폰 위치로
    player_start = centers[0]
    
    # 나머지 방에 고블린 스폰
    entities = []
    for center in centers[1:]:
        goblin = Entity(
            name="Goblin", char='g', 
            x=center[0], y=center[1], 
            hp=7, max_hp=7, ac=12, 
            str=8, dex=14, con=10, int_=10, wis=8, cha=8
        )
        entities.append(goblin)
        
    return game_map, player_start, entities

# --- [3. AI Game Master Narrative] ---
def get_ai_narrative(payload: dict) -> str:
    """LLM을 호출하여 중요 턴의 상황을 묘사하는 텍스트 반환"""
    prompt = f"다음 TRPG 이벤트 결과에 대해 다크 판타지 스타일로 짧고 긴장감 넘치는 2~3줄의 묘사를 작성해. 결과는 다음과 같아: {json.dumps(payload, ensure_ascii=False)}"
    
    openai_key = os.environ.get("OPENAI_API_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    
    try:
        if openai_key:
            req = urllib.request.Request(
                "https://api.openai.com/v1/chat/completions",
                data=json.dumps({
                    "model": "gpt-3.5-turbo",
                    "messages": [{"role": "system", "content": "You are a dark fantasy TRPG Game Master."},
                                 {"role": "user", "content": prompt}],
                    "max_tokens": 150
                }).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {openai_key}"
                }
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                result = json.loads(response.read())
                return result['choices'][0]['message']['content'].strip()
                
        elif gemini_key:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={gemini_key}"
            req = urllib.request.Request(
                url,
                data=json.dumps({
                    "contents": [{"parts": [{"text": "You are a dark fantasy TRPG Game Master. " + prompt}]}]
                }).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                result = json.loads(response.read())
                return result['candidates'][0]['content']['parts'][0]['text'].strip()
        else:
            return f"[시스템] (API 키가 없어 AI 서사가 생략됨): {payload.get('event', payload.get('action'))} - {payload.get('result')}"
            
    except Exception as e:
        return f"[시스템/에러] AI 서사를 불러올 수 없습니다: {e}"

# --- [4. Game Loop & Terminal UI] ---
def render(game_map: GameMap, player: Entity, entities: List[Entity], recent_log: str):
    """현재 맵(시야 내)과 상태창, 로그를 터미널에 렌더링"""
    os.system('cls' if os.name == 'nt' else 'clear')
    
    print("=== HYBRID ROGUELIKE TRPG ===")
    
    for y in range(game_map.height):
        row = ""
        for x in range(game_map.width):
            if game_map.tcod_map.fov[y, x]: # 시야 안에 있음
                char_to_draw = game_map.tiles[y][x]
                for e in entities:
                    if e.x == x and e.y == y and not e.is_dead:
                        char_to_draw = e.char
                if player.x == x and player.y == y:
                    char_to_draw = player.char
                row += char_to_draw
            elif game_map.tiles[y][x] == '#': # 어둠 속의 벽 모양은 보통 안보이지만 편의상 공백 처리
                row += ' ' 
            else:
                row += ' '
        print(row)
        
    print("-" * 35)
    print(f" HP: {player.hp}/{player.max_hp} | AC: {player.ac} | STR: {player.str} | DEX: {player.dex} ")
    print("-" * 35)
    print(f"[GM] {recent_log}")
    print("-" * 35)
    print("명령어: w, a, s, d (이동/공격), action <행동 텍스트>, q (종료)")

def main():
    game_map, (px, py), entities = generate_map(MAP_WIDTH, MAP_HEIGHT)
    player = Entity(
        name="Hero", char='@', 
        x=px, y=py, 
        hp=30, max_hp=30, ac=14, 
        str=16, dex=14, con=14, int_=10, wis=12, cha=10
    )
    
    recent_log = "어두운 던전에 발을 들였습니다..."
    
    while True:
        # 턴마다 시야 업데이트
        game_map.tcod_map.compute_fov(player.x, player.y, radius=FOV_RADIUS, algorithm=tcod.FOV_BASIC)
        
        render(game_map, player, entities, recent_log)
        
        if player.is_dead:
            print("당신은 사망했습니다. 게임 오버!")
            break
            
        cmd = input("> ").strip().lower()
        if not cmd:
            continue
            
        if cmd == 'q':
            print("게임을 종료합니다.")
            break
            
        # 플레이어 이동 처리
        dx, dy = 0, 0
        if cmd == 'w': dy = -1
        elif cmd == 's': dy = 1
        elif cmd == 'a': dx = -1
        elif cmd == 'd': dx = 1
        
        if dx != 0 or dy != 0:
            target_x = player.x + dx
            target_y = player.y + dy
            
            # 이동 가능 확인
            if 0 <= target_x < MAP_WIDTH and 0 <= target_y < MAP_HEIGHT and game_map.tcod_map.walkable[target_y, target_x]:
                # 엔티티(몬스터) 충돌 확인
                hit_entity = None
                for e in entities:
                    if e.x == target_x and e.y == target_y and not e.is_dead:
                        hit_entity = e
                        break
                
                if hit_entity:
                    # 전투 (이동 대신 공격)
                    is_hit, dmg, res_str = resolve_melee_attack(player, hit_entity)
                    if is_hit:
                        hit_entity.hp -= dmg
                        payload = {
                            "action": f"{hit_entity.name} 공격",
                            "result": res_str,
                            "damage_dealt": dmg,
                        }
                        if hit_entity.is_dead:
                            payload["event"] = f"{hit_entity.name} 처치!"
                        
                        recent_log = get_ai_narrative(payload)
                    else:
                        recent_log = f"[시스템] 공격이 빗나갔습니다. ({res_str})"
                else:
                    # 빈 공간 이동
                    player.x = target_x
                    player.y = target_y
                    recent_log = f"[시스템] {cmd} 방향으로 이동했습니다."
            else:
                recent_log = "[시스템] 이동할 수 없습니다 (벽)."
                
        # 자유 행동 입력 처리 (action ~)
        elif cmd.startswith("action "):
            action_text = cmd[7:].strip()
            dc = 15 # 고정 난이도 (임시)
            roll = roll_dice(20)
            total = roll + player.dex_mod # 임시로 DEX 수정치 사용
            success = total >= dc
            
            payload = {
                "action": action_text,
                "d20_roll": roll,
                "dc": dc,
                "modifier": player.dex_mod,
                "result": "SUCCESS" if success else "FAIL",
                "event": "자유 행동 판정 시도"
            }
            recent_log = get_ai_narrative(payload)
        else:
            recent_log = "[시스템] 알 수 없는 명령어입니다."

if __name__ == "__main__":
    main()
